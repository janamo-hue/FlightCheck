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
ARCHIVE_PATH = os.environ.get("ARCHIVE_FILE", "data/archive.jsonl")
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
    branded_fare: str | None = None
    saver_price: float | None = None

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


def archive(path: str | None = None, archive_path: str | None = None) -> tuple[int, int]:
    """Move observations for departed dates out of the live file.

    An earlier version deleted them. That threw away the most valuable thing
    this tool produces: the full price curve for a flight from 180 days out to
    departure, which is what answers "when does this route actually bottom
    out". Live history stays small for fast median lookups; the archive keeps
    the record.
    """
    path = path or HISTORY_PATH
    archive_path = archive_path or ARCHIVE_PATH
    if not os.path.exists(path):
        return 0, 0

    today = now().date().isoformat()
    rows = load_history(path)
    live = [o for o in rows if o.depart >= today]
    departed = [o for o in rows if o.depart < today]

    if departed:
        os.makedirs(os.path.dirname(archive_path) or ".", exist_ok=True)
        with open(archive_path, "a") as fh:
            for obs in departed:
                fh.write(json.dumps(asdict(obs), sort_keys=True) + "\n")

    with open(path, "w") as fh:
        for obs in live:
            fh.write(json.dumps(asdict(obs), sort_keys=True) + "\n")

    return len(live), len(departed)


def prune_alert_state(state: dict, keep_days: int = 30, asof: datetime | None = None) -> int:
    """Drop alert debounce records past any plausible debounce window.

    Without this the dict grows for the life of the repo, since every alert
    ever fired leaves an entry keyed by a date pair that eventually departs.
    """
    asof = asof or now()
    cutoff = (asof - timedelta(days=keep_days)).isoformat()
    alerts = state.get("alerts", {})
    stale = [k for k, v in alerts.items() if v.get("at", "") < cutoff]
    for key in stale:
        del alerts[key]
    return len(stale)


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


def quota(state: dict, asof: datetime | None = None) -> dict:
    """Rolling monthly API call counter, reset on month change."""
    asof = asof or now()
    month = asof.strftime("%Y-%m")
    q = state.setdefault("quota", {"month": month, "calls": 0})
    if q.get("month") != month:
        q.update(month=month, calls=0)
    return q


def spend_quota(state: dict, calls: int, asof: datetime | None = None) -> int:
    q = quota(state, asof)
    q["calls"] += calls
    return q["calls"]
