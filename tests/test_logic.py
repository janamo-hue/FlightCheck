import itertools
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise

import pytest

from src.alerts import evaluate, record_alert
from src.config import Route
from src.planner import (
    mark_priced as planner_mark,
)
from src.planner import (
    plan,
    should_sweep,
    sweep_tasks,
    watchlist_tasks,
)
from src.planner import (
    sweep_complete as planner_sweep_complete,
)
from src.store import Observation

TODAY = date(2026, 8, 2)  # a Sunday
NOW = datetime(2026, 8, 2, 14, 0, tzinfo=UTC)


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


# ------------------------------------------------- fixes: stride composition

def test_stride_applies_after_weekday_filter():
    # Every 2nd Friday across ~166 days is 12. Striding first and then testing
    # the weekday would give 12 only by luck; stride 4 would collapse to 6.
    assert len(sweep_tasks(route(sweep_stride_days=2, depart_weekdays=[4]), TODAY)) == 12
    assert len(sweep_tasks(route(sweep_stride_days=4, depart_weekdays=[4]), TODAY)) == 6
    every_friday = sweep_tasks(route(sweep_stride_days=1, depart_weekdays=[4]), TODAY)
    assert len(every_friday) == 24
    gaps = {
        (date.fromisoformat(b.depart) - date.fromisoformat(a.depart)).days
        for a, b in itertools.pairwise(every_friday)
    }
    assert gaps == {7}


def test_stride_unchanged_without_weekday_filter():
    tasks = sweep_tasks(route(sweep_stride_days=4), TODAY)
    departs = [date.fromisoformat(t.depart) for t in tasks]
    assert {(b - a).days for a, b in itertools.pairwise(departs)} == {4}


# ----------------------------------------------- fixes: resumable sweeps

def test_truncated_sweep_is_not_marked_complete():
    r = route()
    state = {}
    total = len(sweep_tasks(r, TODAY))
    plan([r], TODAY, state, budget=10)
    assert "SEA-SAN" not in state.get("last_sweep", {})
    assert len(state["sweep_cycles"]["SEA-SAN"]["pending"]) == total


