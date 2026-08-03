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
    kind: str                       # "drop", "low", or "spike"
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
    branded_fare: str | None = None
    earns_points: bool = True
    cents_per_point: float | None = None
    threshold_cents: float | None = None
    spike_pct: float | None = None

    @property
    def verdict(self) -> str | None:
        """Redeem or pay.

        This is a property of the fare, not of why the alert fired. A cheap
        fare on a route with a low saver floor can be both a good cash deal
        and good point value, and conflating the two made every drop on such
        a route report itself as a redemption signal.
        """
        if self.cents_per_point is None or self.threshold_cents is None:
            return None
        return ("redeem points" if self.cents_per_point >= self.threshold_cents
                else "pay cash")

    @property
    def pair(self) -> str:
        return f"{self.depart}|{self.ret or ''}"


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def cents_per_point(price: float, route: Route) -> float | None:
    """Value of redeeming at the route's saver floor, in cents per point.

    Alaska's own-metal awards price dynamically *above* a distance-band floor,
    so the floor is the best case. This number therefore answers "if saver
    space exists at the chart price, is burning points better than paying?"
    rather than "what will the award actually cost". Without an award data
    feed that is the honest limit of what can be computed, and it is still
    the useful half: it tells you when checking award space is worth the
    trouble.
    """
    if not route.award_floor_points:
        return None
    return price / route.award_floor_points * 100


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

    # A fare well above baseline is the signal to spend points rather than
    # cash, but only when redeeming at the floor actually beats your
    # valuation. Otherwise it is just an unaffordable flight, which is not
    # news worth an email.
    cpp = cents_per_point(current.price, route)
    spike = None
    if baseline and baseline > 0 and route.spike_pct and cpp is not None:
        rise = (current.price - baseline) / baseline * 100
        if rise >= route.spike_pct and cpp >= route.redeem_above_cents:
            spike = rise
            triggered = True

    if not triggered:
        return None

    if spike is not None:
        kind = "spike"
    elif drop_pct is not None and drop_pct >= route.drop_pct:
        kind = "drop"
    else:
        kind = "low"

    if _debounced(route, current, state, asof, kind):
        return None

    return Alert(
        kind=kind,
        branded_fare=current.branded_fare,
        earns_points=not (current.branded_fare
                          and "SAVER" in current.branded_fare.upper()),
        cents_per_point=cpp,
        threshold_cents=route.redeem_above_cents if cpp is not None else None,
        spike_pct=spike,
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


def _key(route_key: str, depart: str, ret: str | None, kind: str) -> str:
    return f"{route_key}|{depart}|{ret or ''}|{kind}"


def _debounced(
    route: Route, current: Observation, state: dict, asof: datetime, kind: str
) -> bool:
    """True if this trigger already fired recently and has not moved further.

    Keyed by kind, since a drop and a redeem signal are different events. The
    "moved further" test also has to flip direction: for a drop we want a
    lower price to re-alert, for a redeem signal a higher one.
    """
    record = state.get("alerts", {}).get(_key(route.key, current.depart, current.ret, kind))
    if not record:
        return False

    if asof - _parse(record["at"]) >= timedelta(hours=route.debounce_hours):
        return False

    margin = route.realert_pct / 100
    if kind == "spike":
        return current.price < record["price"] * (1 + margin)
    return current.price > record["price"] * (1 - margin)


def record_alert(alert: Alert, state: dict, asof: datetime) -> None:
    key = _key(alert.route_key, alert.depart, alert.ret, alert.kind)
    state.setdefault("alerts", {})[key] = {"at": asof.isoformat(), "price": alert.price}
