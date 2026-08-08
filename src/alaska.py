"""Alaska fare source: scrape the public results page with Playwright.

Drop-in replacement for the decommissioned Amadeus Self-Service client. Exposes
the same surface the scanner depends on: an ``Alaska`` client with
``cheapest_direct(...) -> Offer | None`` and a ``calls_made`` counter, plus the
``Offer`` dataclass.

Why scraping and not an API: Amadeus Self-Service shut down 2026-07-17 and
Alaska publishes no free fare API. Its results page renders a fare matrix into
the DOM with stable ``data-testid`` hooks, discovered via spikes/alaska/. See
that folder's README for the reverse-engineering trail.

The results URL is a real dated deep link:

    https://www.alaskaair.com/search/results?O=SEA&D=SFO&OD=2026-08-19&A=1&RT=false&locale=en-us

Each nonstop flight is a ``flight-card-N`` with fare cells ``valuetile-N-C``,
one per brand column read from ``columnheader-*`` (SAVER, MAIN, PREMIUM, FIRST).
Because Alaska labels the SAVER column explicitly, Saver exclusion is exact
here, unlike the brand-string guess the old Amadeus client had to make.

A round trip is one page load, not two. ``RT=true`` with the return date in
``DD`` renders a grid of round-trip totals, so both directions are priced
together in a single search. An earlier version summed two one-way loads and
overstated every total, because Alaska discounts a round trip against two
one-ways by a flat amount per brand. See ``cheapest_direct`` for the numbers
and spikes/alaska/README.md for how the ``DD`` parameter was found.
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import logging
import random
import re
import sys
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

log = logging.getLogger(__name__)

RESULTS_URL = "https://www.alaskaair.com/search/results"

# Space out navigations. A tight, regular stream of identical requests from one
# datacenter IP is exactly what bot detection scores on, so wait a randomized
# interval between page loads. The jitter matters as much as the mean: a fixed
# delay is itself a signature.
DEFAULT_MIN_INTERVAL_S = 3.0
DEFAULT_MAX_INTERVAL_S = 8.0
# Reload a grid that never rendered a few times before declaring it blocked. A
# slow SPA paint or a one-off challenge is transient; a real outage fails every
# attempt and still trips the scanner's failure guard.
DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_BACKOFF_S = 2.0

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
_FLIGHT = re.compile(r"\b((?:AS|OO|QX)\s?\d{2,4})\b")


@dataclass
class Offer:
    price: float
    currency: str
    carrier: str
    flight_numbers: list[str]
    duration: str | None
    branded_fare: str | None = None
    saver_price: float | None = None
    savers_excluded: int = 0
    # Every brand seen and its cheapest price. Two numbers per search were not
    # enough to notice that they were the wrong two.
    ladder: dict[str, float] = field(default_factory=dict)


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

    def __init__(
        self, *, headless: bool = True, nav_timeout_ms: int = 45000,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        max_interval_s: float = DEFAULT_MAX_INTERVAL_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_s: float = DEFAULT_RETRY_BACKOFF_S,
    ):
        self.headless = headless
        self.nav_timeout_ms = nav_timeout_ms
        self.min_interval_s = min_interval_s
        self.max_interval_s = max_interval_s
        self.max_retries = max_retries
        self.retry_backoff_s = retry_backoff_s
        self.calls_made = 0
        self._last_nav = 0.0
        self._pw = None
        self._browser = None
        self._page = None

    # -------------------------------------------------------------- pacing
    def _pace(self) -> None:
        """Sleep a jittered interval since the last navigation.

        No wait before the first load. Set ``max_interval_s=0`` to disable
        (used in tests so they do not sleep).
        """
        if self._last_nav and self.max_interval_s > 0:
            wait = random.uniform(self.min_interval_s, self.max_interval_s)
            elapsed = time.monotonic() - self._last_nav
            if elapsed < wait:
                time.sleep(wait - elapsed)
        self._last_nav = time.monotonic()

    @staticmethod
    def _is_dead_browser(exc: Exception) -> bool:
        """Whether an exception means the page/context/browser died mid-run.

        A crashed renderer (OOM on the runner, closed target) leaves the cached
        page non-None, so without this every later call would re-fail against a
        dead page and cascade the whole run into the failure guard.
        """
        msg = str(exc).lower()
        return any(s in msg for s in ("closed", "crash", "target", "disconnected"))

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
    def _url(self, origin: str, dest: str, depart: str, adults: int,
             ret: str | None = None) -> str:
        """Results deep link. With a return date this is a round-trip search.

        `DD` is the return date, found by probing candidates against the live
        site (spikes/alaska/rt_probe.py). An unrecognised name is silently
        ignored and still renders a plausible one-way grid, so the probe
        accepted a candidate only when all four brand prices matched a real
        search rather than when the page merely loaded.
        """
        params = {
            "O": origin, "D": dest, "OD": depart, "A": adults,
            "RT": "true" if ret else "false", "locale": "en-us",
        }
        if ret:
            params["DD"] = ret
        return RESULTS_URL + "?" + urlencode(params)

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
        """Cheapest matching fare, or None if unavailable.

        Mirrors the old Amadeus method's contract. ``exclude_saver`` drops the
        SAVER column exactly (Alaska labels it), so the tracked fare is one that
        earns Atmos points.

        A round trip is one search, not two one-ways added together. The
        previous version summed them on the claim that Alaska prices each
        direction independently. It does not: SEA-ABQ on 17-24 Oct is 179 + 179
        one-way for Saver but 297 round trip, and 229 + 229 versus 397 for
        Main. Alaska discounts the round trip by a flat 61 across every brand,
        so the sum overstated every observation this tool ever recorded.

        Pricing the pair together also makes brand availability real. Alaska
        only offers brands bookable on that specific pairing, so a return leg
        with no Saver inventory no longer yields a Saver fare that cannot
        actually be bought.
        """
        return self._price_search(
            origin, destination, depart, ret=ret, adults=adults,
            currency=currency, cabin=cabin, nonstop=nonstop,
            exclude_saver=exclude_saver)

    def _price_search(
        self, origin: str, destination: str, depart: str, *, ret: str | None = None,
        adults: int, currency: str, cabin: str, nonstop: bool, exclude_saver: bool,
    ) -> Offer | None:
        """Cheapest matching fare from one search, or None.

        One page load whether or not there is a return date, because the
        round-trip grid already shows round-trip totals.
        """
        allowed = set(CABIN_BRANDS.get(cabin.upper(), ("SAVER", "MAIN")))
        self.calls_made += 1
        url = self._url(origin, destination, depart, adults, ret)
        fares = self._load_fares(url, nonstop=nonstop)
        if fares is None:
            # Grid never rendered after retries: a bot challenge or an outage,
            # not a merely-unserved route. Raise so the scanner's failure guard
            # trips instead of silently recording "no service".
            raise RuntimeError(
                f"no fare grid for {origin}-{destination} {depart} "
                f"(blocked, or page structure changed)")

        pool = [f for f in fares if f.brand in allowed]
        savers = [f for f in pool if f.brand == "SAVER"]
        saver_price = min((f.price for f in savers), default=None)

        priceable = [f for f in pool if not (exclude_saver and f.brand == "SAVER")]
        if not priceable:
            return None

        ladder: dict[str, float] = {}
        for f in fares:
            if f.brand not in ladder or f.price < ladder[f.brand]:
                ladder[f.brand] = f.price

        best = min(priceable, key=lambda f: f.price)
        return Offer(
            ladder=ladder,
            price=best.price,
            currency=currency,
            carrier="AS",
            flight_numbers=best.flight_numbers,
            duration=best.duration,
            branded_fare=best.brand,
            saver_price=saver_price,
            savers_excluded=len(savers) if exclude_saver else 0,
        )

    def _load_fares(self, url: str, *, nonstop: bool) -> list[_Fare] | None:
        """Load and parse a results page, with pacing, retries and recovery.

        Returns the parsed fares (possibly an empty list for a genuinely
        unserved date), or None if the grid never rendered after every retry.
        A bounded reload absorbs the transient jitter the scanner's failure
        guard is meant to see through, without masking a real block: a genuine
        outage fails every attempt and still returns None. A parse mismatch
        (the markup changed) is a deliberate "refuse to guess" and propagates
        immediately rather than being retried.
        """
        for attempt in range(self.max_retries + 1):
            self._pace()
            page = self._ensure_page()
            try:
                resp = page.goto(url, wait_until="domcontentloaded")
                status = resp.status if resp is not None else None
            except Exception as exc:
                # Navigation itself failed. Rebuild only if the browser died;
                # otherwise just reload on the next attempt.
                if self._is_dead_browser(exc):
                    log.warning("browser unresponsive (%s); rebuilding", exc)
                    self.close()
                else:
                    log.warning("navigation failed (%s); retrying", exc)
            else:
                # _scrape's mismatch RuntimeError is intentionally outside this
                # guard so it surfaces instead of being retried.
                fares = self._scrape(page, nonstop=nonstop)
                # An HTTP error page (403/429/5xx) is a block or outage, never a
                # genuinely unserved date, so do not let the empty-grid branch
                # record it as "no service".
                if fares == [] and status is not None and status >= 400:
                    fares = None
                if fares is not None:
                    return fares
            if attempt < self.max_retries:
                backoff = self.retry_backoff_s * (attempt + 1)
                time.sleep(random.uniform(backoff, backoff * 2))
        return None

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
            with contextlib.suppress(Exception):
                body = page.inner_text("body").lower()
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
                # The stop count is encoded in the card's flight-details testid
                # (flight-details-N-stops-K). This is the only reliable signal:
                # connecting cards render "Multiple flights" and never the word
                # "stop" nor a flight number, so text heuristics miss them.
                stops = self._stops(card, n)
                if stops is None:
                    tl = text.lower()
                    if "multiple flights" in tl or "nonstop" not in tl:
                        continue
                elif stops > 0:
                    continue
            flights = list(dict.fromkeys(
                f.replace(" ", "") for f in _FLIGHT.findall(text)))
            dur = self._duration(text)

            tiles = self._tiles(card, n)
            if not tiles:
                continue

            # Positional labelling is only safe when the tiles and the headers
            # are the same length. When they are not, some column (Saver, on
            # the grids that produced this repo's data) is not a valuetile at
            # all, every label shifts, and the recorded "Main" is really the
            # column above it.
            if len(tiles) != len(brands):
                raise RuntimeError(
                    f"fare grid mismatch on card {n}: {len(brands)} brand "
                    f"headers {brands} but {len(tiles)} price tiles at indices "
                    f"{sorted(tiles)}. Refusing to guess which price is which "
                    f"brand. Run `python -m src.alaska --dump` to inspect.")

            for (idx, price), brand in zip(sorted(tiles.items()), brands, strict=True):
                del idx
                if price is not None:
                    fares.append(_Fare(brand, price, flights, dur))
        return fares

    def _tiles(self, card, n: int) -> dict[int, float | None]:
        """Price tiles on a card, keyed by their actual column index.

        Reading the indices out of the DOM rather than assuming 0..len(brands)
        is what makes the mismatch above detectable.
        """
        found: dict[int, float | None] = {}
        for tile in card.query_selector_all(f'[data-testid^="valuetile-{n}-"]'):
            m = re.search(rf"valuetile-{n}-(\d+)$", tile.get_attribute("data-testid") or "")
            if m:
                found[int(m.group(1))] = self._price(tile.inner_text())
        return found

    @staticmethod
    def _stops(card, n: int) -> int | None:
        """Stop count from the flight-details-{n}-stops-{k} testid, or None."""
        det = card.query_selector(f'[data-testid^="flight-details-{n}-stops-"]')
        if det is None:
            return None
        m = re.search(r"-stops-(\d+)$", det.get_attribute("data-testid") or "")
        return int(m.group(1)) if m else None

    @staticmethod
    def _brand_order(page) -> list[str]:
        """Brand columns in DOM order.

        No fallback. The previous version returned KNOWN_BRANDS when no header
        matched, which meant an unreadable grid was labelled by assumption
        rather than by observation. That is how every price in this repo came
        to be one column too high: labels were assigned positionally to tiles
        that did not line up with them, and nothing complained.
        """
        order = []
        for h in page.query_selector_all('[data-testid^="columnheader-"]'):
            name = (h.get_attribute("data-testid") or "").split("columnheader-")[-1]
            if name in KNOWN_BRANDS:
                order.append(name)
        if not order:
            raise RuntimeError(
                "no recognisable brand column headers found. The grid markup "
                "has changed; refusing to guess the column order.")
        return order

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
