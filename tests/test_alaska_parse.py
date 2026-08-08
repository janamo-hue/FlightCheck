"""Parser and resilience tests for the Alaska scraper.

The DOM parse is the most defect-prone code in the repo and the site of the
historical column-mislabel bug (every tracked fare recorded one column too
high). audit.py catches that only after bad rows accumulate in committed data;
these pin the behaviour at the code level, with no browser, via stub page/card
objects exposing only the five methods the scraper actually calls.
"""

import re

import pytest

from src.alaska import Alaska, _Fare


def _pred(sel):
    m = re.match(r'\[data-testid([\^]?)="([^"]+)"\]', sel)
    if not m:
        return lambda tid: False
    op, val = m.group(1), m.group(2)
    if op == "^":
        return lambda tid: bool(tid) and tid.startswith(val)
    return lambda tid: tid == val


class El:
    """A stand-in for a Playwright element handle."""

    def __init__(self, testid=None, text="", children=()):
        self.testid = testid
        self.text = text
        self.children = list(children)

    def get_attribute(self, name):
        return self.testid if name == "data-testid" else None

    def inner_text(self):
        return self.text

    def query_selector(self, sel):
        pred = _pred(sel)
        return next((c for c in self.children if pred(c.testid)), None)

    def query_selector_all(self, sel):
        pred = _pred(sel)
        return [c for c in self.children if pred(c.testid)]


class Page:
    def __init__(self, cards=(), headers=(), body="", cards_render=True):
        self.cards = list(cards)
        self.headers = list(headers)
        self.body = body
        self.cards_render = cards_render

    def wait_for_selector(self, sel, timeout=None):
        if not self.cards_render:
            raise TimeoutError("timed out waiting for flight cards")
        return True

    def inner_text(self, sel):
        return self.body

    def query_selector(self, sel):
        pred = _pred(sel)
        return next((c for c in self.cards if pred(c.testid)), None)

    def query_selector_all(self, sel):
        pred = _pred(sel)
        return [e for e in (self.headers + self.cards) if pred(e.testid)]


def headers(*brands):
    return [El(testid=f"columnheader-{b}") for b in brands]


def card(n, *, stops=0, tiles=None, text="AS327 Nonstop 2h 15m"):
    children = []
    if stops is not None:
        children.append(El(testid=f"flight-details-{n}-stops-{stops}"))
    for col, price in (tiles or {}).items():
        children.append(El(testid=f"valuetile-{n}-{col}", text=price))
    return El(testid=f"flight-card-{n}", text=text, children=children)


def _client():
    c = Alaska.__new__(Alaska)
    c.nav_timeout_ms = 1000
    return c


# ---------------------------------------------------------------- _scrape

def test_scrape_labels_prices_by_dom_column():
    # The exact case the old bug got wrong: prices are labelled by the DOM
    # column, so SAVER->358 and MAIN->458, not shifted up a column.
    page = Page(
        cards=[card(0, tiles={0: "$358", 1: "$458", 2: "$918", 3: "$1,438"})],
        headers=headers("SAVER", "MAIN", "PREMIUM", "FIRST"),
    )
    got = {f.brand: f.price for f in _client()._scrape(page, nonstop=True)}
    assert got == {"SAVER": 358.0, "MAIN": 458.0, "PREMIUM": 918.0, "FIRST": 1438.0}


def test_scrape_raises_on_tile_header_mismatch():
    # Four brand headers but the SAVER column is not a valuetile (the real
    # historical shape): refuse to guess rather than label MAIN's price SAVER.
    page = Page(
        cards=[card(0, tiles={1: "$458", 2: "$918", 3: "$1,438"})],
        headers=headers("SAVER", "MAIN", "PREMIUM", "FIRST"),
    )
    with pytest.raises(RuntimeError, match="fare grid mismatch"):
        _client()._scrape(page, nonstop=True)


def test_scrape_drops_connecting_cards():
    page = Page(
        cards=[card(0, stops=0, tiles={0: "$358", 1: "$458"}),
               card(1, stops=1, tiles={0: "$500", 1: "$600"})],
        headers=headers("SAVER", "MAIN"),
    )
    prices = sorted(f.price for f in _client()._scrape(page, nonstop=True))
    assert prices == [358.0, 458.0]  # only the nonstop card 0


