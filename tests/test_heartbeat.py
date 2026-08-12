"""The heartbeat must summarise activity and never crash on sparse data."""

from datetime import timedelta

from src import heartbeat, store


def test_build_with_no_data_warns(monkeypatch):
    monkeypatch.setattr(store, "load_state", lambda: {"runs": [], "quota": {"calls": 0}})
    monkeypatch.setattr(store, "load_history", lambda: [])
    subject, body = heartbeat.build(7)
    assert "0 scans" in subject
    assert "No scans ran" in body            # health warning present


def test_build_counts_runs_and_emails(monkeypatch):
    now = store.now()
    recent = (now - timedelta(days=1)).isoformat()
    old = (now - timedelta(days=30)).isoformat()
    state = {"quota": {"calls": 42}, "runs": [
        {"at": recent, "calls": 10, "observations": 6, "alerts": 2, "emailed": True},
        {"at": recent, "calls": 8, "observations": 4, "alerts": 0, "emailed": False},
        {"at": old, "calls": 99, "observations": 99, "alerts": 9, "emailed": True},
    ]}
    obs = [store.Observation(route="SEA-ABQ", depart="2026-09-01", ret="2026-09-08",
                             price=300.0, currency="USD", flight_numbers=["AS1"],
                             observed_at=recent, branded_fare="MAIN")]
    monkeypatch.setattr(store, "load_state", lambda: state)
    monkeypatch.setattr(store, "load_history", lambda: obs)

    subject, body = heartbeat.build(7)
    # only the two recent runs count, not the 30-day-old one
    assert "2 scans" in subject
    assert "1 alert emails" in subject       # one run emailed
    assert "10 prices" in subject            # 6 + 4 observations
    assert "SEA-ABQ" in body and "low USD 300" in body
