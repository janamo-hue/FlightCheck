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


def test_constant_gap_is_flagged():
    # The real signature: $100 fixed while the fare ranges over $235.
    rows = spread("SEA-ABQ", 100, [458, 518, 538, 553, 638, 693])
    findings = audit.constant_gap(rows)
    assert len(findings) == 1
    assert "exactly 100 above Saver" in findings[0]


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


def test_duplicate_flight_numbers_are_flagged():
    rows = [obs("SEA-ABQ", 458, 358, flights=("AS2209", "AS2209", "AS2208", "AS2208"))]
    assert len(audit.duplicate_flights(rows)) == 1


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
    store.append(spread("SEA-ABQ", 100, [458, 518, 538, 553, 638, 693]), str(path))
    assert audit.main(["--history", str(path)]) == 1


def test_main_exits_zero_on_clean_history(tmp_path):
    path = tmp_path / "h.jsonl"
    rows = [obs("SEA-ABQ", p, s, flights=("AS1",), ret=None,
                ladder={"SAVER": s, "MAIN": p})
            for p, s in [(458, 358), (518, 402), (538, 441)]]
    store.append(rows, str(path))
    assert audit.main(["--history", str(path)]) == 0


def test_quarantined_capture_still_reproduces_the_finding():
    rows = store.load_history("data/quarantine/2026-08-04-mislabelled-columns.jsonl")
    findings = audit.constant_gap(rows)
    assert {"SEA-ABQ", "SEA-MSY"} == {f.split(":")[0] for f in findings}
