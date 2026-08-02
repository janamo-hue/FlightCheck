from datetime import date, datetime, timedelta, timezone

import pytest

from src.alerts import evaluate, record_alert
from src.config import Route
from src.planner import plan, should_sweep, sweep_tasks, watchlist_tasks
from src.store import Observation

TODAY = date(2026, 8, 2)  # a Sunday
NOW = datetime(2026, 8, 2, 14, 0, tzinfo=timezone.utc)


def route(**kw) -> Route:
    base = dict(
        name="SEA to SAN", origin="SEA", destination="SAN",
        trip_lengths=[7], sweep_stride_days=4, window_start_days=14,
        window_end_days=180, watchlist_size=3, min_observations=3,
    )
    base.update(kw)
    return Route(**base)


def obs(price, days_ago=0, depart="2026-09-01", ret="2026-09-08"):
    return Observation(
        route="SEA-SAN", depart=depart, ret=ret, price=price, currency="USD",
        flight_numbers=["AS1"], observed_at=(NOW - timedelta(days=days_ago)).isoformat(),
    )


# ------------------------------------------------------------------ planner

def test_sweep_respects_window_bounds():
    tasks = sweep_tasks(route(), TODAY)
    departs = sorted({t.depart for t in tasks})
    assert departs[0] == "2026-08-16"          # today + 14
    assert departs[-1] <= "2026-01-29".replace("2026-01", "2027-01")
    assert date.fromisoformat(departs[-1]) <= TODAY + timedelta(days=180)


def test_stride_controls_call_count():
    dense = len(sweep_tasks(route(sweep_stride_days=2), TODAY))
    sparse = len(sweep_tasks(route(sweep_stride_days=8), TODAY))
    assert dense > sparse * 3


def test_multiple_trip_lengths_multiply_pairs():
    one = len(sweep_tasks(route(trip_lengths=[7]), TODAY))
    two = len(sweep_tasks(route(trip_lengths=[7, 10]), TODAY))
    assert two == one * 2


def test_one_way_has_no_return():
    tasks = sweep_tasks(route(one_way=True), TODAY)
    assert all(t.ret is None for t in tasks)


def test_depart_weekday_filter():
    tasks = sweep_tasks(route(sweep_stride_days=1, depart_weekdays=[4]), TODAY)
    assert {date.fromisoformat(t.depart).weekday() for t in tasks} == {4}


def test_should_sweep_first_run_and_weekly():
    r = route(sweep_weekday=6)
    assert should_sweep(r, TODAY, {}) is True
    state = {"last_sweep": {"SEA-SAN": TODAY.isoformat()}}
    assert should_sweep(r, TODAY, state) is False
    assert should_sweep(r, TODAY + timedelta(days=7), state) is True


def test_stale_sweep_forces_refresh_off_schedule():
    state = {"last_sweep": {"SEA-SAN": (TODAY - timedelta(days=11)).isoformat()}}
    assert should_sweep(route(sweep_weekday=0), TODAY, state) is True


def test_watchlist_drops_pairs_that_left_the_window():
    state = {"watchlists": {"SEA-SAN": ["2026-08-03|2026-08-10", "2026-11-01|2026-11-08"]}}
    tasks = watchlist_tasks(route(), TODAY, state)
    assert [t.depart for t in tasks] == ["2026-11-01"]


def test_plan_truncates_to_budget_keeping_watchlist():
    r = route()
    state = {"watchlists": {"SEA-SAN": ["2026-11-01|2026-11-08"]}}
    tasks = plan([r], TODAY, state, budget=5)
    assert len(tasks) == 5
    assert tasks[0].tier == "watchlist"


def test_plan_deduplicates_overlap():
    r = route()
    state = {"watchlists": {"SEA-SAN": ["2026-08-16|2026-08-23"]}}
    tasks = plan([r], TODAY, state, budget=999)
    pairs = [t.pair for t in tasks]
    assert len(pairs) == len(set(pairs))


# ------------------------------------------------------------------- alerts

def test_no_alert_without_enough_history():
    prior = [obs(300, 3), obs(300, 2)]
    assert evaluate(route(), obs(200), prior, {}, NOW) is None


def test_percent_drop_fires():
    prior = [obs(400, 5), obs(400, 3), obs(380, 1)]
    alert = evaluate(route(drop_pct=15), obs(300), prior, {}, NOW)
    assert alert is not None
    assert alert.drop_pct == pytest.approx(25.0)


def test_small_drop_does_not_fire():
    prior = [obs(400, 5), obs(400, 3), obs(400, 1)]
    assert evaluate(route(drop_pct=15), obs(395), prior, {}, NOW) is None


def test_trivial_new_low_is_below_the_margin():
    prior = [obs(400, 5), obs(400, 3), obs(400, 1)]
    r = route(drop_pct=50, all_time_low_margin_pct=3)
    assert evaluate(r, obs(396), prior, {}, NOW) is None      # 1% under, ignored
    assert evaluate(r, obs(380), prior, {}, NOW) is not None  # 5% under, fires


def test_median_ignores_single_outlier():
    prior = [obs(400, 5), obs(400, 4), obs(120, 3), obs(400, 2)]
    # Mean would be 330 and hide this; median is 400 so a 340 fare is a real drop.
    assert evaluate(route(drop_pct=13, alert_on_all_time_low=False), obs(340), prior, {}, NOW)


def test_stale_observations_excluded_from_baseline():
    prior = [obs(900, 60), obs(300, 3), obs(300, 2), obs(300, 1)]
    assert evaluate(route(drop_pct=15, baseline_days=14, alert_on_all_time_low=False),
                    obs(400), prior, {}, NOW) is None


def test_all_time_low_fires_without_percent_trigger():
    prior = [obs(300, 3), obs(305, 2), obs(302, 1)]
    alert = evaluate(route(drop_pct=50), obs(280), prior, {}, NOW)
    assert alert is not None and alert.all_time_low


def test_debounce_suppresses_repeat():
    r = route(drop_pct=15)
    prior = [obs(400, 5), obs(400, 3), obs(400, 1)]
    state = {}
    first = evaluate(r, obs(300), prior, state, NOW)
    record_alert(first, state, NOW)
    later = NOW + timedelta(hours=6)
    assert evaluate(r, obs(299), prior, state, later) is None


def test_further_drop_breaks_debounce():
    r = route(drop_pct=15, realert_pct=5)
    prior = [obs(400, 5), obs(400, 3), obs(400, 1)]
    state = {}
    record_alert(evaluate(r, obs(300), prior, state, NOW), state, NOW)
    later = NOW + timedelta(hours=6)
    assert evaluate(r, obs(250), prior, state, later) is not None


def test_debounce_expires():
    r = route(drop_pct=15, debounce_hours=48)
    prior = [obs(400, 5), obs(400, 3), obs(400, 1)]
    state = {}
    record_alert(evaluate(r, obs(300), prior, state, NOW), state, NOW)
    assert evaluate(r, obs(300), prior, state, NOW + timedelta(hours=49)) is not None