def test_scrape_returns_none_when_grid_never_renders():
    page = Page(cards_render=False, body="Please verify you are human")
    assert _client()._scrape(page, nonstop=True) is None


def test_scrape_returns_empty_on_a_no_flights_page():
    page = Page(cards_render=False, body="Sorry, no flights for these dates")
    assert _client()._scrape(page, nonstop=True) == []


# ------------------------------------------------------------ _brand_order

def test_brand_order_reads_dom_order():
    page = Page(headers=headers("SAVER", "MAIN", "PREMIUM", "FIRST"))
    assert Alaska._brand_order(page) == ["SAVER", "MAIN", "PREMIUM", "FIRST"]


def test_brand_order_raises_without_known_headers():
    with pytest.raises(RuntimeError, match="no recognisable brand"):
        Alaska._brand_order(Page(headers=headers("FOO", "BAR")))


def test_brand_order_ignores_unknown_columns():
    page = Page(headers=[El(testid="columnheader-SAVER"),
                         El(testid="columnheader-REFUNDABLE"),
                         El(testid="columnheader-MAIN")])
    assert Alaska._brand_order(page) == ["SAVER", "MAIN"]


# ------------------------------------------------------------------ _tiles

def test_tiles_are_keyed_by_actual_column_index():
    # Gap at index 2 must be preserved, not collapsed: that gap is what lets
    # _scrape's length check catch a dropped column.
    c = El(testid="flight-card-2", children=[
        El(testid="valuetile-2-0", text="$358"),
        El(testid="valuetile-2-1", text="$458"),
        El(testid="valuetile-2-3", text="$1,438"),
    ])
    assert _client()._tiles(c, 2) == {0: 358.0, 1: 458.0, 3: 1438.0}


def test_tiles_absent_price_maps_to_none():
    c = El(testid="flight-card-0", children=[
        El(testid="valuetile-0-0", text="$358"),
        El(testid="valuetile-0-1", text="Sold out"),
    ])
    assert _client()._tiles(c, 0) == {0: 358.0, 1: None}


# ------------------------------------------------------------------ _stops

def test_stops_parses_the_testid():
    c = El(testid="flight-card-0", children=[El(testid="flight-details-0-stops-1")])
    assert Alaska._stops(c, 0) == 1


def test_stops_is_none_when_absent():
    assert Alaska._stops(El(testid="flight-card-0"), 0) is None


# ------------------------------------------------------- _price / _duration

def test_price_parsing():
    assert Alaska._price("$1,438") == 1438.0
    assert Alaska._price("$ 358") == 358.0
    assert Alaska._price("From $358 round trip") == 358.0
    assert Alaska._price("Sold out") is None


def test_duration_parsing():
    assert Alaska._duration("AS327 Nonstop 2h 15m") == "2h15m"
    assert Alaska._duration("5h nonstop") == "5h"
    assert Alaska._duration("Multiple flights") is None


# ----------------------------------------------- _load_fares resilience

def _fast(max_retries=2):
    c = Alaska.__new__(Alaska)
    c.min_interval_s = c.max_interval_s = 0.0   # no pacing sleep in tests
    c.retry_backoff_s = 0.0                     # no backoff sleep
    c.max_retries = max_retries
    c._last_nav = 0.0
    return c


class Resp:
    def __init__(self, status):
        self.status = status


class Goto:
    def __init__(self, status=200):
        self.status = status

    def goto(self, url, wait_until=None):
        return Resp(self.status)


def test_load_fares_retries_a_transient_empty_render(monkeypatch):
    c = _fast()
    monkeypatch.setattr(c, "_ensure_page", lambda: Goto(200))
    seq = [None, None, [_Fare("MAIN", 458.0, ["AS1"], None)]]
    monkeypatch.setattr(c, "_scrape", lambda p, *, nonstop: seq.pop(0))
    assert c._load_fares("http://x", nonstop=True) == [_Fare("MAIN", 458.0, ["AS1"], None)]


