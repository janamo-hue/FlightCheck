"""Replay stored history against the configured targets.

Tuning a target by guessing is how you end up with a watcher that is either
silent for months or mails you every morning. This replays data/history.jsonl
against routes.yml and reports, per route, how many observations would have
beaten each target and what the cheapest fares actually were.

    python -m src.targets              # current targets
    python -m src.targets --sweep      # what a range of targets would yield

Read-only: it never writes history, state, or email.
"""

from __future__ import annotations

import argparse
import statistics
from collections import defaultdict

from . import config, store


def _rungs(observations):
    """(MAIN, SAVER) price lists for one route."""
    main = [o.price for o in observations]
    saver = [o.saver_price for o in observations if o.saver_price is not None]
    return main, saver


def _describe(label: str, prices: list[float], target: float | None) -> str:
    if not prices:
        return f"  {label:6} no data"
    lo, med = min(prices), statistics.median(prices)
    line = f"  {label:6} n={len(prices):4} min={lo:7,.0f} median={med:7,.0f}"
    if target is None:
        return line + "   target unset"
    hits = sum(1 for p in prices if p <= target)
    pct = 100 * hits / len(prices)
    line += f"   target={target:,.0f} -> {hits} hits ({pct:.0f}% of observations)"
    if hits == 0:
        line += f"\n         nothing has ever reached it; the floor is {lo:,.0f}"
    return line


def _sweep(label: str, prices: list[float]) -> str:
    """How many observations a range of candidate targets would have caught."""
    if not prices:
        return ""
    lo = min(prices)
    out = [f"  {label} candidate targets (floor {lo:,.0f}):"]
    for mult in (0.85, 0.90, 0.95, 1.00, 1.05):
        t = round(lo * mult)
        hits = sum(1 for p in prices if p <= t)
        out.append(f"    {t:7,.0f}  {hits:4} hits ({100*hits/len(prices):.0f}%)")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sweep", action="store_true",
                    help="also show what nearby target values would have caught")
    args = ap.parse_args(argv)

    cfg = config.load()
    history = store.load_history()
    by_route = defaultdict(list)
    for o in history:
        by_route[o.route].append(o)

    if not history:
        print("No history yet, so there is nothing to tune against.")
        return 0

    for route in cfg.routes:
        obs = by_route.get(route.key, [])
        print(f"\n{route.name} ({route.key})")
        main_p, saver_p = _rungs(obs)
        print(_describe("MAIN", main_p, route.target_price))
        print(_describe("SAVER", saver_p, route.saver_target_price))
        if args.sweep:
            for label, prices in (("MAIN", main_p), ("SAVER", saver_p)):
                out = _sweep(label, prices)
                if out:
                    print(out)

    print("\nThese counts are over past observations, not future ones. A target "
          "that would have hit on most of them will mail you constantly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
