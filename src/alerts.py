"""Decide which observations are worth an email.

Two families of trigger, and they answer different questions.

The relative triggers (drop, low, spike) compare a fare against its own past.
Baseline is the median of the trailing baseline_days of observations for the
same route and date pair, excluding the current one. Median rather than mean
so a single fluke fare does not poison the reference. These need history, and
min_observations gates them.

The absolute triggers (target) compare a fare against a number you chose in
routes.yml. They need no history at all, so they fire on the first sighting of
a date pair. This exists because the relative triggers are structurally blind
to the case that matters most: a fare sitting at its floor is cheap on every
observation, so its median equals its current price, it never sets a new low,
and it never alerts. Target triggers take precedence in the kind ordering,
since "this is under the price you would pay" is more actionable than "this
moved".
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

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
    cities: tuple[str, str] | None = None
    branded_fare: str | None = None
    earns_points: bool = True
    saver_price: float | None = None
    distance_miles: int | None = None
    cents_per_point: float | None = None
    threshold_cents: float | None = None
    spike_pct: float | None = None
    # Which fare rung beat its target ("MAIN" or "SAVER"), the threshold it
    # beat, and the price that beat it. trigger_price differs from `price`
    # for a Saver hit, where `price` is still the priced MAIN fare.
    target_fare: str | None = None
    target_price: float | None = None
    trigger_price: float | None = None

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
    def saver_premium(self) -> float | None:
        """Extra cost of this fare over the cheapest Saver on the same search."""
        if self.saver_price is None:
            return None
        return self.price - self.saver_price

    @property
    def cost_per_point_earned(self) -> float | None:
        """Cents paid per Atmos point earned by buying up from Saver.

        Saver earns nothing, so the premium buys the whole distance-based
        accrual. Compare against point_value_cents: below it, buying up is
        cheaper than acquiring the points any other way.
        """
        premium = self.saver_premium
        if premium is None or not self.distance_miles or premium <= 0:
            return None
        return premium / self.distance_miles * 100

    @property
    def alert_price(self) -> float:
        """The price this alert is actually about.

        For a Saver target hit that is the Saver fare, not the MAIN fare in
        `price`. Debouncing and re-alerting key off this, otherwise a Saver
        alert would be suppressed or re-fired by MAIN movement it has nothing
        to do with.
        """
        return self.trigger_price if self.trigger_price is not None else self.price

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
    asof = asof or datetime.now(UTC)

    # Absolute targets are checked before the history guard below, because
    # needing no history is the entire point of them. Saver first: it is the
    # cheaper rung, so when both beat their targets it is the one worth
    # leading with. If a Saver hit is debounced we still fall through to the
    # MAIN target rather than going silent.
    target_fare = target_threshold = trigger_price = None
    for fare, threshold, value in (
        ("SAVER", route.saver_target_price, current.saver_price),
        ("MAIN", route.target_price, current.price),
    ):
        if threshold is None or value is None or value > threshold:
            continue
        if _debounced(route, current, state, asof, _target_kind(fare), value):
            continue
        target_fare, target_threshold, trigger_price = fare, threshold, value
        break

    if not prior and target_fare is None:
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

    if target_fare is not None:
        triggered = True

    if not triggered:
        return None

    # Target wins the label: it is the one that says "book this", where the
    # others say "this moved".
    if target_fare is not None:
        kind = _target_kind(target_fare)
    elif spike is not None:
        kind = "spike"
    elif drop_pct is not None and drop_pct >= route.drop_pct:
        kind = "drop"
    else:
        kind = "low"

    # Target kinds already cleared debounce above, on the rung that triggered.
    if target_fare is None and _debounced(route, current, state, asof, kind,
                                          current.price):
        return None

    return Alert(
        kind=kind,
        cities=route.cities(),
        branded_fare=current.branded_fare,
        saver_price=current.saver_price,
        distance_miles=route.distance_miles,
        earns_points=not (current.branded_fare
                          and "SAVER" in current.branded_fare.upper()),
        cents_per_point=cpp,
        threshold_cents=route.redeem_above_cents if cpp is not None else None,
        spike_pct=spike,
        target_fare=target_fare,
        target_price=target_threshold,
        trigger_price=trigger_price,
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


def _target_kind(fare: str) -> str:
    """Distinct kind per rung, so MAIN and Saver debounce independently."""
    return "target" if fare == "MAIN" else "target-saver"


def _key(route_key: str, depart: str, ret: str | None, kind: str) -> str:
    return f"{route_key}|{depart}|{ret or ''}|{kind}"


def _debounced(
    route: Route, current: Observation, state: dict, asof: datetime, kind: str,
    price: float,
) -> bool:
    """True if this trigger already fired recently and has not moved further.

    Keyed by kind, since a drop and a redeem signal are different events. The
    "moved further" test also has to flip direction: for a drop we want a
    lower price to re-alert, for a redeem signal a higher one.

    `price` is passed rather than read off `current` because a Saver target
    fires on the Saver rung while `current.price` holds the MAIN fare, and
    comparing the two would let MAIN movement release or suppress a Saver
    alert.
    """
    record = state.get("alerts", {}).get(_key(route.key, current.depart, current.ret, kind))
    if not record:
        return False

    if asof - _parse(record["at"]) >= timedelta(hours=route.debounce_hours):
        return False

    margin = route.realert_pct / 100
    if kind == "spike":
        return price < record["price"] * (1 + margin)
    return price > record["price"] * (1 - margin)


def record_alert(alert: Alert, state: dict, asof: datetime) -> None:
    key = _key(alert.route_key, alert.depart, alert.ret, alert.kind)
    state.setdefault("alerts", {})[key] = {
        "at": asof.isoformat(), "price": alert.alert_price,
    }
