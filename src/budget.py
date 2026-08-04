"""Project monthly Amadeus call usage for the current routes.yml.

Run: python -m src.budget
"""

from __future__ import annotations

from datetime import date

from . import config, planner


def main() -> int:
    cfg = config.load()
    today = date.today()

    print(f"{'route':<28} {'sweep':>7} {'per run':>7} {'per month':>10}")
    print("-" * 56)

    total = 0
    for route in cfg.routes:
        sweep = len(planner.sweep_tasks(route, today))
        daily = min(route.watchlist_size, sweep)
        # Sweeps stay weekly; only the watchlist re-check scales with run count.
        monthly = sweep * 4.35 + daily * 30 * cfg.runs_per_day
        total += monthly
        print(f"{route.name:<28} {sweep:>7} {daily:>7} {monthly:>10,.0f}")

    print("-" * 56)
    print(f"{'total':<28} {'':>7} {'':>7} {total:>10,.0f}")
    # Not an API allowance any more: Amadeus was decommissioned and this is a
    # self-imposed ceiling on page loads against alaskaair.com.
    print(f"\n{cfg.runs_per_day} run(s)/day. Page-load budget: "
          f"{cfg.monthly_call_quota:,}/month. "
          f"Projected use: {total / cfg.monthly_call_quota * 100:.0f}%.")

    peak = max(
        (len(planner.sweep_tasks(r, today)) for r in cfg.routes), default=0
    ) + sum(r.watchlist_size for r in cfg.routes)
    if peak > cfg.daily_call_budget:
        days = -(-peak // max(cfg.daily_call_budget, 1))
        print(f"\nNote: a sweep needs up to {peak} calls but daily_call_budget "
              f"is {cfg.daily_call_budget}, so a cycle will span about {days} "
              f"runs. Work is resumed, not dropped, but the far window updates "
              f"more slowly. Raise the budget or the stride to tighten it.")

    if total > cfg.monthly_call_quota:
        print("\nOver quota. Raise sweep_stride_days, shrink watchlist_size, "
              "narrow window_end_days, or drop a route.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
