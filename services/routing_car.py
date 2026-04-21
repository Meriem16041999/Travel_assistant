import requests

def drive_google(origin_lat: float, origin_lon: float, dest_lat: float, dest_lon: float, api_key: str) -> dict:
    url = "https://maps.googleapis.com/maps/api/directions/json"
    params = {
        "origin": f"{origin_lat},{origin_lon}",
        "destination": f"{dest_lat},{dest_lon}",
        "key": api_key,
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()

    if data.get("status") != "OK" or not data.get("routes"):
        return {"ok": False, "error": data.get("status", "NO_ROUTE")}

    leg = data["routes"][0]["legs"][0]
    return {
        "ok": True,
        "duration_min": round(leg["duration"]["value"] / 60),
        "distance_km": round(leg["distance"]["value"] / 1000, 1),
    }