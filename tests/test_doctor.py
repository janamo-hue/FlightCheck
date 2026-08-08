
from src import doctor
from src.alaska import Offer
from src.config import Config, Route


def route(**kw) -> Route:
    base = dict(name="SEA to ABQ", origin="SEA", destination="ABQ",
                trip_lengths=[7], award_floor_points=7500)
    base.update(kw)
    return Route(**base)


def cfg(*routes) -> Config:
    return Config(routes=list(routes) or [route()], monthly_call_quota=2000)


class FakeClient:
    """Returns offers only for departures in `serves`."""

    def __init__(self, serves=None, offer=None, raises=None):
        self.serves, self.raises = serves, raises
        self.offer = offer or Offer(300.0, "USD", "AS", ["AS1234"], "PT2H")
        self.calls_made = 0

    def cheapest_direct(self, o, d, depart, ret=None, **kw):
        self.calls_made += 1
        if self.raises:
            raise self.raises
        if self.serves is not None and depart not in self.serves:
            return None
        return self.offer


def statuses(rep, needle):
    return [s for s, check, _ in rep.rows if needle in check]


# ---------------------------------------------------------------- probe spread

def test_probes_are_spread_across_the_window_not_clustered():
    from datetime import date
    r = route(window_start_days=14, window_end_days=180)
    dates = doctor.probe_dates(r, date(2026, 8, 2), 3)
    offsets = [(date.fromisoformat(d) - date(2026, 8, 2)).days for d, _ in dates]
    assert offsets == [55, 97, 138]
    assert all(14 <= o <= 180 for o in offsets)


def test_probe_returns_are_offset_by_trip_length():
    from datetime import date
    depart, ret = doctor.probe_dates(route(trip_lengths=[7]), date(2026, 8, 2), 1)[0]
    assert (date.fromisoformat(ret) - date.fromisoformat(depart)).days == 7


def test_one_way_probes_have_no_return():
    from datetime import date
    assert doctor.probe_dates(route(one_way=True), date(2026, 8, 2), 2)[0][1] is None


# -------------------------------------------------------- dead route detection

def test_route_with_no_service_fails_loudly():
    rep = doctor.Report()
    doctor.check_live(rep, cfg(), FakeClient(serves=set()), probes=3)
    assert statuses(rep, "service exists") == [doctor.FAIL]
    assert rep.failed == 1


def test_fully_served_route_passes():
    rep = doctor.Report()
    doctor.check_live(rep, cfg(), FakeClient(), probes=3)
    assert statuses(rep, "service exists") == [doctor.OK]
    assert rep.failed == 0


def test_partially_served_route_warns_rather_than_fails():
    from datetime import date
    r = route()
    served = {doctor.probe_dates(r, date.today(), 3)[0][0]}
    rep = doctor.Report()
    doctor.check_live(rep, cfg(r), FakeClient(serves=served), probes=3)
    assert statuses(rep, "service exists") == [doctor.WARN]
    assert rep.failed == 0


def test_regional_operating_carrier_is_flagged():
    # The grid marks every card AS-marketed; the operating carrier shows up in
    # the flight-number prefix. QX (Horizon) flying as AS should be surfaced.
    rep = doctor.Report()
    client = FakeClient(offer=Offer(300.0, "USD", "AS", ["QX2401"], "PT2H"))
    doctor.check_live(rep, cfg(), client, probes=1)
    assert doctor.WARN in statuses(rep, "operating carrier")


def test_points_check_reports_cents_per_point():
    rep = doctor.Report()
    doctor.check_live(rep, cfg(route(award_floor_points=12500)), FakeClient(), probes=1)
    detail = next(d for s, c, d in rep.rows if "points check" in c)
    assert "2.4c per point" in detail   # 300 / 12500 * 100


def test_route_without_a_floor_gets_no_points_check():
    rep = doctor.Report()
    doctor.check_live(rep, cfg(route(award_floor_points=None)), FakeClient(), probes=1)
    assert not statuses(rep, "points check")


def test_probe_errors_warn_but_do_not_abort_the_route():
    rep = doctor.Report()
    doctor.check_live(rep, cfg(), FakeClient(raises=RuntimeError("boom")), probes=2)
    assert statuses(rep, "service exists") == [doctor.FAIL]
    assert len(statuses(rep, "probe")) == 2


