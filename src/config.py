"""Load routes.yml and merge defaults into each route."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml

CONFIG_PATH = os.environ.get("ROUTES_FILE", "routes.yml")


@dataclass
class Route:
    name: str
    origin: str
    destination: str
    adults: int = 1
    currency: str = "USD"
    cabin: str = "ECONOMY"
    carrier: str = "AS"
    nonstop: bool = True
    exclude_saver: bool = True
    max_offers: int = 20
    distance_miles: int | None = None      # for the points-earned maths
    window_start_days: int = 14
    window_end_days: int = 180
    sweep_stride_days: int = 4
    # Tiered sampling: dates within near_days of today use near_stride_days.
    # Leave near_stride_days unset for a single-tier grid. Keep
    # sweep_stride_days a multiple of near_stride_days so a date stays on the
    # grid as it crosses the boundary.
    near_days: int = 60
    near_stride_days: int | None = None
    sweep_weekday: int = 6
    # Sweep every N days instead of on a fixed weekday. A weekly sweep cannot
    # see a fare sale that opens and closes inside the week, which is most of
    # them. When set, this overrides sweep_weekday.
    sweep_interval_days: int | None = None
    depart_weekdays: list[int] = field(default_factory=list)
    watchlist_size: int = 8
    trip_lengths: list[int] = field(default_factory=lambda: [7])
    one_way: bool = False
    drop_pct: float = 15.0
    baseline_days: int = 14
    min_observations: int = 3
    alert_on_all_time_low: bool = True
    all_time_low_margin_pct: float = 3.0
    atl_days: int = 90
    debounce_hours: int = 48
    realert_pct: float = 5.0

    # Absolute price targets. Unlike drop_pct and the all-time-low trigger,
    # these compare against a number you chose rather than against the fare's
    # own past, so they fire on the very first sighting of a date pair and do
    # not care about min_observations. That matters because most tracked pairs
    # never accumulate enough history to arm the relative triggers, and a fare
    # that is cheap every single time it is checked never "drops" at all.
    #
    # target_price applies to the priced (usually MAIN) fare. Leave unset to
    # disable. saver_target_price applies to the Saver rung on the same
    # search, which is typically ~100 USD below MAIN and earns no Atmos
    # points, so it is a separate knob with its own threshold.
    target_price: float | None = None
    saver_target_price: float | None = None

    # Cross-date ranking: alert when a fare is in the cheapest cheapest_pct of
    # the OTHER date pairs currently visible on the same route. This is the
    # third question the other triggers do not ask. Targets ask "is this under
    # a number I picked", the relative triggers ask "is this cheaper than it
    # used to be", and this asks "is this one of the cheapest dates I could
    # fly right now", which is what actually decides whether you book.
    #
    # It needs no history for the pair being priced, only a spread of other
    # pairs to rank against, so it works on a date's first sighting. Set to
    # None to disable.
    cheapest_pct: float | None = None
    cheapest_days: int = 30        # ignore observations staler than this
    cheapest_min_pairs: int = 8    # too few pairs and a percentile is noise

    # Points. award_floor_points is the saver starting price for this route's
    # distance band, read off Alaska's North America award chart. Leave it
    # unset to disable the redeem/pay analysis for the route.
    award_floor_points: int | None = None
    point_value_cents: float = 1.5
    redeem_above_cents: float = 2.0
    spike_pct: float | None = None

    # City slugs for alaskaair.com route pages. Derived from `name` when
    # unset: "Seattle to New Orleans" gives seattle / new-orleans.
    origin_city: str | None = None
    destination_city: str | None = None

    def cities(self) -> tuple[str, str] | None:
        if self.origin_city and self.destination_city:
            return self.origin_city, self.destination_city
        if " to " not in self.name:
            return None
        a, _, b = self.name.partition(" to ")
        def slug(t):
            return t.strip().lower().replace(" ", "-")
        return slug(a), slug(b)

    @property
    def key(self) -> str:
        return f"{self.origin}-{self.destination}"


@dataclass
class Config:
    routes: list[Route]
    daily_call_budget: int = 70
    monthly_call_quota: int = 2000
    runs_per_day: int = 1
    quota_reserve_pct: float = 95.0


def load(path: str | None = None) -> Config:
    with open(path or CONFIG_PATH) as fh:
        raw: dict[str, Any] = yaml.safe_load(fh)

    defaults = raw.get("defaults", {}) or {}
    known = set(Route.__dataclass_fields__)

    routes = []
    for entry in raw.get("routes", []) or []:
        merged = {**defaults, **entry}
        unknown = set(merged) - known
        if unknown:
            raise ValueError(f"unknown config keys for {entry.get('name')}: {sorted(unknown)}")
        routes.append(Route(**merged))

    if not routes:
        raise ValueError("no routes configured")

    return Config(
        routes=routes,
        daily_call_budget=int(raw.get("daily_call_budget", 70)),
        monthly_call_quota=int(raw.get("monthly_call_quota", 2000)),
        runs_per_day=int(raw.get("runs_per_day", 1)),
        quota_reserve_pct=float(raw.get("quota_reserve_pct", 95)),
    )
