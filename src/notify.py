"""Send one digest email per run via Resend."""

from __future__ import annotations

import html
import logging
import os
from urllib.parse import urlencode

import requests

from .alerts import Alert

log = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"


REPORT_URL = os.environ.get("REPORT_URL", "")


def booking_url(alert: Alert) -> str:
    """Google Flights search for the exact dates.

    An earlier version built an alaskaair.com/search/results deep link from
    guessed query parameters. Alaska publishes no dated deep-link format and
    the guess could not be verified, so it was dropped rather than ship a
    link that may land on an empty search. Google Flights takes a plain
    natural language query and is the only source here that can be pinned to
    a specific departure and return.
    """
    origin, destination = alert.route_key.split("-")
    query = f"Flights from {origin} to {destination} on {alert.depart}"
    if alert.ret:
        query += f" through {alert.ret}"
    query += " nonstop"
    return "https://www.google.com/travel/flights?" + urlencode({"q": query})


def alaska_url(alert: Alert) -> str | None:
    """Alaska's own route page, which carries a fare calendar.

    Verified to exist and resolve. Not date-specific, so it complements the
    Google Flights link rather than replacing it: this one is where you
    actually book, that one is where you confirm the date.
    """
    if not alert.cities:
        return None
    origin, destination = alert.cities
    return f"https://www.alaskaair.com/en/flights-from-{origin}-to-{destination}"


def _links(alert: Alert) -> str:
    style = ("display:inline-block;padding:7px 12px;margin:6px 6px 0 0;"
             "border-radius:6px;font-size:13px;text-decoration:none;")
    out = [f'<a href="{booking_url(alert)}" '
           f'style="{style}background:#2563eb;color:#fff;font-weight:600;">'
           f'See these dates</a>']
    alaska = alaska_url(alert)
    if alaska:
        out.append(f'<a href="{alaska}" '
                   f'style="{style}border:1px solid #2563eb;color:#2563eb;">'
                   f'Book on Alaska</a>')
    return "".join(out)


def render(alerts: list[Alert]) -> tuple[str, str]:
    redeems = [a for a in alerts if a.kind == "spike"]
    if redeems:
        best = max(redeems, key=lambda a: a.cents_per_point or 0)
        subject = (f"Worth points: {best.route_name} "
                   f"{best.cents_per_point:.1f}c/pt at {best.currency} {best.price:,.0f}")
    else:
        best = max(alerts, key=lambda a: a.drop_pct or 0)
        if best.drop_pct:
            subject = (f"Fare drop: {best.route_name} {best.currency} "
                       f"{best.price:,.0f} ({best.drop_pct:.0f}% off)")
        else:
            subject = f"New low: {best.route_name} {best.currency} {best.price:,.0f}"
    if len(alerts) > 1:
        subject += f" +{len(alerts) - 1} more"

    rows = []
    for a in sorted(alerts, key=lambda x: (x.kind != "spike", -(x.drop_pct or 0))):
        tags = []
        if a.kind == "spike":
            if a.spike_pct is not None:
                tags.append(f"{a.spike_pct:.0f}% above the median")
            if a.cents_per_point is not None:
                tags.append(f"{a.cents_per_point:.1f}c per point at the saver floor")
        if a.drop_pct is not None and a.drop_pct > 0:
            tags.append(f"{a.drop_pct:.0f}% below the {a.observations}-sample median")
        if a.all_time_low:
            tags.append("all-time low")
        if not a.earns_points:
            tags.append(f"{a.branded_fare} fare, earns zero points")
        elif a.saver_premium is not None and a.saver_premium > 0:
            cpe = a.cost_per_point_earned
            note = (f"{a.currency} {a.saver_premium:,.0f} above the Saver at "
                    f"{a.currency} {a.saver_price:,.0f}")
            if cpe is not None:
                note += f", {cpe:.1f}c per point earned"
            tags.append(note)
        dates = a.depart + (f" to {a.ret}" if a.ret else " (one way)")
        baseline = f"{a.currency} {a.baseline:,.0f}" if a.baseline else "n/a"

        rows.append(
            f"""
            <tr>
              <td style="padding:12px 8px;border-bottom:1px solid #e4e4e7;">
                <div style="font-weight:600;">{html.escape(a.route_name)}</div>
                <div style="color:#71717a;font-size:13px;">{html.escape(dates)}</div>
                <div style="color:#a1a1aa;font-size:12px;">{html.escape(", ".join(a.flight_numbers))}</div>
              </td>
              <td style="padding:12px 8px;border-bottom:1px solid #e4e4e7;text-align:right;">
                <div style="font-size:20px;font-weight:700;">{a.currency} {a.price:,.0f}</div>
                <div style="color:#71717a;font-size:12px;">baseline {baseline}</div>
                <div style="color:{'#b45309' if a.kind == 'redeem' else '#15803d'};font-size:12px;">
                  {html.escape(", ".join(tags))}</div>
                <div style="font-size:12px;font-weight:600;">{html.escape(a.verdict or '')}</div>
                <div>{_links(a)}</div>
              </td>
            </tr>"""
        )

    body = f"""<html><body style="font-family:-apple-system,Segoe UI,sans-serif;color:#18181b;">
      <h2 style="margin:0 0 4px;">Alaska direct fare alerts</h2>
      <p style="color:#71717a;margin:0 0 16px;font-size:13px;">
        Nonstop Alaska-marketed fares only. Prices are GDS totals and may differ
        slightly from alaskaair.com. Saver fares are excluded where configured,
        since they earn no Atmos points. Cents per point assumes saver space exists
        at the route's chart floor, which is the best case, not a quote. Check
        award space before acting on it.
      </p>
      <table style="width:100%;border-collapse:collapse;">{''.join(rows)}</table>
      {f'<p style="margin-top:20px;"><a href="{REPORT_URL}">Full price history and charts</a></p>' if REPORT_URL else ''}
    </body></html>"""

    return subject, body


def send(alerts: list[Alert], dry_run: bool = False) -> bool:
    if not alerts:
        return False

    subject, body = render(alerts)
    if dry_run:
        print(f"[dry-run] subject: {subject}")
        return False

    api_key = os.environ.get("RESEND_API_KEY")
    to = os.environ.get("ALERT_EMAIL_TO")
    sender = os.environ.get("ALERT_EMAIL_FROM", "alerts@resend.dev")
    if not api_key or not to:
        log.warning("RESEND_API_KEY or ALERT_EMAIL_TO unset, skipping email")
        return False

    resp = requests.post(
        RESEND_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={"from": sender, "to": [t.strip() for t in to.split(",")], "subject": subject, "html": body},
        timeout=30,
    )
    if resp.status_code >= 300:
        log.error("resend failed %s: %s", resp.status_code, resp.text[:300])
        return False
    return True
