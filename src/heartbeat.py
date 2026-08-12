"""Weekly heartbeat: a proof-of-life digest emailed via Resend.

Alert emails only fire on a price drop, so a long silence is ambiguous: it
could mean stable fares, or it could mean the cron stopped firing, a key went
missing, or the scraper is being blocked. This digest removes that ambiguity by
reporting, once a week, what the scanner actually did.

It reports, for the trailing window:
  - how many times the scan ran, and the price checks it made
  - the prices it found, summarised per route
  - the routes being watched
  - how many alert emails went out as a result
  - health signals: last run, monthly call usage, and a warning if nothing ran

Run: python -m src.heartbeat [--days 7] [--dry-run]

Reads only committed state and history, so it needs no browser and no scraping.
"""

from __future__ import annotations

import argparse
import html
import logging
from collections import defaultdict
from datetime import timedelta

from . import config, notify, store

log = logging.getLogger("heartbeat")


def _fmt(dt_iso: str) -> str:
    return dt_iso[:16].replace("T", " ") + " UTC" if dt_iso else "never"


def build(days: int = 7) -> tuple[str, str]:
    """Return (subject, html_body) for the trailing `days` window."""
    cfg = config.load()
    state = store.load_state()
    history = store.load_history()
    asof = store.now()
    since = asof - timedelta(days=days)
    cutoff = since.isoformat()

    runs = store.runs_since(state, since)
    scans = len(runs)
    checks = sum(r.get("calls", 0) for r in runs)
    observations = sum(r.get("observations", 0) for r in runs)
    alerts_fired = sum(r.get("alerts", 0) for r in runs)
    emails_sent = sum(1 for r in runs if r.get("emailed"))
    last_run = max((r.get("at", "") for r in runs), default="")

    recent = [o for o in history if o.observed_at >= cutoff]
    by_route: dict[str, list] = defaultdict(list)
    for o in recent:
        by_route[o.route].append(o)

    q = store.quota(state, asof)

    # Per-route price summary, configured routes first so an unwatched route
    # shows up as an explicit gap rather than silently missing.
    route_rows = []
    for route in cfg.routes:
        obs = by_route.get(route.key, [])
        if obs:
            prices = [o.price for o in obs]
            latest = max(obs, key=lambda o: o.observed_at)
            cur = latest.currency
            cell = (f"{len(obs)} checks &middot; low {cur} {min(prices):,.0f} "
                    f"&middot; latest {cur} {latest.price:,.0f}")
        else:
            cell = '<span style="color:#a1a1aa;">no observations this week</span>'
        trip = "one way" if route.one_way else f"round trip ({'/'.join(map(str, route.trip_lengths))}d)"
        route_rows.append(
            f'<tr><td style="padding:6px 10px 6px 0;"><strong>{html.escape(route.name)}</strong>'
            f'<div style="color:#71717a;font-size:12px;">{route.key} &middot; '
            f'{route.cabin.lower()} &middot; {trip}</div></td>'
            f'<td style="padding:6px 0;">{cell}</td></tr>')

    warn = ""
    if scans == 0:
        warn = ('<div style="background:#fef2f2;border:1px solid #fecaca;color:#991b1b;'
                'padding:10px 12px;border-radius:8px;margin:0 0 16px;font-size:13px;">'
                '<strong>No scans ran in this window.</strong> The scheduled scan may not '
                'be firing (check the workflow schedule and that RESEND_API_KEY / '
                'ALERT_EMAIL_TO are set). No price data is being collected.</div>')

    subject = (f"FlightCheck heartbeat: {scans} scans, {observations} prices, "
               f"{emails_sent} alert emails (last {days}d)")

    stat = lambda label, val: (  # noqa: E731 - tiny local formatter
        f'<td style="padding:8px 14px 8px 0;">'
        f'<div style="font-size:22px;font-weight:700;">{val}</div>'
        f'<div style="color:#71717a;font-size:12px;">{label}</div></td>')

    report_link = (f'<p style="margin-top:18px;"><a href="{notify.REPORT_URL}">'
                   f'Full price history and charts</a></p>' if notify.REPORT_URL else "")

    body = f"""<html><body style="font-family:-apple-system,Segoe UI,sans-serif;color:#18181b;max-width:640px;">
      <h2 style="margin:0 0 2px;">FlightCheck weekly heartbeat</h2>
      <p style="color:#71717a;margin:0 0 16px;font-size:13px;">
        Activity for the last {days} days, as of {_fmt(asof.isoformat())}.</p>
      {warn}
      <table style="border-collapse:collapse;margin-bottom:8px;"><tr>
        {stat("scans run", scans)}
        {stat("price checks", checks)}
        {stat("prices recorded", observations)}
        {stat("alerts", alerts_fired)}
        {stat("alert emails", emails_sent)}
      </tr></table>
      <p style="color:#71717a;font-size:12px;margin:0 0 20px;">
        Last scan: {_fmt(last_run)} &middot; monthly call usage: {q.get('calls', 0):,}.</p>

      <h3 style="font-size:15px;margin:0 0 8px;">Routes watched ({len(cfg.routes)})</h3>
      <table style="width:100%;border-collapse:collapse;font-size:14px;">{''.join(route_rows)}</table>
      {report_link}
      <p style="color:#a1a1aa;font-size:11px;margin-top:20px;">
        Heartbeat is informational. Price-drop alerts are sent separately, only when a
        fare falls below its baseline or hits a recent low.</p>
    </body></html>"""
    return subject, body


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Send the weekly heartbeat email")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the subject and skip sending")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    subject, body = build(args.days)
    if args.dry_run:
        print(subject)
        print(body)
        return 0

    ok = notify.send_email(subject, body)
    log.info("heartbeat %s: %s", "sent" if ok else "not sent", subject)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
