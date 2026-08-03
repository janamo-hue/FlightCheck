"""Tests for the Amadeus client.

_parse_offer was the least-covered code in the project and the most likely
place a real API payload surprises us, especially now that MEX allows
connections and returns multi-segment itineraries.
"""

import json

import pytest

from src.amadeus import Amadeus, _parse_offer

# Shape taken from the Flight Offers Search v2 response: a round trip with a
# nonstop outbound and a one-stop return.
OFFER = json.loads("""
{
  "type": "flight-offer",
  "id": "1",
  "numberOfBookableSeats": 4,
  "price": {"currency": "USD", "total": "412.30", "grandTotal": "438.60",
            "base": "350.00"},
  "itineraries": [
    {"duration": "PT2H55M",
     "segments": [
       {"departure": {"iataCode": "SEA", "at": "2026-10-01T08:15:00"},
        "arrival":   {"iataCode": "MEX", "at": "2026-10-01T15:10:00"},
        "carrierCode": "AS", "number": "1234", "numberOfStops": 0}
     ]},
    {"duration": "PT8H40M",
     "segments": [
       {"departure": {"iataCode": "MEX", "at": "2026-10-11T09:00:00"},
        "arrival":   {"iataCode": "LAX", "at": "2026-10-11T12:05:00"},
        "carrierCode": "AS", "number": "641", "numberOfStops": 0},
       {"departure": {"iataCode": "LAX", "at": "2026-10-11T14:20:00"},
        "arrival":   {"iataCode": "SEA", "at": "2026-10-11T17:05:00"},
        "carrierCode": "AS", "number": "9", "numberOfStops": 0}
     ]}
  ]
}
""")


def test_parses_grand_total_not_base_or_total():
    # grandTotal includes taxes. Alerting on `base` would understate by ~25%.
    assert _parse_offer(OFFER).price == 438.60


def test_collects_flight_numbers_across_both_itineraries():
    assert _parse_offer(OFFER).flight_numbers == ["AS1234", "AS641", "AS9"]


def test_multi_segment_return_does_not_break_parsing():
    offer = _parse_offer(OFFER)
    assert offer.carrier == "AS"
    assert offer.seats_left == 4
    assert offer.duration == "PT2H55M"      # outbound, not the connecting leg


def test_codeshare_carriers_are_all_reported():
    mixed = json.loads(json.dumps(OFFER))
    mixed["itineraries"][1]["segments"][0]["carrierCode"] = "AA"
    assert _parse_offer(mixed).carrier == "AA,AS"


def test_missing_optional_fields_do_not_raise():
    lean = {"price": {"currency": "USD", "grandTotal": "199.00"},
            "itineraries": [{"segments": [
                {"carrierCode": "AS", "number": "22"}]}]}
    offer = _parse_offer(lean)
    assert offer.price == 199.0
    assert offer.seats_left is None
    assert offer.duration is None


def test_one_way_offer_has_a_single_itinerary():
    one_way = json.loads(json.dumps(OFFER))
    one_way["itineraries"] = one_way["itineraries"][:1]
    assert _parse_offer(one_way).flight_numbers == ["AS1234"]


# ------------------------------------------------------- request construction

class _Resp:
    def __init__(self, payload, status=200):
        self.status_code, self._payload = status, payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def _client(monkeypatch, captured):
    client = Amadeus(key="k", secret="s", host="https://example.invalid")
    client._token, client._token_expires = "tok", 1 << 40

    def fake_get(url, params=None, headers=None, timeout=None):
        captured.update(params)
        return _Resp({"data": [OFFER]})

    monkeypatch.setattr(client.session, "get", fake_get)
    return client


def test_search_sends_nonstop_and_airline_filter(monkeypatch):
    params: dict = {}
    client = _client(monkeypatch, params)
    client.cheapest_direct("SEA", "MSY", "2026-10-01", "2026-10-08")

    assert params["nonStop"] == "true"
    assert params["includedAirlineCodes"] == "AS"
    assert params["originLocationCode"] == "SEA"
    assert params["returnDate"] == "2026-10-08"


def test_nonstop_false_is_sent_as_a_string_not_a_bool(monkeypatch):
    # The SEA-MEX route sets nonstop: false. Python's False would serialise to
    # "False", which Amadeus does not accept.
    params: dict = {}
    client = _client(monkeypatch, params)
    client.cheapest_direct("SEA", "MEX", "2026-10-01", nonstop=False)

    assert params["nonStop"] == "false"
    assert "returnDate" not in params


def test_cheapest_offer_wins_when_several_are_returned(monkeypatch):
    cheaper = json.loads(json.dumps(OFFER))
    cheaper["price"]["grandTotal"] = "301.00"

    client = Amadeus(key="k", secret="s", host="https://example.invalid")
    client._token, client._token_expires = "tok", 1 << 40
    monkeypatch.setattr(client.session, "get",
                        lambda *a, **k: _Resp({"data": [OFFER, cheaper]}))

    assert client.cheapest_direct("SEA", "MEX", "2026-10-01").price == 301.0


