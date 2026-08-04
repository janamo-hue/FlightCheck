"""Regression tests for the audit that would have caught the column mislabel."""

from src import audit, store


def obs(route, price, saver, depart="2026-10-17", ret="2026-10-24",
        flights=("AS1", "AS2"), ladder=None):
    return store.Observation(
        route=route, depart=depart, ret=ret, price=price, currency="USD",
        flight_numbers=list(flights), observed_at="2026-08-04T00:00:00+00:00",
        saver_price=saver, fare_ladder=ladder)


def spread(route, gap, prices):
    return [obs(route, p, p - gap, depart=f"2026-10-{d:02d}", ret=None,
                flights=("AS1",))
            for d, p in enumerate(prices, start=1)]


def ladder(price, saver, prem_gap):
    return {"SAVER": saver, "MAIN": price, "PREMIUM": price + prem_gap}


def test_frozen_ladder_is_flagged():
    # Every gap constant while the fare moves: no column shifts relative to
    # any other, so a systematic offset beats that many coincidences.
    rows = [obs("SEA-ABQ", p, p - 100, depart=f"2026-10-{d:02d}", ret=None,
                flights=("AS1",), ladder=ladder(p, p - 100, 70))
            for d, p in enumerate([458, 518, 538, 553, 638, 693], start=1)]
    assert len(audit.constant_gap(rows)) == 1


def test_real_fixed_saver_discount_is_not_flagged():
    # Alaska genuinely prices Saver a flat $50/leg below Main on SEA-ABQ. The
    # varying Main-to-Premium gap across the same reads corroborates that the
    # columns are aligned, so the constant Saver gap is pricing, not a bug.
    prem = [70, 65, 95, 120, 74, 70]
    rows = [obs("SEA-ABQ", p, p - 100, depart=f"2026-10-{d:02d}", ret=None,
                flights=("AS1",), ladder=ladder(p, p - 100, g))
            for d, (p, g) in enumerate(
                zip([458, 518, 538, 553, 638, 693], prem, strict=True), start=1)]
    assert audit.constant_gap(rows) == []


def test_varying_gap_is_not_flagged():
    rows = [obs("SEA-ABQ", p, s) for p, s in
            [(458, 358), (518, 402), (538, 441), (553, 470), (638, 519)]]
    assert audit.constant_gap(rows) == []


def test_constant_gap_needs_a_sample():
    assert audit.constant_gap(spread("SEA-ABQ", 100, [458, 518])) == []


def test_constant_gap_on_a_flat_fare_is_not_flagged():
    # If the fare never moved, a constant gap proves nothing.
    rows = spread("SEA-ABQ", 100, [458, 458, 458, 458, 458, 458])
    assert audit.constant_gap(rows) == []


def test_implausible_price_is_flagged():
    findings = audit.implausible_prices([obs("SEA-MEX", 14678, None, flights=())])
    assert len(findings) == 1 and "14,678" in findings[0]


def test_normal_price_is_not_flagged():
    assert audit.implausible_prices([obs("SEA-ABQ", 458, 358)]) == []


def test_flight_number_beyond_the_leg_count_is_flagged():
    rows = [obs("SEA-ABQ", 458, 358,
                flights=("AS2209", "AS2209", "AS2209", "AS2208"))]
    assert len(audit.duplicate_flights(rows)) == 1


def test_same_flight_number_on_both_legs_is_legitimate():
    # AS331 out and AS331 back is a real rotation, not a double read.
    assert audit.duplicate_flights([obs("SEA-ABQ", 458, 358,
                                        flights=("AS331", "AS331"))]) == []


def test_deduplicated_flights_are_clean():
    assert audit.duplicate_flights([obs("SEA-ABQ", 458, 358)]) == []


def test_too_many_legs_for_a_round_trip_is_flagged():
    rows = [obs("SEA-ABQ", 458, 358, flights=("AS1", "AS2", "AS3"))]
    assert len(audit.leg_count(rows)) == 1


def test_two_legs_for_a_round_trip_is_clean():
    assert audit.leg_count([obs("SEA-ABQ", 458, 358)]) == []


def test_missing_ladder_is_flagged():
    assert len(audit.missing_ladder([obs("SEA-ABQ", 458, 358)])) == 1


def test_present_ladder_is_clean():
    rows = [obs("SEA-ABQ", 458, 358, ladder={"SAVER": 308.0, "MAIN": 358.0})]
    assert audit.missing_ladder(rows) == []


def test_main_exits_non_zero_on_findings(tmp_path):
    path = tmp_path / "h.jsonl"
    store.append([obs("SEA-MEX", 14678, None, flights=())], str(path))
    assert audit.main(["--history", str(path)]) == 1


def test_missing_ladder_alone_does_not_fail_the_build(tmp_path):
    # Observations predating the ladder are history, not a defect.
    path = tmp_path / "h.jsonl"
    store.append([obs("SEA-ABQ", 458, 358, flights=("AS1", "AS2"))], str(path))
    assert audit.main(["--history", str(path)]) == 0


def test_main_exits_zero_on_clean_history(tmp_path):
    path = tmp_path / "h.jsonl"
    rows = [obs("SEA-ABQ", p, s, flights=("AS1",), ret=None,
                ladder={"SAVER": s, "MAIN": p})
            for p, s in [(458, 358), (518, 402), (538, 441)]]
    store.append(rows, str(path))
    assert audit.main(["--history", str(path)]) == 0


def test_real_history_is_clean():
    """The committed capture is correct data and must not trip the audit."""
    rows = store.load_history("data/history.jsonl")
    for name, check in audit.CHECKS:
        assert check(rows) == [], f"{name} fired on real data"
