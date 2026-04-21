import requests

BASE_URL = "https://api.sncf.com/v1"

class SncfNavitiaClient:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def get(self, path: str, params: dict | None = None) -> dict:
        if not path.startswith("/"):
            path = "/" + path

        url = f"{BASE_URL}{path}"
        r = requests.get(url, params=params, auth=(self.api_key, ""), timeout=25)

        # 🔥 IMPORTANT : ne plus faire r.raise_for_status()
        if r.status_code >= 400:
            return {
                "_error": True,
                "status_code": r.status_code,
                "url": r.url,
                "text": r.text[:800],
            }

        return r.json()

    def places(self, q: str) -> dict:
        return self.get("/coverage/sncf/places", {"q": q})

    def journeys(self, from_id, to_id, datetime_str, count=10, datetime_represents="departure"):
        return self.get("/coverage/sncf/journeys", {
         "from": from_id,
         "to": to_id,
          "datetime": datetime_str,
         "count": count,
         "datetime_represents": datetime_represents,
         "data_freshness": "realtime",
    })

    def stop_areas_near(self, lat: float, lon: float, distance_m: int = 50000, count: int = 30) -> dict:
        return self.get("/coverage/sncf/stop_areas", {
            "coord": f"{lon};{lat}",
            "distance": distance_m,
            "count": count
        })