def test_load_fares_returns_none_when_every_attempt_fails(monkeypatch):
    c = _fast(max_retries=2)
    monkeypatch.setattr(c, "_ensure_page", lambda: Goto(200))
    monkeypatch.setattr(c, "_scrape", lambda p, *, nonstop: None)
    assert c._load_fares("http://x", nonstop=True) is None


def test_load_fares_http_error_is_not_recorded_as_unserved(monkeypatch):
    c = _fast(max_retries=0)
    monkeypatch.setattr(c, "_ensure_page", lambda: Goto(403))
    monkeypatch.setattr(c, "_scrape", lambda p, *, nonstop: [])   # empty grid
    assert c._load_fares("http://x", nonstop=True) is None        # not [] -> not "no service"


def test_load_fares_empty_grid_on_200_is_unserved(monkeypatch):
    c = _fast(max_retries=0)
    monkeypatch.setattr(c, "_ensure_page", lambda: Goto(200))
    monkeypatch.setattr(c, "_scrape", lambda p, *, nonstop: [])
    assert c._load_fares("http://x", nonstop=True) == []


def test_load_fares_rebuilds_on_a_dead_browser(monkeypatch):
    c = _fast(max_retries=1)
    page = Goto(200)
    closed = []
    monkeypatch.setattr(c, "_ensure_page", lambda: page)
    monkeypatch.setattr(c, "close", lambda: closed.append(True))
    calls = {"n": 0}

    def goto(url, wait_until=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Target page, context or browser has been closed")
        return Resp(200)

    monkeypatch.setattr(page, "goto", goto)
    monkeypatch.setattr(c, "_scrape", lambda p, *, nonstop: [_Fare("MAIN", 458.0, ["AS1"], None)])
    out = c._load_fares("http://x", nonstop=True)
    assert closed == [True]                                       # rebuilt after the crash
    assert out == [_Fare("MAIN", 458.0, ["AS1"], None)]


def test_load_fares_propagates_a_parse_mismatch(monkeypatch):
    # A markup change is a deliberate "refuse to guess" and must surface, not
    # be swallowed by the retry loop.
    c = _fast()
    monkeypatch.setattr(c, "_ensure_page", lambda: Goto(200))

    def boom(p, *, nonstop):
        raise RuntimeError("fare grid mismatch on card 0")

    monkeypatch.setattr(c, "_scrape", boom)
    with pytest.raises(RuntimeError, match="fare grid mismatch"):
        c._load_fares("http://x", nonstop=True)


# ------------------------------------------------------------ _price_search

def test_price_search_raises_when_grid_never_renders(monkeypatch):
    c = Alaska.__new__(Alaska)
    c.calls_made = 0
    monkeypatch.setattr(c, "_load_fares", lambda url, *, nonstop: None)
    with pytest.raises(RuntimeError, match="no fare grid"):
        c._price_search("SEA", "ABQ", "2026-10-17", ret=None, adults=1,
                        currency="USD", cabin="ECONOMY", nonstop=True, exclude_saver=False)
    assert c.calls_made == 1


def test_price_search_excludes_saver_and_builds_the_ladder(monkeypatch):
    c = Alaska.__new__(Alaska)
    c.calls_made = 0
    fares = [_Fare("SAVER", 358.0, ["AS1"], "2h"),
             _Fare("MAIN", 458.0, ["AS1"], "2h"),
             _Fare("PREMIUM", 918.0, ["AS2"], "2h")]
    monkeypatch.setattr(c, "_load_fares", lambda url, *, nonstop: fares)
    offer = c._price_search("SEA", "ABQ", "2026-10-17", ret="2026-10-24", adults=1,
                            currency="USD", cabin="ECONOMY", nonstop=True, exclude_saver=True)
    assert offer.branded_fare == "MAIN"          # cheapest non-Saver
    assert offer.price == 458.0
    assert offer.saver_price == 358.0
    assert offer.savers_excluded == 1
    # Ladder spans every brand seen, including the out-of-cabin PREMIUM.
    assert offer.ladder == {"SAVER": 358.0, "MAIN": 458.0, "PREMIUM": 918.0}
