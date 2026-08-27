"""Send one digest email per run via Resend."""

from __future__ import annotations

import html
import logging
import os

import requests

from . import links
from .alerts import Alert

log = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"


REPORT_URL = os.environ.get("REPORT_URL", "")


def booking_url(alert: Alert) -> str:
    """Google Flights search for the exact dates (see src.links)."""
    origin, destination = alert.route_key.split("-")
    return links.google_flights_url(origin, destination, alert.depart, alert.ret)


def alaska_url(alert: Alert) -> str | None:
    """Alaska's own route page, which carries a fare calendar (see src.links)."""
    return links.alaska_route_url(alert.cities)


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


def _is_target(a: Alert) -> bool:
    return a.kind.startswith("target")


def render(alerts: list[Alert]) -> tuple[str, str]:
    targets = [a for a in alerts if _is_target(a)]
    spikes = [a for a in alerts if a.kind == "spike"]
    if targets:
        # Lead with the biggest margin under target, not the lowest price: a
        # fare 80 under its target is better news than a cheaper route sitting
        # 5 under its own.
        best = max(targets, key=lambda a: (a.target_price or 0) - a.alert_price)
        subject = (f"Under target: {best.route_name} {best.currency} "
                   f"{best.alert_price:,.0f}")
        if best.target_fare == "SAVER":
            subject += " Saver"
        if best.target_price:
            subject += f" (target {best.target_price:,.0f})"
    elif spikes:
        best = max(spikes, key=lambda a: a.cents_per_point or 0)
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
    for a in sorted(alerts, key=lambda x: (not _is_target(x), x.kind != "spike",
                                           -(x.drop_pct or 0))):
        tags = []
        if _is_target(a) and a.target_price is not None:
            under = a.target_price - a.alert_price
            rung = "Saver" if a.target_fare == "SAVER" else (a.branded_fare or "MAIN").title()
            tags.append(f"{rung} at {a.currency} {a.alert_price:,.0f}, "
                        f"{a.currency} {under:,.0f} under your "
                        f"{a.currency} {a.target_price:,.0f} target")
            if not a.observations:
                tags.append("first time this date pair has been priced")
        if a.kind == "spike":
            if a.spike_pct is not None:
                tags.append(f"{a.spike_pct:.0f}% above the median")
            if a.cents_per_point is not None:
                tags.append(f"{a.cents_per_point:.1f}c per point at the saver floor")
        if a.drop_pct is not None and a.drop_pct > 0:
            tags.append(f"{a.drop_pct:.0f}% below the {a.observations}-sample median")
        if a.all_time_low:
            tags.append("all-time low")
        if a.target_fare == "SAVER":
            tags.append("Saver earns no Atmos points")
        elif not a.earns_points:
            tags.append(f"{a.branded_fare} fare, earns zero points")
        elif a.saver_premium is not None and a.saver_premium > 0:
            cpe = a.cost_per_point_earned
            note = (f"{a.currency} {a.saver_premium:,.0f} above the Saver at "
                    f"{a.currency} {a.saver_price:,.0f}")
            if cpe is not None:
                note += f", {cpe:.1f}c per point earned"
            tags.append(note)
        dates = a.depart + (f" to {a.ret}" if a.ret else " (one way)")
        # A Saver alert leads with the Saver fare, so the second line should
        # show the MAIN fare it undercuts rather than a baseline that may not
        # exist yet. Everything else keeps the baseline it was always showing.
        if a.target_fare == "SAVER":
            subline = f"MAIN {a.currency} {a.price:,.0f}"
        elif a.baseline:
            subline = f"baseline {a.currency} {a.baseline:,.0f}"
        else:
            subline = "no baseline yet"

        rows.append(
            f"""
            <tr>
              <td style="padding:12px 8px;border-bottom:1px solid #e4e4e7;">
                <div style="font-weight:600;">{html.escape(a.route_name)}</div>
                <div style="color:#71717a;font-size:13px;">{html.escape(dates)}</div>
                <div style="color:#a1a1aa;font-size:12px;">{html.escape(", ".join(a.flight_numbers))}</div>
              </td>
              <td style="padding:12px 8px;border-bottom:1px solid #e4e4e7;text-align:right;">
                <div style="font-size:20px;font-weight:700;">{a.currency} {a.alert_price:,.0f}</div>
                <div style="color:#71717a;font-size:12px;">{html.escape(subline)}</div>
                <div style="color:{'#b45309' if a.kind == 'spike' else '#15803d'};font-size:12px;">
                  {html.escape(", ".join(tags))}</div>
                <div style="font-size:12px;font-weight:600;">{html.escape(a.verdict or '')}</div>
                <div>{_links(a)}</div>
              </td>
            </tr>"""
        )

    body = f"""<html><body style="font-family:-apple-system,Segoe UI,sans-serif;color:#18181b;">
      <h2 style="margin:0 0 4px;">Alaska direct fare alerts</h2>
      <p style="color:#71717a;margin:0 0 16px;font-size:13px;">
        Nonstop Alaska-marketed fares only, read from alaskaair.com's own
        results page. Saver is excluded from the priced fare where configured,
        since it earns no Atmos points, but is still watched against its own
        target and alerted on. Cents per point assumes saver space exists
        at the route's chart floor, which is the best case, not a quote. Check
        award space before acting on it.
      </p>
      <table style="width:100%;border-collapse:collapse;">{''.join(rows)}</table>
      {f'<p style="margin-top:20px;"><a href="{REPORT_URL}">Full price history and charts</a></p>' if REPORT_URL else ''}
    </body></html>"""

    return subject, body


def send_email(subject: str, body: str) -> bool:
    """Deliver one HTML email via Resend. Shared by alerts and the heartbeat.

    Returns True only on a real 2xx send, so callers can record whether mail
    actually went out rather than assuming it did.
    """
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


def send(alerts: list[Alert], dry_run: bool = False) -> bool:
    if not alerts:
        return False

    subject, body = render(alerts)
    if dry_run:
        print(f"[dry-run] subject: {subject}")
        return False

    return send_email(subject, body)
