"""Append-only JSONL price history, committed back to the repo by CI.

Chosen over a database so there is zero infrastructure and git diffs stay
readable. At a few routes checked daily this stays well under a megabyte
per year. Swap in Supabase later by reimplementing observations()/append().
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

HISTORY_PATH = os.environ.get("HISTORY_FILE", "data/history.jsonl")
STATE_PATH = os.environ.get("STATE_FILE", "data/state.json")


@dataclass
class Observation:
    route: str
    depart: str
    ret: str | None
    price: float
    currency: str
    flight_numbers: list[str]
    observed_at: str

    @property
    def pair(self) -> str:
        return f"{self.depart}|{self.ret or ''}"


def now() -> datetime:
    return datetime.now(timezone.utc)


def load_history(path: str | None = None) -> list[Observation]:
    path = path or HISTORY_PATH
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            out.append(Observation(**json.loads(line)))
    return out


def append(observations: list[Observation], path: str | None = None) -> None:
    path = path or HISTORY_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a") as fh:
        for obs in observations:
            fh.write(json.dumps(asdict(obs), sort_keys=True) + "\n")


def prune(path: str | None = None, keep_days: int = 400) -> int:
    """Drop observations older than keep_days and departures already in the past."""
    path = path or HISTORY_PATH
    if not os.path.exists(path):
        return 0
    cutoff = (now() - timedelta(days=keep_days)).isoformat()
    today = now().date().isoformat()

    kept = [
        o
        for o in load_history(path)
        if o.observed_at >= cutoff and o.depart >= today
    ]
    with open(path, "w") as fh:
        for obs in kept:
            fh.write(json.dumps(asdict(obs), sort_keys=True) + "\n")
    return len(kept)


def index(history: list[Observation]) -> dict[tuple[str, str], list[Observation]]:
    """Group observations by (route, date pair), oldest first."""
    buckets: dict[tuple[str, str], list[Observation]] = {}
    for obs in history:
        buckets.setdefault((obs.route, obs.pair), []).append(obs)
    for rows in buckets.values():
        rows.sort(key=lambda o: o.observed_at)
    return buckets


def load_state(path: str | None = None) -> dict:
    path = path or STATE_PATH
    if not os.path.exists(path):
        return {"last_sweep": {}, "watchlists": {}, "alerts": {}}
    with open(path) as fh:
        return json.load(fh)


def save_state(state: dict, path: str | None = None) -> None:
    path = path or STATE_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")
