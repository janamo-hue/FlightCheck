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


class Amadeus:
    def __init__(self, key: str | None = None, secret: str | None = None, host: str | None = None):
        self.key = key or os.environ["AMADEUS_CLIENT_ID"]
        self.secret = secret or os.environ["AMADEUS_CLIENT_SECRET"]
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
    ) -> Offer | None:
        """Return the cheapest matching offer, or None if the route has no service."""
        params = {
            "originLocationCode": origin,
            "destinationLocationCode": destination,
            "departureDate": depart,
            "adults": adults,
            "currencyCode": currency,
            "travelClass": cabin,
            "nonStop": "true" if nonstop else "false",
            "includedAirlineCodes": carrier,
            "max": 5,
        }
        if ret:
            params["returnDate"] = ret

        data = self._get("/v2/shopping/flight-offers", params)
        offers = data.get("data") or []
        if not offers:
            return None

        best = min(offers, key=lambda o: float(o["price"]["grandTotal"]))
        return _parse_offer(best)

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

    return Offer(
        price=float(raw["price"]["grandTotal"]),
        currency=raw["price"]["currency"],
        carrier=",".join(sorted(carriers)),
        flight_numbers=numbers,
        duration=duration,
        seats_left=raw.get("numberOfBookableSeats"),
    )
