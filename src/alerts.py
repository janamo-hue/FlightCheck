"""Decide which observations are worth an email.

Baseline is the median of the trailing baseline_days of observations for the
same route and date pair, excluding the current one. Median rather than mean
so a single fluke fare does not poison the reference.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import Route
from .store import Observation


@dataclass
class Alert:
    route_name: str
    route_key: str
    depart: str
    ret: str | None
    price: float
    currency: str
    baseline: float | None
    drop_pct: float | None
    all_time_low: bool
    flight_numbers: list[str]
    observations: int

    @property
    def pair(self) -> str:
        return f"{self.depart}|{self.ret or ''}"


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def baseline_for(prior: list[Observation], route: Route, asof: datetime) -> float | None:
    cutoff = asof - timedelta(days=route.baseline_days)
    window = [o.price for o in prior if _parse(o.observed_at) >= cutoff]
    if len(window) < route.min_observations:
        return None
    return statistics.median(window)


def evaluate(
    route: Route,
    current: Observation,
    prior: list[Observation],
    state: dict,
    asof: datetime | None = None,
) -> Alert | None:
    asof = asof or datetime.now(timezone.utc)
    if not prior:
        return None

    baseline = baseline_for(prior, route, asof)
    drop_pct = None
    triggered = False

    if baseline and baseline > 0:
        drop_pct = (baseline - current.price) / baseline * 100
        if drop_pct >= route.drop_pct:
            triggered = True

    # Scope the low to a fixed horizon. Comparing against everything retained
    # meant one cheap fare from eight months ago muted this trigger forever,
    # while the percent baseline only looked back baseline_days. Two horizons
    # for two triggers made the alerting hard to reason about.
    atl_cutoff = (asof - timedelta(days=route.atl_days)).isoformat()
    recent = [o for o in prior if o.observed_at >= atl_cutoff]

    all_time_low = False
    if recent:
        # Require a real margin, otherwise a flat fare that ticks down a dollar
        # generates an alert on every run.
        prior_low = min(o.price for o in recent)
        all_time_low = current.price < prior_low * (1 - route.all_time_low_margin_pct / 100)

    if (
        route.alert_on_all_time_low
        and all_time_low
        and len(recent) >= route.min_observations
    ):
        triggered = True

    if not triggered:
        return None

    if _debounced(route, current, state, asof):
        return None

    return Alert(
        route_name=route.name,
        route_key=route.key,
        depart=current.depart,
        ret=current.ret,
        price=current.price,
        currency=current.currency,
        baseline=baseline,
        drop_pct=drop_pct,
        all_time_low=all_time_low,
        flight_numbers=current.flight_numbers,
        observations=len(prior),
    )


def _debounced(route: Route, current: Observation, state: dict, asof: datetime) -> bool:
    """True if we already alerted recently and the price has not fallen further."""
    record = state.get("alerts", {}).get(f"{route.key}|{current.depart}|{current.ret or ''}")
    if not record:
        return False

    age = asof - _parse(record["at"])
    if age >= timedelta(hours=route.debounce_hours):
        return False

    threshold = record["price"] * (1 - route.realert_pct / 100)
    return current.price > threshold


def record_alert(alert: Alert, state: dict, asof: datetime) -> None:
    state.setdefault("alerts", {})[f"{alert.route_key}|{alert.depart}|{alert.ret or ''}"] = {
        "at": asof.isoformat(),
        "price": alert.price,
    }
