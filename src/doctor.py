"""Preflight checks. Run this before the first real scan.

The failure mode this exists to catch: a route with no Alaska nonstop service
returns no offers, the scanner logs it at debug level and moves on, and the
tool runs cleanly forever without ever alerting. That looks exactly like
success. Everything else here is cheap to check at the same time.

Run: python -m src.doctor [--skip-live] [--probes 3]
Exit code is non-zero if any check fails, so CI can gate on it.
"""

from __future__ import annotations

import argparse
import os
import re
from datetime import date, timedelta

import requests

from . import config, planner
from .amadeus import Amadeus, QuotaExceeded

IATA = re.compile(r"^[A-Z]{3}$")

OK, WARN, FAIL = "pass", "warn", "FAIL"
MARK = {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL "}


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, status: str, check: str, detail: str = "") -> None:
        self.rows.append((status, check, detail))
        print(f"[{MARK[status]}] {check}" + (f"\n{' ' * 9}{detail}" if detail else ""))

    @property
    def failed(self) -> int:
        return sum(1 for s, _, _ in self.rows if s == FAIL)

    @property
    def warned(self) -> int:
        return sum(1 for s, _, _ in self.rows if s == WARN)


# --------------------------------------------------------------------- config


def check_config(rep: Report):
    try:
        cfg = config.load()
    except Exception as exc:
        rep.add(FAIL, "routes.yml parses", str(exc))
        return None

    rep.add(OK, "routes.yml parses", f"{len(cfg.routes)} routes configured")

    for route in cfg.routes:
        bad = [c for c in (route.origin, route.destination) if not IATA.match(c)]
        if bad:
            rep.add(FAIL, f"{route.name}: airport codes",
                    f"not 3-letter uppercase IATA: {bad}")
        if route.window_start_days >= route.window_end_days:
            rep.add(FAIL, f"{route.name}: window",
                    f"start {route.window_start_days} is not before end {route.window_end_days}")
        if not route.one_way and not route.trip_lengths:
            rep.add(FAIL, f"{route.name}: trip_lengths", "round trip with no lengths set")
        if route.award_floor_points is None:
            rep.add(WARN, f"{route.name}: award floor",
                    "unset, so no redeem/pay verdict for this route")

    today = date.today()
    monthly = sum(
        len(planner.sweep_tasks(r, today)) * 4.35 + min(r.watchlist_size, len(planner.sweep_tasks(r, today))) * 30
        for r in cfg.routes
    )
    pct = monthly / cfg.monthly_call_quota * 100
    status = FAIL if pct > 100 else WARN if pct > 85 else OK
    rep.add(status, "monthly quota projection",
            f"{monthly:,.0f} of {cfg.monthly_call_quota:,} calls ({pct:.0f}%)")
    return cfg


# ---------------------------------------------------------------- credentials


def check_credentials(rep: Report) -> Amadeus | None:
    missing = [k for k in ("AMADEUS_CLIENT_ID", "AMADEUS_CLIENT_SECRET") if not os.environ.get(k)]
    if missing:
        rep.add(FAIL, "Amadeus credentials present", f"unset: {', '.join(missing)}")
        return None

    env = os.environ.get("AMADEUS_ENV", "production")
    if env == "test":
        rep.add(WARN, "Amadeus environment",
                "test env serves cached, incomplete inventory. Prices from it "
                "are not trustworthy. Move to production before relying on alerts.")
    else:
        rep.add(OK, "Amadeus environment", "production")

    client = Amadeus()
    try:
        client._auth()
        rep.add(OK, "Amadeus authenticates", client.host)
    except Exception as exc:
        rep.add(FAIL, "Amadeus authenticates", str(exc)[:200])
        return None
    return client


def check_email(rep: Report) -> None:
    key, to = os.environ.get("RESEND_API_KEY"), os.environ.get("ALERT_EMAIL_TO")
    if not key or not to:
        rep.add(FAIL, "email configured",
                "RESEND_API_KEY or ALERT_EMAIL_TO unset, alerts will be computed "
                "and then silently dropped")
        return

    sender = os.environ.get("ALERT_EMAIL_FROM", "alerts@resend.dev")
    try:
        # Read-only endpoint. Validates the key without sending anything.
        resp = requests.get(
            "https://api.resend.com/domains",
            headers={"Authorization": f"Bearer {key}"}, timeout=20)
    except Exception as exc:
        rep.add(WARN, "Resend reachable", str(exc)[:150])
        return

    if resp.status_code == 401:
        rep.add(FAIL, "Resend key valid", "401 unauthorized")
        return
    if resp.status_code >= 300:
        rep.add(WARN, "Resend key valid", f"unexpected {resp.status_code}")
        return

    rep.add(OK, "Resend key valid", f"sending to {to} from {sender}")
    if sender.endswith("resend.dev"):
        rep.add(WARN, "sender domain",
                "resend.dev only delivers to your own verified address. Verify "
                "a domain for anything else.")


# ---------------------------------------------------------------------- live


