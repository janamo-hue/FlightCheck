"""The shared booking-link builders (src.links), used by notify and report."""

from src import links


def test_google_flights_round_trip_pins_both_dates():
    url = links.google_flights_url("SEA", "ABQ", "2026-10-17", "2026-10-24")
    assert url.startswith("https://www.google.com/travel/flights?")
    assert "SEA" in url and "ABQ" in url
    assert "2026-10-17" in url and "2026-10-24" in url
    assert "nonstop" in url
    assert "through" in url


def test_google_flights_one_way_has_no_return():
    url = links.google_flights_url("SEA", "ABQ", "2026-10-17", None)
    assert "through" not in url
    assert "2026-10-17" in url


def test_alaska_route_url_uses_city_slugs():
    assert (links.alaska_route_url(("seattle", "albuquerque"))
            == "https://www.alaskaair.com/en/flights-from-seattle-to-albuquerque")


def test_alaska_route_url_is_none_without_cities():
    assert links.alaska_route_url(None) is None


def test_notify_delegates_to_the_shared_builders():
    # Guards against notify and report drifting apart: notify must produce the
    # same strings the shared helpers do.
    from src.alerts import Alert
    from src.notify import alaska_url, booking_url

    a = Alert(kind="drop", route_name="Seattle to Albuquerque", route_key="SEA-ABQ",
              depart="2026-10-17", ret="2026-10-24", price=300.0, currency="USD",
              baseline=400.0, drop_pct=25.0, all_time_low=False,
              flight_numbers=["AS327"], observations=5,
              cities=("seattle", "albuquerque"))
    assert booking_url(a) == links.google_flights_url("SEA", "ABQ", "2026-10-17", "2026-10-24")
    assert alaska_url(a) == links.alaska_route_url(("seattle", "albuquerque"))