# ------------------------------------------------------------- config checking

def test_bad_airport_code_fails(monkeypatch, tmp_path):
    rep = doctor.Report()
    bad = Config(routes=[route(destination="Albuquerque")])
    monkeypatch.setattr(doctor.config, "load", lambda: bad)
    doctor.check_config(rep)
    assert doctor.FAIL in statuses(rep, "airport codes")


def test_inverted_window_fails(monkeypatch):
    rep = doctor.Report()
    monkeypatch.setattr(doctor.config, "load",
                        lambda: Config(routes=[route(window_start_days=200,
                                                     window_end_days=30)]))
    doctor.check_config(rep)
    assert doctor.FAIL in statuses(rep, "window")


def test_missing_award_floor_only_warns(monkeypatch):
    rep = doctor.Report()
    monkeypatch.setattr(doctor.config, "load",
                        lambda: Config(routes=[route(award_floor_points=None)]))
    doctor.check_config(rep)
    assert statuses(rep, "award floor") == [doctor.WARN]
    assert rep.failed == 0


def test_over_quota_config_fails(monkeypatch):
    rep = doctor.Report()
    heavy = [route(name=f"r{i}", destination=d, sweep_stride_days=1)
             for i, d in enumerate(["ABQ", "MSY", "MEX", "SAN"])]
    monkeypatch.setattr(doctor.config, "load",
                        lambda: Config(routes=heavy, monthly_call_quota=2000))
    doctor.check_config(rep)
    assert statuses(rep, "projection") == [doctor.FAIL]


def test_missing_browser_fails(monkeypatch):
    class Dead:
        def _ensure_page(self):
            raise RuntimeError("chromium not installed")

        def close(self):
            pass

    monkeypatch.setattr(doctor, "Alaska", lambda *a, **k: Dead())
    rep = doctor.Report()
    assert doctor.check_browser(rep) is None
    assert rep.failed == 1


def test_browser_ready_passes(monkeypatch):
    class Live:
        def _ensure_page(self):
            return object()

        def close(self):
            pass

    monkeypatch.setattr(doctor, "Alaska", lambda *a, **k: Live())
    rep = doctor.Report()
    assert doctor.check_browser(rep) is not None
    assert rep.failed == 0


def test_missing_email_config_fails(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("ALERT_EMAIL_TO", raising=False)
    rep = doctor.Report()
    doctor.check_email(rep)
    assert rep.failed == 1


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


def _email_env(monkeypatch, sender=None):
    monkeypatch.setenv("RESEND_API_KEY", "k")
    monkeypatch.setenv("ALERT_EMAIL_TO", "me@example.com")
    if sender is None:
        monkeypatch.delenv("ALERT_EMAIL_FROM", raising=False)
    else:
        monkeypatch.setenv("ALERT_EMAIL_FROM", sender)


def test_email_invalid_key_fails(monkeypatch):
    _email_env(monkeypatch, sender="alerts@my.com")
    monkeypatch.setattr(doctor.requests, "get", lambda *a, **k: _Resp(401))
    rep = doctor.Report()
    doctor.check_email(rep)
    assert doctor.FAIL in statuses(rep, "Resend key valid")


def test_email_unexpected_status_warns(monkeypatch):
    _email_env(monkeypatch, sender="alerts@my.com")
    monkeypatch.setattr(doctor.requests, "get", lambda *a, **k: _Resp(500))
    rep = doctor.Report()
    doctor.check_email(rep)
    assert doctor.WARN in statuses(rep, "Resend key valid")


def test_email_network_error_warns(monkeypatch):
    _email_env(monkeypatch, sender="alerts@my.com")

    def boom(*a, **k):
        raise ConnectionError("dns failure")

    monkeypatch.setattr(doctor.requests, "get", boom)
    rep = doctor.Report()
    doctor.check_email(rep)
    assert doctor.WARN in statuses(rep, "Resend reachable")


def test_email_ok_but_resend_dev_sender_warns(monkeypatch):
    _email_env(monkeypatch, sender=None)   # defaults to alerts@resend.dev
    monkeypatch.setattr(doctor.requests, "get", lambda *a, **k: _Resp(200))
    rep = doctor.Report()
    doctor.check_email(rep)
    assert doctor.OK in statuses(rep, "Resend key valid")
    assert doctor.WARN in statuses(rep, "sender domain")
