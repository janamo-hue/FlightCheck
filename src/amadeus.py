"""Thin Amadeus Self-Service client: OAuth2 + Flight Offers Search.

Only the pieces this project needs. No SDK dependency.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

PROD_HOST = "https://api.amadeus.com"
TEST_HOST = "https://test.api.amadeus.com"

# Amadeus test env allows 1 request per 100ms; prod is more generous.
MIN_INTERVAL_S = 0.25


class QuotaExceeded(RuntimeError):
    """Raised when Amadeus reports the monthly quota is spent."""


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
    saver_price: float | None = None      # cheapest Saver seen, for comparison
    savers_excluded: int = 0

    @property
    def is_saver(self) -> bool:
        """Saver fares earn zero Atmos points on tickets issued from June 2026.

        The cheapest offer on a route is very often the Saver, so the fare the
        scanner tracks is frequently the one that earns nothing. Matching is
        deliberately loose because the exact branded-fare string Amadeus
        returns for Alaska has not been verified against a live response;
        `python -m src.doctor` reports the strings it actually sees.
        """
        return bool(self.branded_fare and "SAVER" in self.branded_fare.upper())


class Amadeus:
    def __init__(self, key: str | None = None, secret: str | None = None, host: str | None = None):
        self.key = key or os.environ.get("AMADEUS_CLIENT_ID", "")
        self.secret = secret or os.environ.get("AMADEUS_CLIENT_SECRET", "")
        # An unset GitHub secret arrives as an empty string, not as an absent
        # variable, so os.environ[...] would happily hand back "" and the
        # failure would surface later as a confusing auth or DNS error.
        missing = [n for n, v in (("AMADEUS_CLIENT_ID", self.key),
                                  ("AMADEUS_CLIENT_SECRET", self.secret)) if not v]
        if missing:
            raise RuntimeError(
                f"{' and '.join(missing)} is empty or unset. Add it under "
                f"Settings > Secrets and variables > Actions.")
        if host:
            self.host = host
        else:
            self.host = TEST_HOST if os.environ.get("AMADEUS_ENV") == "test" else PROD_HOST
        self._token: str | None = None
        self._token_expires: float = 0.0
        self._last_call: float = 0.0
        self.calls_made = 0
        self.session = requests.Session()

    # ---------------------------------------------------------------- auth

    def _auth(self) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token

        resp = self.session.post(
            f"{self.host}/v1/security/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.key,
                "client_secret": self.secret,
            },
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expires = time.time() + int(payload.get("expires_in", 1799))
        return self._token

    # --------------------------------------------------------------- search

    def cheapest_direct(
        self,
        origin: str,
        destination: str,
        depart: str,
        ret: str | None = None,
        *,
        carrier: str = "AS",
        adults: int = 1,
        currency: str = "USD",
        cabin: str = "ECONOMY",
        nonstop: bool = True,
        exclude_saver: bool = False,
        max_offers: int = 20,
    ) -> Offer | None:
        """Cheapest matching offer, or None if the route has no service.

        Saver exclusion happens here rather than through a request parameter.
        Amadeus offers no branded-fare filter on this endpoint, and the fare
        rule flags that come closest (noPenaltyFare and friends) are on the
        POST variant and would filter on restrictions rather than on the
        brand itself. Since a single call already returns many offers, the
        cheapest non-Saver can be picked from what we have for free.

        Offers with no brand label are kept. Dropping them would silently
        empty out any route where Amadeus omits brandedFare, which is exactly
        the invisible failure the doctor exists to catch.
        """
        params = {
            "originLocationCode": origin,
            "destinationLocationCode": destination,
            "departureDate": depart,
            "adults": adults,
            "currencyCode": currency,
            "travelClass": cabin,
            "nonStop": "true" if nonstop else "false",
            "includedAirlineCodes": carrier,
        }
        if ret:
            params["returnDate"] = ret

        params["max"] = max_offers
        data = self._get("/v2/shopping/flight-offers", params)
        raw = data.get("data") or []
        if not raw:
            return None

        parsed = sorted((_parse_offer(o) for o in raw), key=lambda o: o.price)
        savers = [o for o in parsed if o.is_saver]
        saver_price = savers[0].price if savers else None

        pool = [o for o in parsed if not o.is_saver] if exclude_saver else parsed
        if not pool:
            # Every offer was a Saver. Report nothing rather than silently
            # falling back to a fare that earns no points.
            return None

        best = pool[0]
        best.saver_price = saver_price
        best.savers_excluded = len(savers) if exclude_saver else 0
        return best

    # ----------------------------------------------------------------- http

    def _get(self, path: str, params: dict) -> dict:
        elapsed = time.time() - self._last_call
        if elapsed < MIN_INTERVAL_S:
            time.sleep(MIN_INTERVAL_S - elapsed)

        token = self._auth()
        for attempt in range(3):
            resp = self.session.get(
                f"{self.host}{path}",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=45,
            )
            self._last_call = time.time()

            if resp.status_code == 429:
                wait = 2 ** attempt
                log.warning("rate limited, sleeping %ss", wait)
                time.sleep(wait)
                continue

            if resp.status_code == 200:
                self.calls_made += 1
                return resp.json()

            body = resp.text[:500]
            if resp.status_code == 400 and "INVALID" in body.upper():
                # Unserved route or malformed date pair. Not worth retrying.
                log.info("400 for %s: %s", params, body)
                self.calls_made += 1
                return {"data": []}
            if "quota" in body.lower() or resp.status_code == 429:
                raise QuotaExceeded(body)

            resp.raise_for_status()

        raise RuntimeError("exhausted retries on Amadeus request")


def _parse_offer(raw: dict) -> Offer:
    segments = [
        seg
        for itin in raw.get("itineraries", [])
        for seg in itin.get("segments", [])
    ]
    numbers = [f"{s['carrierCode']}{s['number']}" for s in segments]
    carriers = {s["carrierCode"] for s in segments}
    duration = raw["itineraries"][0].get("duration") if raw.get("itineraries") else None

    # Branded fare lives under traveller pricing, one entry per segment. The
    # outbound segment is what determines the brand for our purposes.
    branded = fare_basis = None
    pricings = raw.get("travelerPricings") or []
    if pricings:
        details = pricings[0].get("fareDetailsBySegment") or []
        if details:
            branded = details[0].get("brandedFare")
            fare_basis = details[0].get("fareBasis")

    return Offer(
        price=float(raw["price"]["grandTotal"]),
        currency=raw["price"]["currency"],
        carrier=",".join(sorted(carriers)),
        flight_numbers=numbers,
        duration=duration,
        seats_left=raw.get("numberOfBookableSeats"),
        branded_fare=branded,
        fare_basis=fare_basis,
    )
