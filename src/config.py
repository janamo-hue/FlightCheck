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
    window_start_days: int = 14
    window_end_days: int = 180
    sweep_stride_days: int = 4
    sweep_weekday: int = 6
    depart_weekdays: list[int] = field(default_factory=list)
    watchlist_size: int = 8
    trip_lengths: list[int] = field(default_factory=lambda: [7])
    one_way: bool = False
    drop_pct: float = 15.0
    baseline_days: int = 14
    min_observations: int = 3
    alert_on_all_time_low: bool = True
    all_time_low_margin_pct: float = 3.0
    debounce_hours: int = 48
    realert_pct: float = 5.0

    @property
    def key(self) -> str:
        return f"{self.origin}-{self.destination}"


@dataclass
class Config:
    routes: list[Route]
    daily_call_budget: int = 70


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

    return Config(routes=routes, daily_call_budget=int(raw.get("daily_call_budget", 70)))
