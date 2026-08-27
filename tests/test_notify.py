"""Digest subject-line selection in notify.render().

The subject is the most visible output and the field most likely to break
silently when Alert shape shifts, yet the existing suite only checks the body.
"""

from src.alerts import Alert
from src.notify import render


def alert(**kw):
    base = dict(kind="drop", route_name="Seattle to Albuquerque", route_key="SEA-ABQ",
                depart="2026-10-17", ret="2026-10-24", price=300.0, currency="USD",
                baseline=400.0, drop_pct=25.0, all_time_low=False,
                flight_numbers=["AS327"], observations=5)
    base.update(kw)
    return Alert(**base)


def test_drop_subject_leads_with_percent_off():
    subject, _ = render([alert(kind="drop", drop_pct=25.0)])
    assert subject.startswith("Fare drop:")
    assert "% off" in subject


def test_new_low_subject_when_no_percent_drop():
    subject, _ = render([alert(kind="low", drop_pct=None, all_time_low=True)])
    assert subject.startswith("New low:")


def test_spike_subject_leads_with_points():
    subject, _ = render([alert(kind="spike", drop_pct=None, spike_pct=30.0,
                               cents_per_point=2.5)])
    assert subject.startswith("Worth points:")
    assert "c/pt" in subject


def test_subject_counts_the_extra_alerts():
    subject, _ = render([alert(), alert(route_name="Seattle to New Orleans",
                                        route_key="SEA-MSY")])
    assert "+1 more" in subject


def test_headline_is_the_biggest_drop():
    small = alert(route_name="Seattle to Albuquerque", drop_pct=16.0)
    big = alert(route_name="Seattle to New Orleans", route_key="SEA-MSY", drop_pct=40.0)
    subject, _ = render([small, big])
    assert "Seattle to New Orleans" in subject


def _target_alert(**kw):
    base = dict(
        kind="target", route_name="SEA to ABQ", route_key="SEA-ABQ",
        depart="2026-10-17", ret="2026-10-24", price=380.0, currency="USD",
        baseline=None, drop_pct=None, all_time_low=False,
        flight_numbers=["AS327"], observations=0,
        target_fare="MAIN", target_price=400.0, trigger_price=380.0,
    )
    base.update(kw)
    return Alert(**base)


def test_target_subject_leads_with_the_target():
    subject, body = render([_target_alert()])
    assert subject.startswith("Under target:")
    assert "380" in subject and "400" in subject
    assert "20 under" in body


def test_saver_target_subject_and_row_use_the_saver_fare():
    a = _target_alert(kind="target-saver", target_fare="SAVER",
                      target_price=310.0, trigger_price=300.0,
                      price=500.0, saver_price=300.0)
    subject, body = render([a])
    assert "Saver" in subject
    assert "300" in subject           # the Saver fare leads
    assert "MAIN USD 500" in body     # MAIN kept as context
    assert "Saver earns no Atmos points" in body


def test_target_rows_sort_above_other_kinds():
    drop = _target_alert(kind="drop", route_name="SEA to MSY", target_fare=None,
                         target_price=None, trigger_price=None,
                         baseline=600.0, drop_pct=20.0, observations=5)
    _, body = render([drop, _target_alert()])
    assert body.index("SEA to ABQ") < body.index("SEA to MSY")


def test_first_sighting_is_called_out():
    _, body = render([_target_alert(observations=0)])
    assert "first time this date pair has been priced" in body
    _, body2 = render([_target_alert(observations=9)])
    assert "first time this date pair has been priced" not in body2


def test_cheapest_subject_and_row():
    a = Alert(
        kind="cheapest", route_name="SEA to MSY", route_key="SEA-MSY",
        depart="2027-01-05", ret="2027-01-12", price=487.0, currency="USD",
        baseline=None, drop_pct=None, all_time_low=False,
        flight_numbers=["AS1"], observations=0,
        cheapest_pct=10.0, cheapest_cutoff=497.0, market_pairs=73,
        market_median=537.0,
    )
    subject, body = render([a])
    assert subject.startswith("Cheapest dates:")
    assert "2027-01-05" in subject
    assert "cheapest 10% of 73 other dates" in body
    assert "route median right now USD 537" in body


def test_target_rows_still_outrank_cheapest_rows():
    cheap = Alert(
        kind="cheapest", route_name="SEA to MSY", route_key="SEA-MSY",
        depart="2027-01-05", ret="2027-01-12", price=487.0, currency="USD",
        baseline=None, drop_pct=None, all_time_low=False,
        flight_numbers=["AS1"], observations=0,
        cheapest_pct=10.0, cheapest_cutoff=497.0, market_pairs=73,
    )
    _, body = render([cheap, _target_alert()])
    assert body.index("SEA to ABQ") < body.index("SEA to MSY")
