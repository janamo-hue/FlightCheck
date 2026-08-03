from datetime import UTC, datetime, timedelta

from src import store
from src.alerts import evaluate
from src.config import Route
from src.store import Observation

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


def route(**kw) -> Route:
    base = dict(name="t", origin="SEA", destination="ABQ", min_observations=3)
    base.update(kw)
    return Route(**base)


def obs(price, days_ago=0, depart="2026-09-01"):
    return Observation(
        route="SEA-ABQ", depart=depart, ret=None, price=price, currency="USD",
        flight_numbers=["AS1"], observed_at=(NOW - timedelta(days=days_ago)).isoformat(),
    )


# --------------------------------------------------------------- atl horizon

def test_stale_low_no_longer_mutes_the_all_time_low_trigger():
    r = route(drop_pct=99, atl_days=90, all_time_low_margin_pct=3)
    prior = [obs(150, 200), obs(400, 5), obs(400, 3), obs(400, 1)]
    # 150 is 200 days old. Inside a 90-day horizon the low is 400, so 350 wins.
    assert evaluate(r, obs(350), prior, {}, NOW) is not None


def test_low_inside_the_horizon_still_mutes():
    r = route(drop_pct=99, atl_days=90, all_time_low_margin_pct=3)
    prior = [obs(150, 30), obs(400, 5), obs(400, 3), obs(400, 1)]
    assert evaluate(r, obs(350), prior, {}, NOW) is None


def test_atl_needs_min_observations_inside_the_horizon():
    r = route(drop_pct=99, atl_days=10, min_observations=3)
    prior = [obs(400, 60), obs(400, 50), obs(400, 40), obs(400, 2)]
    # Only one observation is inside 10 days, so the trigger stays quiet.
    assert evaluate(r, obs(200), prior, {}, NOW) is None


# ------------------------------------------------------------------- archive

def test_archive_moves_departed_rows_and_keeps_them(tmp_path):
    live_path, arch_path = tmp_path / "h.jsonl", tmp_path / "a.jsonl"
    today = store.now().date()
    rows = [
        obs(300, depart=(today + timedelta(days=30)).isoformat()),
        obs(310, depart=(today + timedelta(days=60)).isoformat()),
        obs(280, depart=(today - timedelta(days=1)).isoformat()),
        obs(290, depart=(today - timedelta(days=90)).isoformat()),
    ]
    store.append(rows, str(live_path))

    kept, moved = store.archive(str(live_path), str(arch_path))
    assert (kept, moved) == (2, 2)

    assert len(store.load_history(str(live_path))) == 2
    archived = store.load_history(str(arch_path))
    assert sorted(o.price for o in archived) == [280.0, 290.0]


def test_archive_appends_rather_than_overwriting(tmp_path):
    live_path, arch_path = tmp_path / "h.jsonl", tmp_path / "a.jsonl"
    past = (store.now().date() - timedelta(days=5)).isoformat()

    store.append([obs(100, depart=past)], str(live_path))
    store.archive(str(live_path), str(arch_path))
    store.append([obs(200, depart=past)], str(live_path))
    store.archive(str(live_path), str(arch_path))

    assert len(store.load_history(str(arch_path))) == 2


def test_archive_on_missing_file_is_a_noop(tmp_path):
    assert store.archive(str(tmp_path / "nope.jsonl"), str(tmp_path / "a.jsonl")) == (0, 0)


# --------------------------------------------------------------------- quota

def test_quota_accumulates_within_a_month():
    state: dict = {}
    store.spend_quota(state, 40, NOW)
    store.spend_quota(state, 35, NOW + timedelta(days=3))
    assert state["quota"] == {"month": "2026-08", "calls": 75}


def test_quota_resets_on_month_boundary():
    state: dict = {}
    store.spend_quota(state, 1900, NOW)
    store.spend_quota(state, 10, NOW + timedelta(days=40))
    assert state["quota"]["month"] == "2026-09"
    assert state["quota"]["calls"] == 10


# ------------------------------------------------------- alert state pruning

def test_stale_alert_records_are_dropped():
    state = {"alerts": {
        "SEA-ABQ|2026-09-01|": {"at": (NOW - timedelta(days=60)).isoformat(), "price": 300},
        "SEA-MSY|2026-09-02|": {"at": (NOW - timedelta(days=2)).isoformat(), "price": 250},
    }}
    dropped = store.prune_alert_state(state, keep_days=30, asof=NOW)
    assert dropped == 1
    assert list(state["alerts"]) == ["SEA-MSY|2026-09-02|"]


def test_pruning_empty_alert_state_is_safe():
    assert store.prune_alert_state({}, asof=NOW) == 0


# -------------------------------------------------------------------- report

def test_report_renders_with_history(tmp_path, monkeypatch):
    from src import report
    monkeypatch.setattr(store, "HISTORY_PATH", str(tmp_path / "h.jsonl"))
    monkeypatch.setattr(store, "STATE_PATH", str(tmp_path / "s.json"))
    store.append([obs(300, 5), obs(280, 3), obs(295, 1)], str(tmp_path / "h.jsonl"))

    page = report.build(days=60)
    assert "<svg" in page and "Seattle to Albuquerque" in page
    assert "280" in page


def test_report_handles_a_route_with_no_history(tmp_path, monkeypatch):
    from src import report
    monkeypatch.setattr(store, "HISTORY_PATH", str(tmp_path / "empty.jsonl"))
    monkeypatch.setattr(store, "STATE_PATH", str(tmp_path / "s.json"))
    page = report.build(days=60)
    assert "No observations" in page
