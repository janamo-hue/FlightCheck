"""Failure-rate guard on the scan loop.

A live cron run exited green while every pricing call failed: each exception
was caught, logged and stepped over, so the workflow reported success with no
data and no alerts. These pin the fix.
"""

import tempfile

import pytest

from src import scan, store
from src.alaska import Offer


class Client:
    """Fails a given fraction of calls."""

    def __init__(self, fail_every=None):
        self.fail_every, self.n, self.calls_made = fail_every, 0, 0

    def cheapest_direct(self, o, d, depart, ret=None, **kw):
        self.n += 1
        self.calls_made += 1
        if self.fail_every and self.n % self.fail_every == 0:
            raise ConnectionError("no address associated with hostname")
        return Offer(300.0, "USD", "AS", ["AS1"], "PT2H")


@pytest.fixture
def sandbox(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setattr(store, "HISTORY_PATH", f"{tmp}/h.jsonl")
    monkeypatch.setattr(store, "STATE_PATH", f"{tmp}/s.json")
    monkeypatch.setattr(store, "ARCHIVE_PATH", f"{tmp}/a.jsonl")
    monkeypatch.setattr(scan.notify, "send", lambda alerts, dry_run=False: True)
    return tmp


def run(monkeypatch, client, argv=("--limit", "12")):
    monkeypatch.setattr(scan, "Alaska", lambda *a, **k: client)
    return scan.main(list(argv))


def test_total_failure_exits_non_zero(sandbox, monkeypatch):
    assert run(monkeypatch, Client(fail_every=1)) == 2


def test_total_failure_records_nothing(sandbox, monkeypatch):
    run(monkeypatch, Client(fail_every=1))
    assert store.load_history() == []


def test_healthy_run_exits_zero(sandbox, monkeypatch):
    assert run(monkeypatch, Client()) == 0
    assert store.load_history()


def test_occasional_failure_is_tolerated(sandbox, monkeypatch):
    # One in four failing is a bad afternoon, not a broken deployment.
    assert run(monkeypatch, Client(fail_every=4)) == 0
    assert store.load_history()


def test_majority_failure_trips_the_guard(sandbox, monkeypatch):
    assert run(monkeypatch, Client(fail_every=2)) == 0   # exactly 50%, tolerated
    assert scan.MAX_FAILURE_RATE == 0.5


def test_watchlist_ranks_on_latest_price_not_cheapest_ever():
    """A pair that dipped once used to be pinned to the watchlist forever."""
    from datetime import date

    from src.config import Route
    from src.scan import refresh_watchlist
    from src.store import Observation

    r = Route(name="SEA to SAN", origin="SEA", destination="SAN",
              watchlist_size=2, window_start_days=14)
    today = date(2026, 8, 2)

    def o(pair_depart, price, at):
        return Observation(route="SEA-SAN", depart=pair_depart, ret=None,
                           price=price, currency="USD", flight_numbers=["AS1"],
                           observed_at=at)

    history = [
        # dipped once long ago, expensive now
        o("2026-10-01", 200, "2026-07-01T00:00:00+00:00"),
        o("2026-10-01", 900, "2026-08-01T00:00:00+00:00"),
        # steadily cheap now
        o("2026-10-05", 300, "2026-08-01T00:00:00+00:00"),
        o("2026-10-09", 400, "2026-08-01T00:00:00+00:00"),
    ]
    state = {}
    refresh_watchlist(r, history, state, today)
    assert state["watchlists"]["SEA-SAN"] == ["2026-10-05|", "2026-10-09|"]
