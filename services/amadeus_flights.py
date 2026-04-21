# services/amadeus_flights.py
from __future__ import annotations

import time
import requests
from datetime import datetime
from typing import Any


class AmadeusClient:
    """
    Amadeus Self-Service API
    - OAuth2 client credentials -> access token
    - Reference Data (airports near coords)
    - Flight Offers Search (offers for airport->airport on a date)
    """

    def __init__(self, client_id: str, client_secret: str, environment: str = "test"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.environment = environment.lower().strip()
        self.base_url = (
            "https://test.api.amadeus.com" if self.environment != "production"
            else "https://api.amadeus.com"
        )

        self._token: str | None = None
        self._token_exp: float = 0.0  # epoch seconds

    def _get_token(self) -> str:
        # cache token in-memory
        now = time.time()
        if self._token and now < (self._token_exp - 30):
            return self._token

        url = f"{self.base_url}/v1/security/oauth2/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        r = requests.post(url, data=data, timeout=20)
        r.raise_for_status()
        j = r.json()
        self._token = j["access_token"]
        expires_in = int(j.get("expires_in", 1800))
        self._token_exp = now + expires_in
        return self._token

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        token = self._get_token()
        url = f"{self.base_url}{path}"
        r = requests.get(url, params=params, headers={"Authorization": f"Bearer {token}"}, timeout=25)
        r.raise_for_status()
        return r.json()

    # -------- Airports near coords (IMPORTANT: gives you IATA) --------
    def airports_near(self, lat: float, lon: float, radius_km: int = 200, max_results: int = 10) -> list[dict]:
        """
        Return list like:
        [
          {"iata":"TLS","name":"TOULOUSE BLAGNAC AIRPORT","lat":..., "lon":...},
          ...
        ]
        """
        params = {
            "latitude": float(lat),
            "longitude": float(lon),
            "radius": int(radius_km),
            "page[limit]": int(max_results),
            "sort": "relevance",
        }
        j = self._get("/v1/reference-data/locations/airports", params=params)
        out: list[dict] = []
        for it in (j.get("data") or []):
            iata = it.get("iataCode")
            name = it.get("name") or it.get("detailedName") or iata
            geo = (it.get("geoCode") or {})
            alat = geo.get("latitude")
            alon = geo.get("longitude")
            if not iata or alat is None or alon is None:
                continue
            out.append({
                "iata": iata,
                "name": name,
                "lat": float(alat),
                "lon": float(alon),
            })
        return out

    # -------- Flight offers search --------
    def flight_offers(self, origin_iata: str, dest_iata: str, date_yyyy_mm_dd: str, adults: int = 1, max_offers: int = 6) -> list[dict]:
        """
        Normalized offers:
        [
          {"dep_dt": datetime, "arr_dt": datetime, "duration_min": int, "stops": int, "price": float|None, "currency": str|None},
          ...
        ]
        """
        params = {
            "originLocationCode": origin_iata,
            "destinationLocationCode": dest_iata,
            "departureDate": date_yyyy_mm_dd,
            "adults": int(adults),
            "max": int(max_offers),
            "currencyCode": "EUR",
        }
        j = self._get("/v2/shopping/flight-offers", params=params)

        offers = []
        for off in (j.get("data") or []):
            # first itinerary = outbound
            itins = off.get("itineraries") or []
            if not itins:
                continue
            itin = itins[0]
            segs = itin.get("segments") or []
            if not segs:
                continue

            dep = segs[0].get("departure", {}).get("at")
            arr = segs[-1].get("arrival", {}).get("at")
            if not dep or not arr:
                continue

            dep_dt = datetime.fromisoformat(dep.replace("Z", "+00:00"))
            arr_dt = datetime.fromisoformat(arr.replace("Z", "+00:00"))

            stops = max(0, len(segs) - 1)

            # duration like "PT1H20M"
            dur = (itin.get("duration") or "")
            duration_min = _iso8601_duration_to_min(dur)

            price = None
            currency = None
            pr = off.get("price") or {}
            if pr.get("total"):
                try:
                    price = float(pr["total"])
                    currency = pr.get("currency") or "EUR"
                except Exception:
                    pass

            offers.append({
                "dep_dt": dep_dt,
                "arr_dt": arr_dt,
                "duration_min": duration_min,
                "stops": stops,
                "price": price,
                "currency": currency,
            })

        # order by duration then price
        offers.sort(key=lambda x: (x["duration_min"] if x["duration_min"] is not None else 10**9,
                                  x["price"] if x["price"] is not None else 10**9))
        return offers


def _iso8601_duration_to_min(s: str) -> int:
    # supports formats like PT2H10M, PT55M
    if not s or not s.startswith("PT"):
        return 0
    s = s[2:]
    hours = 0
    mins = 0
    num = ""
    for ch in s:
        if ch.isdigit():
            num += ch
        else:
            if ch == "H" and num:
                hours = int(num)
            if ch == "M" and num:
                mins = int(num)
            num = ""
    return hours * 60 + mins