def test_sweep_resumes_and_eventually_closes():
    r = route()
    state = {}
    total = len(sweep_tasks(r, TODAY))
    runs = 0
    while True:
        runs += 1
        tasks = plan([r], TODAY, state, budget=10)
        for t in tasks:
            planner_mark(r, t.pair, state)
        if planner_sweep_complete(r, TODAY, state):
            break
        assert runs < 50
    assert runs == -(-total // 10)                      # ceil division
    assert state["last_sweep"]["SEA-SAN"] == TODAY.isoformat()
    assert "SEA-SAN" not in state["sweep_cycles"]


def test_resumed_sweep_does_not_repeat_priced_pairs():
    r = route()
    state = {}
    first = plan([r], TODAY, state, budget=10)
    for t in first:
        planner_mark(r, t.pair, state)
    second = plan([r], TODAY, state, budget=10)
    assert not ({t.pair for t in first} & {t.pair for t in second})


def test_backlog_is_interleaved_so_no_route_starves():
    routes = [route(name="a"), route(name="b", destination="BOS"),
              route(name="c", destination="BCN")]
    tasks = plan(routes, TODAY, {}, budget=12)
    counts = {}
    for t in tasks:
        counts[t.route.key] = counts.get(t.route.key, 0) + 1
    assert len(counts) == 3 and max(counts.values()) - min(counts.values()) <= 1


def test_pairs_falling_out_of_window_are_dropped_from_backlog():
    r = route()
    state = {"sweep_cycles": {"SEA-SAN": {
        "started": TODAY.isoformat(),
        "pending": ["2026-08-03|2026-08-10", "2026-11-01|2026-11-08"]}}}
    tasks = plan([r], TODAY, state, budget=99)
    assert [t.depart for t in tasks if t.tier == "sweep"] == ["2026-11-01"]


# ------------------------------------------------------- fixes: booking link

def test_booking_url_is_google_flights_with_both_dates():
    from src.alerts import Alert
    from src.notify import booking_url
    a = Alert(kind="drop", route_name="x", route_key="SEA-BCN", depart="2026-10-01",
              ret="2026-10-11", price=500.0, currency="USD", baseline=600.0,
              drop_pct=16.7, all_time_low=False, flight_numbers=["AS1"],
              observations=5)
    url = booking_url(a)
    assert url.startswith("https://www.google.com/travel/flights?")
    assert "alaskaair" not in url
    for token in ("SEA", "BCN", "2026-10-01", "2026-10-11", "nonstop"):
        assert token.replace("-", "-") in url.replace("%2C", ",").replace("+", " ")


# ------------------------------------------------------------------- points

def points_route(**kw):
    base = dict(award_floor_points=12500, point_value_cents=1.5,
                redeem_above_cents=2.0, spike_pct=25, drop_pct=15,
                min_observations=3)
    base.update(kw)
    return route(**base)


def test_cents_per_point_uses_the_saver_floor():
    from src.alerts import cents_per_point
    # $300 against a 12,500 point floor is 2.4 cents per point.
    assert cents_per_point(300.0, points_route()) == pytest.approx(2.4)


def test_no_floor_configured_means_no_valuation():
    from src.alerts import cents_per_point
    assert cents_per_point(300.0, points_route(award_floor_points=None)) is None


def test_expensive_fare_triggers_a_redeem_alert():
    prior = [obs(200, 5), obs(210, 3), obs(205, 1)]
    alert = evaluate(points_route(), obs(320), prior, {}, NOW)
    assert alert is not None
    assert alert.kind == "spike"
    assert alert.verdict == "redeem points"
    assert alert.cents_per_point == pytest.approx(2.56)


def test_cheap_fare_on_an_expensive_floor_says_pay_cash():
    r = points_route(award_floor_points=25000)
    prior = [obs(400, 5), obs(400, 3), obs(400, 1)]
    alert = evaluate(r, obs(200), prior, {}, NOW)
    assert alert is not None
    assert alert.kind == "drop"
    assert alert.verdict == "pay cash"
    assert alert.cents_per_point == pytest.approx(0.8)


def test_redeem_needs_the_threshold_not_just_a_spike():
    # 1.2c per point is below redeem_above_cents even though the fare rose.
    r = points_route(award_floor_points=25000, spike_pct=None)
    prior = [obs(200, 5), obs(200, 3), obs(200, 1)]
    assert evaluate(r, obs(300), prior, {}, NOW) is None


def test_routes_without_a_floor_never_produce_spike_alerts():
    r = points_route(award_floor_points=None)
    prior = [obs(200, 5), obs(210, 3), obs(205, 1)]
    alert = evaluate(r, obs(400), prior, {}, NOW)
    assert alert is None



def test_drop_and_redeem_debounce_independently():
    r = points_route()
    state = {}
    prior = [obs(400, 5), obs(400, 3), obs(400, 1)]

    drop = evaluate(r, obs(250), prior, state, NOW)
    assert drop.kind == "drop"
    # Cheap AND good point value: both true, and the verdict says so.
    assert drop.verdict == "redeem points"
    record_alert(drop, state, NOW)

    # A spike an hour later is a different event and must still fire.
    later = NOW + timedelta(hours=1)
    spike = evaluate(r, obs(600), prior, state, later)
    assert spike is not None and spike.kind == "spike"


def test_spike_debounce_flips_direction():
    r = points_route()
    state = {}
    prior = [obs(200, 5), obs(200, 3), obs(200, 1)]

    first = evaluate(r, obs(300), prior, state, NOW)
    record_alert(first, state, NOW)
    later = NOW + timedelta(hours=2)

    # Barely higher: still the same signal, stay quiet.
    assert evaluate(r, obs(305), prior, state, later) is None
    # Meaningfully higher: the case for redeeming got stronger, so re-alert.
    assert evaluate(r, obs(400), prior, state, later) is not None


def test_saver_fare_is_flagged_as_earning_nothing():
    o = obs(200)
    o.branded_fare = "SAVER"
    prior = [obs(400, 5), obs(400, 3), obs(400, 1)]
    alert = evaluate(points_route(award_floor_points=25000), o, prior, {}, NOW)
    assert alert is not None
    assert alert.earns_points is False
    assert alert.branded_fare == "SAVER"


def test_unbranded_fare_is_assumed_to_earn():
    prior = [obs(400, 5), obs(400, 3), obs(400, 1)]
    alert = evaluate(points_route(award_floor_points=25000), obs(200), prior, {}, NOW)
    assert alert.earns_points is True


# --------------------------------------------------------------------- links

def make_alert(**kw):
    from src.alerts import Alert
    base = dict(kind="drop", route_name="Seattle to New Orleans",
                route_key="SEA-MSY", depart="2026-10-01", ret="2026-10-08",
                price=209.0, currency="USD", baseline=300.0, drop_pct=30.0,
                all_time_low=False, flight_numbers=["AS490"], observations=6,
                cities=("seattle", "new-orleans"))
    base.update(kw)
    return Alert(**base)


def test_alaska_url_uses_the_verified_route_page_pattern():
    from src.notify import alaska_url
    assert alaska_url(make_alert()) == (
        "https://www.alaskaair.com/en/flights-from-seattle-to-new-orleans")


def test_alaska_url_is_omitted_without_city_slugs():
    from src.notify import alaska_url
    assert alaska_url(make_alert(cities=None)) is None


def test_city_slugs_come_from_the_route_name():
    assert route(name="Seattle to New Orleans").cities() == ("seattle", "new-orleans")
    assert route(name="Seattle to Mexico City").cities() == ("seattle", "mexico-city")


def test_explicit_city_overrides_beat_the_derived_name():
    r = route(name="SEA run", origin_city="seattle", destination_city="orange-county")
    assert r.cities() == ("seattle", "orange-county")


def test_unparseable_route_name_yields_no_cities():
    assert route(name="SEA-MSY watcher").cities() is None


def test_email_contains_both_links_and_the_report_link(monkeypatch):
    import importlib

    from src import notify
    monkeypatch.setenv("REPORT_URL", "https://example.github.io/FlightCheck/")
    importlib.reload(notify)
    try:
        _, body = notify.render([make_alert()])
        assert "google.com/travel/flights" in body
        assert "alaskaair.com/en/flights-from-seattle-to-new-orleans" in body
        assert "https://example.github.io/FlightCheck/" in body
    finally:
        monkeypatch.delenv("REPORT_URL", raising=False)
        importlib.reload(notify)


def test_report_link_is_omitted_when_unset():
    from src.notify import render
    _, body = render([make_alert()])
    assert "Full price history" not in body


def test_dated_link_carries_both_dates():
    from src.notify import booking_url
    url = booking_url(make_alert())
    assert "2026-10-01" in url and "2026-10-08" in url


def test_one_way_dated_link_has_no_return():
    from src.notify import booking_url
    assert "through" not in booking_url(make_alert(ret=None))


def test_saver_premium_and_cost_per_point_earned():
    a = make_alert(price=268.0, saver_price=209.0, distance_miles=2083)
    assert a.saver_premium == pytest.approx(59.0)
    # $59 buys 2,083 points, so 2.8 cents each.
    assert a.cost_per_point_earned == pytest.approx(2.83, abs=0.01)


def test_no_saver_seen_means_no_premium():
    a = make_alert(saver_price=None)
    assert a.saver_premium is None and a.cost_per_point_earned is None


def test_missing_distance_blocks_the_per_point_maths_only():
    a = make_alert(price=268.0, saver_price=209.0, distance_miles=None)
    assert a.saver_premium == pytest.approx(59.0)
    assert a.cost_per_point_earned is None


def test_email_shows_the_saver_premium():
    from src.notify import render
    _, body = render([make_alert(price=268.0, saver_price=209.0,
                                 distance_miles=2083, branded_fare="MAIN")])
    assert "above the Saver" in body
    assert "2.8c per point earned" in body


# ---------------------------------------------------- calendar-anchored grid

def test_grid_is_anchored_to_the_calendar_not_the_run_date():
    """Consecutive weekly sweeps must revisit the same departures.

    The drifting version overlapped on 3 of ~18 dates between real sweeps,
    which left 125 of 133 pairs with one observation and no path to alerting.
    """
    r = route(near_stride_days=4, sweep_stride_days=12)
    a = {t.depart for t in sweep_tasks(r, TODAY)}
    b = {t.depart for t in sweep_tasks(r, TODAY + timedelta(days=7))}
    assert len(a & b) >= len(a) - 3          # only the window edges differ
    inside = {d for d in a if "2026-09-01" < d < "2026-12-01"}
    assert inside <= b                        # interior dates all revisited


def test_far_date_stays_on_grid_entering_the_near_tier():
    r = route(near_stride_days=4, sweep_stride_days=12, near_days=60)
    first = {t.depart for t in sweep_tasks(r, TODAY)}
    far = sorted(d for d in first
                 if date.fromisoformat(d) > TODAY + timedelta(days=60))
    probe = far[0]
    later = TODAY + timedelta(days=35)        # probe now inside the near tier
    assert date.fromisoformat(probe) <= later + timedelta(days=60)
    assert probe in {t.depart for t in sweep_tasks(r, later)}


def test_near_tier_is_denser_than_far():
    r = route(near_stride_days=4, sweep_stride_days=12, near_days=60)
    dates = [date.fromisoformat(t.depart) for t in sweep_tasks(r, TODAY)]
    near = [d for d in dates if d <= TODAY + timedelta(days=60)]
    far = [d for d in dates if d > TODAY + timedelta(days=60)]
    near_gap = min((b - a).days for a, b in pairwise(near))
    far_gap = min((b - a).days for a, b in pairwise(far))
    assert near_gap == 4 and far_gap == 12


def test_single_tier_when_near_stride_unset():
    dates = [date.fromisoformat(t.depart)
             for t in sweep_tasks(route(sweep_stride_days=4), TODAY)]
    assert {(b - a).days for a, b in pairwise(dates)} == {4}
    assert all(d.toordinal() % 4 == 0 for d in dates)


def test_weekday_grid_is_week_anchored():
    r = route(near_stride_days=None, sweep_stride_days=2, depart_weekdays=[4])
    a = {t.depart for t in sweep_tasks(r, TODAY)}
    b = {t.depart for t in sweep_tasks(r, TODAY + timedelta(days=7))}
    assert all(date.fromisoformat(d).weekday() == 4 for d in a)
    inside = {d for d in a if "2026-09-01" < d < "2026-12-01"}
    assert inside <= b


# --- absolute price targets -------------------------------------------------
#
# The point of these is that they work where the relative triggers cannot:
# with no history at all, and on a fare that is cheap on every observation and
# therefore never "drops".


def _obs_with_saver(price, saver, days_ago=0):
    o = obs(price, days_ago=days_ago)
    o.saver_price = saver
    return o


def test_target_fires_with_no_history_at_all():
    r = route(target_price=400)
    a = evaluate(r, _obs_with_saver(380, 300), [], {}, NOW)
    assert a is not None
    assert a.kind == "target"
    assert a.target_fare == "MAIN"
    assert a.target_price == 400
    assert a.alert_price == 380
    assert a.observations == 0


def test_target_ignores_min_observations():
    """min_observations gates the relative triggers; it must not gate this."""
    r = route(target_price=400, min_observations=99)
    assert evaluate(r, _obs_with_saver(380, 300), [obs(380, 1)], {}, NOW) is not None


def test_no_target_when_price_above_it():
    r = route(target_price=350)
    assert evaluate(r, _obs_with_saver(380, 300), [], {}, NOW) is None


def test_target_fires_on_a_perfectly_flat_fare():
    """The case the relative triggers are blind to: median == current == floor."""
    r = route(target_price=400, drop_pct=15)
    prior = [obs(397, days_ago=d) for d in range(1, 15)]
    a = evaluate(r, _obs_with_saver(397, 297), prior, {}, NOW)
    assert a is not None and a.kind == "target"
    assert a.drop_pct == 0  # nothing moved, which is exactly the point


def test_saver_target_fires_on_the_saver_rung():
    r = route(saver_target_price=310)
    a = evaluate(r, _obs_with_saver(500, 300), [], {}, NOW)
    assert a is not None
    assert a.kind == "target-saver"
    assert a.target_fare == "SAVER"
    assert a.alert_price == 300      # the Saver fare
    assert a.price == 500            # MAIN is still carried for context


def test_saver_wins_when_both_rungs_beat_their_targets():
    r = route(target_price=600, saver_target_price=310)
    a = evaluate(r, _obs_with_saver(500, 300), [], {}, NOW)
    assert a.target_fare == "SAVER"


def test_saver_target_ignored_when_no_saver_price_recorded():
    r = route(saver_target_price=310)
    assert evaluate(r, _obs_with_saver(500, None), [], {}, NOW) is None


def test_target_takes_precedence_over_a_drop():
    r = route(target_price=400, drop_pct=10, min_observations=3)
    prior = [obs(600, days_ago=d) for d in range(1, 6)]
    a = evaluate(r, _obs_with_saver(380, 300), prior, {}, NOW)
    assert a.kind == "target"
    assert a.drop_pct > 10          # the drop is still reported, just not the label


def test_target_debounces_and_rearms_on_a_further_fall():
    r = route(target_price=400, debounce_hours=48, realert_pct=5)
    state = {}
    first = evaluate(r, _obs_with_saver(380, 300), [], state, NOW)
    record_alert(first, state, NOW)

    same = evaluate(r, _obs_with_saver(379, 300), [], state, NOW + timedelta(hours=1))
    assert same is None, "a penny lower should not re-alert inside the window"

    lower = evaluate(r, _obs_with_saver(340, 300), [], state, NOW + timedelta(hours=1))
    assert lower is not None, "a further fall past realert_pct should re-alert"


def test_main_and_saver_targets_debounce_independently():
    r = route(target_price=400, saver_target_price=310)
    state = {}
    saver_hit = evaluate(r, _obs_with_saver(500, 300), [], state, NOW)
    assert saver_hit.kind == "target-saver"
    record_alert(saver_hit, state, NOW)

    # Saver is debounced, so MAIN coming into range should still speak.
    main_hit = evaluate(r, _obs_with_saver(380, 300), [], state, NOW + timedelta(hours=1))
    assert main_hit is not None
    assert main_hit.kind == "target"


def test_targets_unset_leaves_behaviour_unchanged():
    r = route()
    assert evaluate(r, _obs_with_saver(1, 1), [], {}, NOW) is None
