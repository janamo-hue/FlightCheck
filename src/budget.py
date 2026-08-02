"""Project monthly Amadeus call usage for the current routes.yml.

Run: python -m src.budget
"""

from __future__ import annotations

from datetime import date

from . import config, planner

FREE_TIER = 2000


def main() -> int:
    cfg = config.load()
    today = date.today()

    print(f"{'route':<28} {'sweep':>7} {'daily':>7} {'per month':>10}")
    print("-" * 56)

    total = 0
    for route in cfg.routes:
        sweep = len(planner.sweep_tasks(route, today))
        daily = min(route.watchlist_size, sweep)
        monthly = sweep * 4.35 + daily * 30
        total += monthly
        print(f"{route.name:<28} {sweep:>7} {daily:>7} {monthly:>10,.0f}")

    print("-" * 56)
    print(f"{'total':<28} {'':>7} {'':>7} {total:>10,.0f}")
    print(f"\nAmadeus free tier: {FREE_TIER:,}/month. "
          f"Projected use: {total / FREE_TIER * 100:.0f}% of quota.")

    peak = max(
        (len(planner.sweep_tasks(r, today)) for r in cfg.routes), default=0
    ) + sum(r.watchlist_size for r in cfg.routes)
    if peak > cfg.daily_call_budget:
        days = -(-peak // max(cfg.daily_call_budget, 1))
        print(f"\nNote: a sweep needs up to {peak} calls but daily_call_budget "
              f"is {cfg.daily_call_budget}, so a cycle will span about {days} "
              f"runs. Work is resumed, not dropped, but the far window updates "
              f"more slowly. Raise the budget or the stride to tighten it.")

    if total > FREE_TIER:
        print("\nOver quota. Raise sweep_stride_days, shrink watchlist_size, "
              "narrow window_end_days, or drop a route.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
