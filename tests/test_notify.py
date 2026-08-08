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
