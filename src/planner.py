"""Decide which (departure, return) pairs to price on a given run.

Two tiers:
  sweep     once a week, sample the whole window at sweep_stride_days
  watchlist every run, re-price the K cheapest pairs the last sweep found

This keeps the far end of the window under observation without loading a page
per date per day, which the self-imposed page-load ceiling could not support.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from itertools import zip_longest

from .config import Route


@dataclass(frozen=True)
class Task:
    route: Route
    depart: str
    ret: str | None
    tier: str  # "sweep" or "watchlist"

    @property
    def pair(self) -> str:
        return f"{self.depart}|{self.ret or ''}"


def _on_grid(d: date, stride: int, weekly: bool) -> bool:
    """Membership in a calendar-anchored grid.

    Anchored to the calendar (toordinal), never to the run date. The previous
    version strided from ``today + window_start_days``, so each weekly sweep
    sampled an almost disjoint set of departures: measured on 16 days of real
    data, consecutive sweeps overlapped on 3 dates out of ~18, which left 125
    of 133 tracked pairs with a single observation and, with min_observations
    gating alerts, structurally unable to alert at all.

    With a fixed phase, a departure date is on the grid or it is not, for
    life. Every sweep revisits the same dates as the window slides, so history
    accumulates and booking curves become comparable.

    When a weekday filter is active the phase within the week is already fixed
    by the filter, so the stride selects every Nth ISO-stable week instead
    (toordinal//7 keeps no year-boundary seam).
    """
    if weekly:
        return (d.toordinal() // 7) % stride == 0
    return d.toordinal() % stride == 0


def window_dates(route: Route, today: date) -> list[date]:
    """Departure dates to sample: a fixed calendar grid, tiered by distance.

    Dates within ``near_days`` of the window start use ``near_stride_days``;
    the rest use ``sweep_stride_days``. Sixteen days of observations showed
    fares five months out moving $10 while the volatility sits close in, so
    the near tier samples densely where repricing actually happens. Keeping
    ``sweep_stride_days`` a multiple of ``near_stride_days`` means a date
    sampled in the far tier stays on the grid when it crosses into the near
    tier, so its history is continuous.
    """
    start = today + timedelta(days=route.window_start_days)
    end = today + timedelta(days=route.window_end_days)
    near_end = today + timedelta(days=route.near_days) if route.near_days else start

    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    if route.depart_weekdays:
        days = [d for d in days if d.weekday() in route.depart_weekdays]

    weekly = bool(route.depart_weekdays)
    out = []
    for d in days:
        stride = (route.near_stride_days
                  if route.near_stride_days and d <= near_end
                  else route.sweep_stride_days)
        if _on_grid(d, stride, weekly):
            out.append(d)
    return out


def _pairs(route: Route, depart: date) -> list[tuple[str, str | None]]:
    if route.one_way:
        return [(depart.isoformat(), None)]
    return [
        (depart.isoformat(), (depart + timedelta(days=n)).isoformat())
        for n in route.trip_lengths
    ]


def sweep_tasks(route: Route, today: date) -> list[Task]:
    tasks = []
    for depart in window_dates(route, today):
        for dep, ret in _pairs(route, depart):
            tasks.append(Task(route=route, depart=dep, ret=ret, tier="sweep"))
    return tasks


def route_monthly_loads(route: Route, runs_per_day: int, today: date) -> float:
    """Projected page loads/month for one route.

    Sweeps stay weekly (4.35 weeks/month); only the daily watchlist re-check
    scales with runs_per_day. Single-sourced here so src.budget and src.doctor
    cannot drift apart.
    """
    sweep = len(sweep_tasks(route, today))
    daily = min(route.watchlist_size, sweep)
    return sweep * 4.35 + daily * 30 * runs_per_day


def projected_monthly_loads(routes: list[Route], runs_per_day: int, today: date) -> float:
    return sum(route_monthly_loads(r, runs_per_day, today) for r in routes)


def should_sweep(route: Route, today: date, state: dict) -> bool:
    last = state.get("last_sweep", {}).get(route.key)
    if last is None:
        return True
    if today.weekday() == route.sweep_weekday:
        return last != today.isoformat()
    # Safety net: never let a route go more than 10 days without a sweep.
    return (today - date.fromisoformat(last)).days >= 10


def open_sweep(route: Route, today: date, state: dict) -> list[str]:
    """Pairs still owed for this route's current sweep cycle.

    Opens a new cycle when one is due. Mutates state: the pending list has to
    survive to the next run, otherwise a sweep truncated by the call budget is
    silently forgotten and the route waits a full week for another chance.
    """
    cycles = state.setdefault("sweep_cycles", {})
    cycle = cycles.get(route.key)

    if cycle is None:
        if not should_sweep(route, today, state):
            return []
        cycle = {
            "started": today.isoformat(),
            "pending": [t.pair for t in sweep_tasks(route, today)],
        }
        cycles[route.key] = cycle

    # Drop pairs that rolled out of the window while the cycle was in progress.
    horizon = today + timedelta(days=route.window_start_days)
    cycle["pending"] = [
        p for p in cycle["pending"]
        if date.fromisoformat(p.partition("|")[0]) >= horizon
    ]
    return cycle["pending"]


def mark_priced(route: Route, pair: str, state: dict) -> None:
    """Retire a pair from the open sweep cycle. Called on attempt, not success.

    A date with no Alaska nonstop service returns no offer, and retrying it
    every run would burn the quota forever.
    """
    cycle = state.get("sweep_cycles", {}).get(route.key)
    if cycle and pair in cycle["pending"]:
        cycle["pending"].remove(pair)


def sweep_complete(route: Route, today: date, state: dict) -> bool:
    """True if the cycle just finished. Closes it out and stamps last_sweep."""
    cycles = state.get("sweep_cycles", {})
    cycle = cycles.get(route.key)
    if cycle is None or cycle["pending"]:
        return False
    del cycles[route.key]
    state.setdefault("last_sweep", {})[route.key] = today.isoformat()
    return True


def watchlist_tasks(route: Route, today: date, state: dict) -> list[Task]:
    pairs = state.get("watchlists", {}).get(route.key, [])
    horizon = today + timedelta(days=route.window_start_days)

    tasks = []
    for pair in pairs[: route.watchlist_size]:
        depart, _, ret = pair.partition("|")
        if date.fromisoformat(depart) < horizon:
            continue  # fell out of the window since the last sweep
        tasks.append(Task(route=route, depart=depart, ret=ret or None, tier="watchlist"))
    return tasks


def plan(routes: list[Route], today: date, state: dict, budget: int) -> list[Task]:
    """Build the run's task list, watchlist first so it survives truncation.

    Sweep work left over after the budget stays in state and resumes next run,
    interleaved round-robin so one long route cannot starve the others.
    """
    watch: list[Task] = []
    backlogs: list[list[Task]] = []

    for route in routes:
        watch.extend(watchlist_tasks(route, today, state))
        pending = open_sweep(route, today, state)
        if pending:
            backlogs.append([
                Task(route=route, depart=p.partition("|")[0],
                     ret=p.partition("|")[2] or None, tier="sweep")
                for p in pending
            ])

    sweep: list[Task] = []
    for row in zip_longest(*backlogs):
        sweep.extend(t for t in row if t is not None)

    seen: set[tuple[str, str]] = set()
    ordered: list[Task] = []
    for task in watch + sweep:
        ident = (task.route.key, task.pair)
        if ident in seen:
            continue
        seen.add(ident)
        ordered.append(task)

    return ordered[:budget]