def test_no_offers_returns_none(monkeypatch):
    client = Amadeus(key="k", secret="s", host="https://example.invalid")
    client._token, client._token_expires = "tok", 1 << 40
    monkeypatch.setattr(client.session, "get", lambda *a, **k: _Resp({"data": []}))

    assert client.cheapest_direct("SEA", "XXX", "2026-10-01") is None


def test_successful_call_increments_the_counter(monkeypatch):
    client = _client(monkeypatch, {})
    client.cheapest_direct("SEA", "MSY", "2026-10-01")
    client.cheapest_direct("SEA", "MSY", "2026-10-05")
    assert client.calls_made == 2


# ------------------------------------------------------------- branded fares

SAVER = json.loads("""
{"price": {"currency": "USD", "grandTotal": "188.00"},
 "itineraries": [{"segments": [{"carrierCode": "AS", "number": "490"}]}],
 "travelerPricings": [{"fareDetailsBySegment": [
   {"segmentId": "1", "cabin": "ECONOMY", "brandedFare": "SAVER",
    "fareBasis": "QLZNA0MQ", "class": "Q"}]}]}
""")


def test_branded_fare_and_basis_are_captured():
    offer = _parse_offer(SAVER)
    assert offer.branded_fare == "SAVER"
    assert offer.fare_basis == "QLZNA0MQ"


def test_saver_is_detected():
    assert _parse_offer(SAVER).is_saver is True


def test_saver_matching_is_case_and_prefix_tolerant():
    for label in ("saver", "AS_SAVER", "Saver Fare", "MAIN_SAVER"):
        variant = json.loads(json.dumps(SAVER))
        variant["travelerPricings"][0]["fareDetailsBySegment"][0]["brandedFare"] = label
        assert _parse_offer(variant).is_saver is True, label


def test_main_cabin_is_not_a_saver():
    main = json.loads(json.dumps(SAVER))
    main["travelerPricings"][0]["fareDetailsBySegment"][0]["brandedFare"] = "MAIN"
    assert _parse_offer(main).is_saver is False


def test_offer_without_traveler_pricing_has_no_brand():
    offer = _parse_offer(OFFER)
    assert offer.branded_fare is None
    # Unknown brand must not be assumed to be a Saver.
    assert offer.is_saver is False


# ---------------------------------------------------------- saver exclusion

def _offer(price, brand=None):
    o = json.loads(json.dumps(OFFER))
    o["price"]["grandTotal"] = f"{price:.2f}"
    o["travelerPricings"] = [{"fareDetailsBySegment": [{"brandedFare": brand}]}] if brand \
        else [{"fareDetailsBySegment": [{}]}]
    return o


def _pool(monkeypatch, offers, **kw):
    client = Amadeus(key="k", secret="s", host="https://example.invalid")
    client._token, client._token_expires = "tok", 1 << 40
    monkeypatch.setattr(client.session, "get", lambda *a, **k: _Resp({"data": offers}))
    return client.cheapest_direct("SEA", "MSY", "2026-10-01", **kw)


def test_saver_is_skipped_for_the_next_cheapest(monkeypatch):
    offers = [_offer(209, "SAVER"), _offer(268, "MAIN"), _offer(410, "FIRST")]
    result = _pool(monkeypatch, offers, exclude_saver=True)
    assert result.price == 268.0
    assert result.branded_fare == "MAIN"


def test_saver_price_is_retained_for_comparison(monkeypatch):
    offers = [_offer(209, "SAVER"), _offer(268, "MAIN")]
    result = _pool(monkeypatch, offers, exclude_saver=True)
    assert result.saver_price == 209.0
    assert result.savers_excluded == 1


def test_exclusion_off_still_returns_the_saver(monkeypatch):
    offers = [_offer(209, "SAVER"), _offer(268, "MAIN")]
    result = _pool(monkeypatch, offers, exclude_saver=False)
    assert result.price == 209.0
    assert result.savers_excluded == 0
    # The comparison price is still recorded either way.
    assert result.saver_price == 209.0


def test_all_saver_returns_none_rather_than_falling_back(monkeypatch):
    offers = [_offer(209, "SAVER"), _offer(239, "SAVER")]
    assert _pool(monkeypatch, offers, exclude_saver=True) is None


def test_unlabelled_offers_are_kept_not_dropped(monkeypatch):
    # Dropping unknown brands would silently empty any route where Amadeus
    # omits brandedFare, which is the invisible failure we are avoiding.
    offers = [_offer(209, "SAVER"), _offer(268)]
    result = _pool(monkeypatch, offers, exclude_saver=True)
    assert result.price == 268.0
    assert result.branded_fare is None


def test_cheapest_non_saver_wins_regardless_of_response_order(monkeypatch):
    offers = [_offer(410, "FIRST"), _offer(268, "MAIN"), _offer(209, "SAVER")]
    assert _pool(monkeypatch, offers, exclude_saver=True).price == 268.0


def test_no_saver_present_leaves_the_comparison_empty(monkeypatch):
    result = _pool(monkeypatch, [_offer(268, "MAIN")], exclude_saver=True)
    assert result.saver_price is None
    assert result.savers_excluded == 0


def test_max_offers_is_sent_to_the_api(monkeypatch):
    params: dict = {}
    client = _client(monkeypatch, params)
    client.cheapest_direct("SEA", "MSY", "2026-10-01", max_offers=30)
    assert params["max"] == 30
