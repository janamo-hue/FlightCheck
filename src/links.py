"""Shared booking-link builders, used by both alerts (notify) and the report.

Kept in one place so the two callers cannot drift: a change to the Google
Flights query phrasing or the Alaska route-page format applies everywhere.
"""

from __future__ import annotations

from urllib.parse import urlencode


def google_flights_url(origin: str, destination: str, depart: str,
                       ret: str | None = None) -> str:
    """Google Flights search pinned to the exact dates.

    The only source here that can be pinned to a specific departure and return
    from a plain natural-language query.
    """
    query = f"Flights from {origin} to {destination} on {depart}"
    if ret:
        query += f" through {ret}"
    query += " nonstop"
    return "https://www.google.com/travel/flights?" + urlencode({"q": query})


def alaska_route_url(cities: tuple[str, str] | None) -> str | None:
    """Alaska's own route page (a fare calendar), or None without city slugs.

    Verified to resolve. Not date-specific, so it complements the dated Google
    Flights link rather than replacing it.
    """
    if not cities:
        return None
    origin, destination = cities
    return f"https://www.alaskaair.com/en/flights-from-{origin}-to-{destination}"
