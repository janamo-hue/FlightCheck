"""Round-trip deep link tests.

DD was found by probing candidates against the live site: ID, RD, RTD, OD2,
ID1, returnDate and IDate all produced no grid, while DD returned prices
matching a real search on all four brands.
"""

# ------------------------------------------------------- round-trip deep link

def _client():
    from src.alaska import Alaska
    return Alaska.__new__(Alaska)


def test_one_way_url_has_no_return_date():
    u = _client()._url("SEA", "ABQ", "2026-10-17", 1)
    assert "RT=false" in u
    assert "DD=" not in u


def test_round_trip_url_uses_DD_and_RT_true():
    # DD was found by probing candidates against the live site; ID, RD, RTD,
    # OD2, ID1, returnDate and IDate all produced no grid at all.
    u = _client()._url("SEA", "ABQ", "2026-10-17", 1, "2026-10-24")
    assert "RT=true" in u
    assert "DD=2026-10-24" in u
    assert "OD=2026-10-17" in u


def test_round_trip_is_a_single_page_load(monkeypatch):
    """The sum of two one-ways overstated every brand by $61 on SEA-ABQ."""
    from src import alaska
    calls = []
    client = _client()
    monkeypatch.setattr(
        alaska.Alaska, "_price_search",
        lambda self, o, d, dep, **kw: calls.append((o, d, dep, kw.get("ret"))))
    client.cheapest_direct("SEA", "ABQ", "2026-10-17", "2026-10-24")
    assert len(calls) == 1
    assert calls[0] == ("SEA", "ABQ", "2026-10-17", "2026-10-24")


def test_combine_helper_is_gone():
    from src.alaska import Alaska
    assert not hasattr(Alaska, "_combine")
