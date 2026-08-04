"""Alaska fare source: scrape the public results page with Playwright.

Drop-in replacement for the decommissioned Amadeus Self-Service client. Exposes
the same surface the scanner depends on: an ``Alaska`` client with
``cheapest_direct(...) -> Offer | None`` and a ``calls_made`` counter, plus the
``Offer`` dataclass and a ``QuotaExceeded`` exception (kept for interface
compatibility; scraping has no monthly quota, so it is never raised here).

Why scraping and not an API: Amadeus Self-Service shut down 2026-07-17 and
Alaska publishes no free fare API. Its results page renders a fare matrix into
the DOM with stable ``data-testid`` hooks, discovered via spikes/alaska/. See
that folder's README for the reverse-engineering trail.

The results URL is a real dated deep link (this overturns the note in
notify.py:booking_url, written before the format was known):

    https://www.alaskaair.com/search/results?O=SEA&D=SFO&OD=2026-08-19&A=1&RT=false&locale=en-us

Each nonstop flight is a ``flight-card-N`` with fare cells ``valuetile-N-C``,
one per brand column read from ``columnheader-*`` (SAVER, MAIN, PREMIUM, FIRST).
Because Alaska labels the SAVER column explicitly, Saver exclusion is exact
here, unlike the brand-string guess the Amadeus client had to make.

Round trips are not supported yet: only the one-way deep link is verified. A
round-trip request returns None with a warning until the RT=true URL and its
two-slice selection are captured (tracked in spikes/alaska/README.md).
"""

from __future__ import annotations

import argparse
import atexit
import logging
import re
import sys
from dataclasses import dataclass
from urllib.parse import urlencode

log = logging.getLogger(__name__)

RESULTS_URL = "https://www.alaskaair.com/search/results"

# Column headers, in the order Alaska renders them, map 1:1 to valuetile columns.
KNOWN_BRANDS = ("SAVER", "MAIN", "PREMIUM", "FIRST")
# Which brand columns count as the requested cabin. ECONOMY is Saver + Main;
# Premium/First are separate cabins we do not track for an ECONOMY route.
CABIN_BRANDS = {
    "ECONOMY": ("SAVER", "MAIN"),
    "PREMIUM_ECONOMY": ("PREMIUM",),
    "PREMIUM": ("PREMIUM",),
    "BUSINESS": ("FIRST",),
    "FIRST": ("FIRST",),
}

_PRICE = re.compile(r"\$\s?([0-9][0-9,]*)")
_FLIGHT = re.compile(r"\b((?:AS|OO)\s?\d{2,4})\b")
_STOPS = re.compile(r"(\d+)\s*stop", re.I)


class QuotaExceeded(RuntimeError):
    """Kept for parity with the old Amadeus client. Not raised by scraping."""


@dataclass
class Offer:
    price: float
    currency: str
    carrier: str
    flight_numbers: list[str]
    duration: str | None
    seats_left: int | None
    branded_fare: str | None = None
    fare_basis: str | None = None
    saver_price: float | None = None
    savers_excluded: int = 0

    @property
    def is_saver(self) -> bool:
        return bool(self.branded_fare and "SAVER" in self.branded_fare.upper())


@dataclass
class _Fare:
    brand: str
    price: float
    flight_numbers: list[str]
    duration: str | None


