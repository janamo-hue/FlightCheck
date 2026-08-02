"""Decide which (departure, return) pairs to price on a given run.

Two tiers:
  sweep     once a week, sample the whole window at sweep_stride_days
  watchlist every run, re-price the K cheapest pairs the last sweep found

This keeps the far end of the window under observation without spending a
call per date per day, which the free Amadeus quota cannot support.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

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


def window_dates(route: Route, today: date) -> list[date]:
    start = today + timedelta(days=route.window_start_days)
    end = today + timedelta(days=route.window_end_days)

    out = []
    cursor = start
    while cursor <= end:
        if not route.depart_weekdays or cursor.weekday() in route.depart_weekdays:
            out.append(cursor)
        cursor += timedelta(days=route.sweep_stride_days)
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


def should_sweep(route: Route, today: date, state: dict) -> bool:
    last = state.get("last_sweep", {}).get(route.key)
    if last is None:
        return True
    if today.weekday() == route.sweep_weekday:
        return last != today.isoformat()
    # Safety net: never let a route go more than 10 days without a sweep.
    return (today - date.fromisoformat(last)).days >= 10


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
    """Build the run's task list, watchlist first so it survives truncation."""
    watch: list[Task] = []
    sweep: list[Task] = []

    for route in routes:
        watch.extend(watchlist_tasks(route, today, state))
        if should_sweep(route, today, state):
            sweep.extend(sweep_tasks(route, today))

    seen: set[tuple[str, str]] = set()
    ordered: list[Task] = []
    for task in watch + sweep:
        ident = (task.route.key, task.pair)
        if ident in seen:
            continue
        seen.add(ident)
        ordered.append(task)

    return ordered[:budget]
