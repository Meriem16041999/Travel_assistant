import requests

def geocode_google(address: str, api_key: str) -> dict:
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": address, "key": api_key}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()

    status = data.get("status", "")
    if status != "OK" or not data.get("results"):
        return {
            "ok": False,
            "error": status or "NO_RESULT",
            "message": data.get("error_message", ""),
        }

    res = data["results"][0]
    loc = res["geometry"]["location"]
    return {
        "ok": True,
        "label": res.get("formatted_address", address),
        "lat": float(loc["lat"]),
        "lon": float(loc["lng"]),
    }