class Alaska:
    """Prices one route+date per ``cheapest_direct`` call by loading the grid.

    Holds one headless browser for the life of the client and reuses a page
    across calls; call ``close()`` (or use as a context manager) when done. The
    scanner constructs this once per run, so a persistent browser amortises the
    launch cost across every date it prices.
    """

    def __init__(self, *, headless: bool = True, nav_timeout_ms: int = 45000):
        self.headless = headless
        self.nav_timeout_ms = nav_timeout_ms
        self.calls_made = 0
        self._pw = None
        self._browser = None
        self._page = None

    # ------------------------------------------------------------ browser
    def _ensure_page(self):
        if self._page is not None:
            return self._page
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - env guard
            raise RuntimeError(
                "playwright is required for the Alaska client. Install with "
                "`pip install playwright && playwright install chromium`."
            ) from exc
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        ctx = self._browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"),
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="America/Los_Angeles",
        )
        self._page = ctx.new_page()
        self._page.set_default_timeout(self.nav_timeout_ms)
        atexit.register(self.close)
        return self._page

    def close(self) -> None:
        for obj, meth in ((self._browser, "close"), (self._pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, meth)()
            except Exception:  # pragma: no cover - best-effort teardown
                pass
        self._browser = self._pw = self._page = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------- search
    def _url(self, origin: str, dest: str, depart: str, adults: int) -> str:
        return RESULTS_URL + "?" + urlencode({
            "O": origin, "D": dest, "OD": depart,
            "A": adults, "RT": "false", "locale": "en-us",
        })

    def cheapest_direct(
        self,
        origin: str,
        destination: str,
        depart: str,
        ret: str | None = None,
        *,
        carrier: str = "AS",          # accepted for signature parity; AS-only
        adults: int = 1,
        currency: str = "USD",
        cabin: str = "ECONOMY",
        nonstop: bool = True,
        exclude_saver: bool = False,
        max_offers: int = 20,         # accepted for parity; grid returns all
    ) -> Offer | None:
        """Cheapest matching fare for the date, or None if unavailable.

        Mirrors the old Amadeus method's contract. ``exclude_saver`` drops the
        SAVER column exactly (Alaska labels it), so the tracked fare is one that
        earns Atmos points. Offers with no price cell are skipped.
        """
        if ret is not None:
            log.warning("round trips not yet supported by the Alaska scraper "
                        "(%s-%s %s/%s); skipping", origin, destination, depart, ret)
            return None

        allowed = set(CABIN_BRANDS.get(cabin.upper(), ("SAVER", "MAIN")))
        page = self._ensure_page()
        self.calls_made += 1
        page.goto(self._url(origin, destination, depart, adults),
                  wait_until="domcontentloaded")

        fares = self._scrape(page, nonstop=nonstop)
        if fares is None:
            # Grid never rendered: likely a bot challenge or an outage, not a
            # merely-unserved route. Raise so the scanner's failure guard trips
            # instead of silently recording "no service".
            raise RuntimeError(
                f"no fare grid for {origin}-{destination} {depart} "
                f"(blocked, or page structure changed)")

        pool = [f for f in fares if f.brand in allowed]
        savers = [f for f in pool if f.brand == "SAVER"]
        saver_price = min((f.price for f in savers), default=None)

        priceable = [f for f in pool if not (exclude_saver and f.brand == "SAVER")]
        if not priceable:
            return None

        best = min(priceable, key=lambda f: f.price)
        return Offer(
            price=best.price,
            currency=currency,
            carrier="AS",
            flight_numbers=best.flight_numbers,
            duration=best.duration,
            seats_left=None,               # not exposed in the grid
            branded_fare=best.brand,
            fare_basis=None,
            saver_price=saver_price,
            savers_excluded=len(savers) if exclude_saver else 0,
        )

    # ------------------------------------------------------------- scrape
    def _scrape(self, page, *, nonstop: bool) -> list[_Fare] | None:
        """Parse the fare matrix from the rendered DOM.

        Returns a flat list of (brand, price, flight) fares across all cards, or
        None if the grid never appeared. An empty list means the grid rendered
        but held no bookable fares (a genuinely unserved date).
        """
        try:
            page.wait_for_selector('[data-testid^="flight-card-"]',
                                   timeout=self.nav_timeout_ms)
        except Exception:
            body = ""
            try:
                body = page.inner_text("body").lower()
            except Exception:
                pass
            if any(s in body for s in ("no flights", "no results",
                                       "we couldn't find", "unavailable")):
                return []
            return None

        brands = self._brand_order(page)
        fares: list[_Fare] = []
        for n in range(50):  # generous upper bound; break when a card is missing
            card = page.query_selector(f'[data-testid="flight-card-{n}"]')
            if card is None:
                break
            text = card.inner_text()
            if nonstop:
                m = _STOPS.search(text)
                if m and int(m.group(1)) > 0:
                    continue
                if "nonstop" not in text.lower() and (m is None):
                    # No explicit nonstop label and no stop count: keep it, but
                    # only if the whole itinerary looks like a single segment.
                    if len(_FLIGHT.findall(text)) > 1:
                        continue
            flights = [f.replace(" ", "") for f in _FLIGHT.findall(text)]
            dur = self._duration(text)
            for c, brand in enumerate(brands):
                tile = card.query_selector(f'[data-testid="valuetile-{n}-{c}"]')
                if tile is None:
                    continue
                price = self._price(tile.inner_text())
                if price is not None:
                    fares.append(_Fare(brand, price, flights, dur))
        return fares

    @staticmethod
    def _brand_order(page) -> list[str]:
        order = []
        for h in page.query_selector_all('[data-testid^="columnheader-"]'):
            name = (h.get_attribute("data-testid") or "").split("columnheader-")[-1]
            if name in KNOWN_BRANDS:
                order.append(name)
        return order or list(KNOWN_BRANDS)

    @staticmethod
    def _price(text: str) -> float | None:
        m = _PRICE.search(text or "")
        return float(m.group(1).replace(",", "")) if m else None

    @staticmethod
    def _duration(text: str) -> str | None:
        m = re.search(r"\d+\s*h\s*\d*\s*m?", text or "")
        return m.group(0).replace(" ", "") if m else None


# --------------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Price one Alaska route+date")
    ap.add_argument("--from", dest="origin", required=True)
    ap.add_argument("--to", dest="dest", required=True)
    ap.add_argument("--depart", required=True, help="YYYY-MM-DD")
    ap.add_argument("--return", dest="ret", default=None)
    ap.add_argument("--cabin", default="ECONOMY")
    ap.add_argument("--exclude-saver", action="store_true")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    with Alaska(headless=not args.headed) as client:
        offer = client.cheapest_direct(
            args.origin, args.dest, args.depart, args.ret,
            cabin=args.cabin, exclude_saver=args.exclude_saver)

    if offer is None:
        print(f"no offer for {args.origin}-{args.dest} {args.depart}")
        return 1
    print(f"{args.origin}-{args.dest} {args.depart}: {offer.currency} "
          f"{offer.price:,.0f} [{offer.branded_fare}] "
          f"{'/'.join(offer.flight_numbers) or '?'} {offer.duration or ''}"
          + (f"  (saver {offer.saver_price:,.0f}, excluded {offer.savers_excluded})"
             if offer.saver_price is not None else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