def probe_dates(route, today: date, n: int) -> list[tuple[str, str | None]]:
    """Spread n probes across the window rather than clustering at one end."""
    lo, hi = route.window_start_days, route.window_end_days
    out = []
    for i in range(n):
        offset = lo + int((hi - lo) * (i + 1) / (n + 1))
        depart = today + timedelta(days=offset)
        ret = None if route.one_way else depart + timedelta(days=route.trip_lengths[0])
        out.append((depart.isoformat(), ret.isoformat() if ret else None))
    return out


def check_live(rep: Report, cfg, client: Amadeus, probes: int) -> None:
    today = date.today()

    for route in cfg.routes:
        found, prices, carriers, segment_counts = 0, [], set(), []
        brands, savers, premiums = set(), 0, []

        for depart, ret in probe_dates(route, today, probes):
            try:
                offer = client.cheapest_direct(
                    route.origin, route.destination, depart, ret,
                    carrier=route.carrier, adults=route.adults,
                    currency=route.currency, cabin=route.cabin,
                    nonstop=route.nonstop,
                    exclude_saver=route.exclude_saver,
                    max_offers=route.max_offers)
            except QuotaExceeded as exc:
                rep.add(FAIL, "Amadeus quota", str(exc)[:150])
                return
            except Exception as exc:
                rep.add(WARN, f"{route.name}: probe {depart}", str(exc)[:150])
                continue

            if offer is None:
                continue
            found += 1
            prices.append(offer.price)
            brands.add(offer.branded_fare or offer.fare_basis or "unlabelled")
            savers += offer.savers_excluded
            if offer.saver_price is not None:
                premiums.append(offer.price - offer.saver_price)
            carriers.update(offer.carrier.split(","))
            segment_counts.append(len(offer.flight_numbers))

        label = f"{route.name} ({route.key})"
        if found == 0:
            rep.add(FAIL, f"{label}: service exists",
                    f"0 of {probes} probes returned an offer. Either {route.carrier} "
                    f"does not fly this route" +
                    (" nonstop" if route.nonstop else "") +
                    ", or the codes are wrong. This route will never alert.")
            continue

        detail = (f"{found}/{probes} probes, "
                  f"{route.currency} {min(prices):,.0f} to {max(prices):,.0f}, "
                  f"carriers {','.join(sorted(carriers))}")
        rep.add(OK if found == probes else WARN, f"{label}: service exists", detail)

        stray = carriers - {route.carrier}
        if stray:
            rep.add(WARN, f"{label}: operating carrier",
                    f"offers include segments on {','.join(sorted(stray))}. "
                    f"includedAirlineCodes filters on the marketing carrier, so "
                    f"these are codeshares.")

        if route.nonstop and any(c > (1 if route.one_way else 2) for c in segment_counts):
            rep.add(WARN, f"{label}: nonstop filter",
                    "an offer has more segments than legs, which should not "
                    "happen with nonStop=true")

        rep.add(OK if brands != {"unlabelled"} else WARN,
                f"{label}: fare brands",
                f"tracked offers are {', '.join(sorted(brands))}"
                + ("" if brands != {"unlabelled"} else
                   ". No brandedFare in the response, so Saver detection and "
                   "exclusion cannot work on this route."))

        if route.exclude_saver:
            if not premiums:
                rep.add(WARN, f"{label}: Saver exclusion",
                        "no Saver fares appeared in any probe, so exclusion is "
                        "either working silently or not matching the brand string")
            else:
                avg = sum(premiums) / len(premiums)
                per_point = (avg / route.distance_miles * 100
                             if route.distance_miles else None)
                rep.add(OK, f"{label}: Saver exclusion",
                        f"{savers} Saver offers skipped across probes; tracked "
                        f"fare averages {route.currency} {avg:,.0f} above the Saver"
                        + (f", {per_point:.1f}c per point earned" if per_point else ""))

        if route.award_floor_points:
            cpp = min(prices) / route.award_floor_points * 100
            rep.add(OK, f"{label}: points check",
                    f"cheapest probe is {cpp:.1f}c per point at the "
                    f"{route.award_floor_points:,} floor "
                    f"(threshold {route.redeem_above_cents}c)")


# ---------------------------------------------------------------------- main


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-live", action="store_true",
                        help="config and credentials only, no API calls")
    parser.add_argument("--probes", type=int, default=3,
                        help="live probes per route, spread across the window")
    args = parser.parse_args(argv)

    rep = Report()
    print("\nconfig\n" + "-" * 60)
    cfg = check_config(rep)

    print("\ncredentials\n" + "-" * 60)
    client = check_credentials(rep) if cfg else None
    check_email(rep)

    if cfg and client and not args.skip_live:
        print(f"\nlive service check ({args.probes} probes per route)\n" + "-" * 60)
        check_live(rep, cfg, client, args.probes)
        print(f"\nspent {client.calls_made} Amadeus calls")

    print("\n" + "=" * 60)
    print(f"{len(rep.rows)} checks, {rep.failed} failed, {rep.warned} warnings")
    if rep.failed:
        print("Fix the failures before enabling the cron.")
    return 1 if rep.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
