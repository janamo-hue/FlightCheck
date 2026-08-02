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


def booking_url(alert: Alert) -> str:
    """Deep link into Alaska's search results for this itinerary."""
    origin, destination = alert.route_key.split("-")
    params = {
        "O": origin,
        "D": destination,
        "OD": alert.depart,
        "A": "1",
        "C": "0",
        "L": "0",
        "RT": "true" if alert.ret else "false",
    }
    if alert.ret:
        params["DD"] = alert.ret
    return f"https://www.alaskaair.com/search/results?{urlencode(params)}"


def render(alerts: list[Alert]) -> tuple[str, str]:
    best = max(alerts, key=lambda a: a.drop_pct or 0)
    if best.drop_pct:
        subject = f"Fare drop: {best.route_name} {best.currency} {best.price:,.0f} ({best.drop_pct:.0f}% off)"
    else:
        subject = f"New low: {best.route_name} {best.currency} {best.price:,.0f}"
    if len(alerts) > 1:
        subject += f" +{len(alerts) - 1} more"

    rows = []
    for a in sorted(alerts, key=lambda x: -(x.drop_pct or 0)):
        tags = []
        if a.drop_pct is not None:
            tags.append(f"{a.drop_pct:.0f}% below the {a.observations}-sample median")
        if a.all_time_low:
            tags.append("all-time low")
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
                <div style="color:#15803d;font-size:12px;">{html.escape(", ".join(tags))}</div>
                <a href="{booking_url(a)}" style="font-size:12px;">book</a>
              </td>
            </tr>"""
        )

    body = f"""<html><body style="font-family:-apple-system,Segoe UI,sans-serif;color:#18181b;">
      <h2 style="margin:0 0 4px;">Alaska direct fare alerts</h2>
      <p style="color:#71717a;margin:0 0 16px;font-size:13px;">
        Nonstop Alaska-marketed fares only. Prices are GDS totals and may differ
        slightly from alaskaair.com.
      </p>
      <table style="width:100%;border-collapse:collapse;">{''.join(rows)}</table>
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
