"""Entry point. Run: python -m src.scan [--dry-run] [--sweep] [--limit N]"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from . import alerts as alerting
from . import config, notify, planner, store
from .amadeus import Amadeus, QuotaExceeded

log = logging.getLogger("scan")


def refresh_watchlist(route, history, state, today):
    """Rank this route's live date pairs by cheapest price seen and keep top K."""
    horizon = today.toordinal() + route.window_start_days
    best: dict[str, float] = {}
    for obs in history:
        if obs.route != route.key:
            continue
        if date.fromisoformat(obs.depart).toordinal() < horizon:
            continue
        best[obs.pair] = min(best.get(obs.pair, float("inf")), obs.price)

    ranked = sorted(best, key=lambda p: best[p])[: route.watchlist_size]
    state.setdefault("watchlists", {})[route.key] = ranked


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="price everything, send nothing, write nothing")
    parser.add_argument("--sweep", action="store_true",
                        help="force a deep sweep regardless of weekday")
    parser.add_argument("--limit", type=int, help="override the per-run call budget")
    parser.add_argument("--config", default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    cfg = config.load(args.config)
    history = store.load_history()
    state = store.load_state()
    today = date.today()
    asof = store.now()

    if args.sweep:
        state["last_sweep"] = {}
        state["sweep_cycles"] = {}

    budget = args.limit or cfg.daily_call_budget

    # Stop short of the monthly allowance rather than discovering it mid-month
    # when Amadeus starts rejecting calls and the tool goes quiet.
    q = store.quota(state, asof)
    ceiling = int(cfg.monthly_call_quota * cfg.quota_reserve_pct / 100)
    remaining = ceiling - q["calls"]
    if remaining <= 0:
        log.warning(
            "monthly quota reserve reached: %d/%d calls used this month, "
            "resuming in %s", q["calls"], ceiling, "next month")
        return 0
    if remaining < budget:
        log.warning("throttling to %d calls, only %d left before the reserve",
                    remaining, remaining)
        budget = remaining

    tasks = planner.plan(cfg.routes, today, state, budget)
    if not tasks:
        log.info("nothing to price today")
        return 0

    sweeping = {t.route.key for t in tasks if t.tier == "sweep"}
    owed = sum(len(c["pending"]) for c in state.get("sweep_cycles", {}).values())
    log.info("%d calls planned, %d routes sweeping, %d sweep pairs owed",
             len(tasks), len(sweeping), owed)

    client = Amadeus()
    buckets = store.index(history)
    fresh: list[store.Observation] = []
    triggered: list[alerting.Alert] = []

    for i, task in enumerate(tasks, 1):
        try:
            offer = client.cheapest_direct(
                task.route.origin,
                task.route.destination,
                task.depart,
                task.ret,
                carrier=task.route.carrier,
                adults=task.route.adults,
                currency=task.route.currency,
                cabin=task.route.cabin,
                nonstop=task.route.nonstop,
                exclude_saver=task.route.exclude_saver,
                max_offers=task.route.max_offers,
            )
        except QuotaExceeded as exc:
            log.error("Amadeus quota exhausted after %d calls: %s", client.calls_made, exc)
            break
        except Exception:
            log.exception("failed pricing %s %s", task.route.key, task.pair)
            continue

        # Attempted counts as done. Retrying a date with no service every run
        # would never let the cycle close.
        planner.mark_priced(task.route, task.pair, state)

        if offer is None:
            log.debug("no direct %s service %s %s", task.route.carrier, task.route.key, task.pair)
            continue

        obs = store.Observation(
            route=task.route.key,
            depart=task.depart,
            ret=task.ret,
            price=offer.price,
            currency=offer.currency,
            flight_numbers=offer.flight_numbers,
            observed_at=asof.isoformat(),
            branded_fare=offer.branded_fare,
            saver_price=offer.saver_price,
        )
        fresh.append(obs)
        log.info("[%d/%d] %s %s %s %s %.0f", i, len(tasks), task.tier,
                 task.route.key, task.depart, obs.currency, obs.price)

        prior = buckets.get((task.route.key, obs.pair), [])
        alert = alerting.evaluate(task.route, obs, prior, state, asof)
        if alert:
            triggered.append(alert)
            alerting.record_alert(alert, state, asof)

    log.info("%d observations, %d alerts, %d api calls",
             len(fresh), len(triggered), client.calls_made)

    if args.dry_run:
        notify.send(triggered, dry_run=True)
        for a in triggered:
            print(f"  {a.route_key} {a.depart} {a.currency} {a.price:,.0f} "
                  f"drop={a.drop_pct and round(a.drop_pct)} atl={a.all_time_low}")
        return 0

    store.append(fresh)
    combined = history + fresh
    for route in cfg.routes:
        if planner.sweep_complete(route, today, state):
            refresh_watchlist(route, combined, state, today)
            log.info("sweep complete: %s", route.key)
        else:
            owed = len(state.get("sweep_cycles", {}).get(route.key, {}).get("pending", []))
            if owed:
                log.info("sweep resumes next run: %s, %d pairs owed", route.key, owed)

    if triggered:
        notify.send(triggered)

    used = store.spend_quota(state, client.calls_made, asof)
    live, moved = store.archive()
    dropped = store.prune_alert_state(state, asof=asof)
    log.info("quota %d/%d this month | history %d live, %d archived | "
             "%d stale alert records dropped",
             used, cfg.monthly_call_quota, live, moved, dropped)

    store.save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
