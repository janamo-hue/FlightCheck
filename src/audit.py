"""Sanity-check recorded observations for signs the scraper is misreading.

This exists because the fare grid can be parsed successfully, produce
plausible-looking numbers, and still be wrong. The mislabelled-column bug sat
in 33 observations for as long as the tool had been running: every SEA-ABQ
row had its tracked fare exactly $100 above the Saver, every SEA-MSY row
exactly $110, across base fares from $358 to $1,038. A real brand
differential moves with the fare. A constant one is an artefact.

Run: python -m src.audit [--history data/history.jsonl]
Exit code is non-zero on findings, so CI can gate on it.
"""

from __future__ import annotations

import argparse
from collections import defaultdict

from . import store

# Below this many observations a constant gap is unremarkable.
MIN_SAMPLE = 5
# Domestic round trips above this are almost certainly a currency or parse bug.
IMPLAUSIBLE_USD = 5000


def constant_gap(rows: list[store.Observation]) -> list[str]:
    """Flag routes where the tracked fare sits a fixed distance above Saver."""
    findings = []
    by_route: dict[str, list[store.Observation]] = defaultdict(list)
    for o in rows:
        if o.saver_price:
            by_route[o.route].append(o)

    for route, obs in sorted(by_route.items()):
        gaps = [round(o.price - o.saver_price, 2) for o in obs]
        if len(gaps) < MIN_SAMPLE:
            continue
        spread = max(gaps) - min(gaps)
        prices = [o.price for o in obs]
        price_spread = max(prices) - min(prices)

        # A fixed gap is only damning when the underlying fares actually moved.
        if spread == 0 and price_spread > max(gaps[0] * 2, 100):
            findings.append(
                f"{route}: tracked fare is exactly {gaps[0]:,.0f} above Saver in "
                f"all {len(gaps)} observations, while the fare itself ranged over "
                f"{price_spread:,.0f}. A brand differential moves with the fare; "
                f"a constant one means the two prices are not the brands they "
                f"claim to be.")
    return findings


def implausible_prices(rows: list[store.Observation]) -> list[str]:
    return [
        f"{o.route} {o.depart}: {o.currency} {o.price:,.0f} with "
        f"{len(o.flight_numbers)} flight numbers. Above {IMPLAUSIBLE_USD:,} is "
        f"usually a currency mismatch or a parse error, not a fare."
        for o in rows
        if o.price > IMPLAUSIBLE_USD
    ]


def duplicate_flights(rows: list[store.Observation]) -> list[str]:
    findings = []
    for o in rows:
        nums = o.flight_numbers or []
        if len(nums) != len(set(nums)):
            findings.append(
                f"{o.route} {o.depart}: repeated flight numbers {nums}. The "
                f"same card is being read more than once.")
    return findings


def leg_count(rows: list[store.Observation]) -> list[str]:
    """A nonstop round trip has two flight numbers, a one-way has one."""
    findings = []
    for o in rows:
        expected = 2 if o.ret else 1
        nums = set(o.flight_numbers or [])
        if nums and len(nums) > expected:
            findings.append(
                f"{o.route} {o.depart}: {len(nums)} distinct flights "
                f"{sorted(nums)} for a {'round trip' if o.ret else 'one way'}. "
                f"Connections are leaking past the nonstop filter.")
    return findings


def missing_ladder(rows: list[store.Observation]) -> list[str]:
    blind = [o for o in rows if not o.fare_ladder]
    if not blind:
        return []
    return [f"{len(blind)} of {len(rows)} observations carry no fare ladder, so "
            f"a mislabelled column cannot be detected after the fact."]


CHECKS = (
    ("constant brand gap", constant_gap),
    ("implausible prices", implausible_prices),
    ("duplicate flight numbers", duplicate_flights),
    ("leg count", leg_count),
    ("missing fare ladder", missing_ladder),
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", default=None)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    rows = store.load_history(args.history)
    if not rows:
        print("no observations to audit")
        return 0

    total = 0
    for name, check in CHECKS:
        findings = check(rows)
        total += len(findings)
        if findings:
            print(f"\n[{name}] {len(findings)} finding(s)")
            for f in findings[:10] if args.quiet else findings:
                print(f"  - {f}")
            if args.quiet and len(findings) > 10:
                print(f"  ... and {len(findings) - 10} more")
        elif not args.quiet:
            print(f"[{name}] clean")

    print(f"\n{len(rows)} observations audited, {total} finding(s)")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
