# app.py
import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
from datetime import datetime, time, timedelta
import requests
import datetime as dt
import folium
from streamlit_folium import st_folium
import re

# services externes (garde tes modules)
from services.sncf_navitia import SncfNavitiaClient
from services.geocoding import geocode_google
from services.routing_car import drive_google
from services.amadeus_flights import AmadeusClient


# ----------------------------
# Page
# ----------------------------
st.set_page_config(page_title="Trajets multimodaux (MVP)", layout="wide")
st.title("MVP — Trajets multimodaux (Train/TC + Voiture + Vol)")

# ----------------------------
# Secrets
# ----------------------------
sncf_key = st.secrets.get("SNCF_API_KEY", "")
g_key = st.secrets.get("GOOGLE_MAPS_API_KEY", "")

amadeus_id = st.secrets.get("AMADEUS_CLIENT_ID", "")
amadeus_secret = st.secrets.get("AMADEUS_CLIENT_SECRET", "")
amadeus_env = st.secrets.get("AMADEUS_ENV", "test")  # "test" ou "production"

if not sncf_key:
    st.error("Ajoute SNCF_API_KEY dans .streamlit/secrets.toml")
    st.stop()

if not g_key:
    st.error("Ajoute GOOGLE_MAPS_API_KEY dans .streamlit/secrets.toml")
    st.stop()

client = SncfNavitiaClient(sncf_key)

amadeus = None
if amadeus_id and amadeus_secret:
    amadeus = AmadeusClient(amadeus_id, amadeus_secret, environment=amadeus_env)

# ----------------------------
# Helpers (SNCF)
# ----------------------------
def _weekday_idx_fr(d: dt.date | None = None) -> int:
    # Lundi=0 ... Dimanche=6 (comme Python)
    if d is None:
        d = dt.date.today()
    return d.weekday()

def _extract_today_hours(weekday_text: list[str] | None) -> str:
    """
    weekday_text typique:
    ["Monday: 9:00 AM – 6:00 PM", ...]
    ou en FR selon locale.
    """
    if not weekday_text:
        return "Horaires indisponibles"
    i = _weekday_idx_fr()
    if 0 <= i < len(weekday_text):
        return weekday_text[i]
    return "Horaires indisponibles"


@st.cache_data(show_spinner=False)
def google_places_search_nearby(
    api_key: str,
    lat: float,
    lon: float,
    radius_m: int,
    included_types: list[str],
    max_results: int = 20,
) -> dict:
    """
    (Ancienne tentative avec nouveau endpoint Places - conservée si besoin)
    Google Places API (New) - Nearby Search via POST.
    """
    url = "https://places.googleapis.com/v1/places:searchNearby"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": ",".join([
            "places.id",
            "places.displayName",
            "places.location",
            "places.formattedAddress",
            "places.types",
            "places.currentOpeningHours",
            "places.regularOpeningHours",
        ])
    }
    body = {
        "locationRestriction": {
            "circle": {
                "center": {"latitude": float(lat), "longitude": float(lon)},
                "radius": float(radius_m),
            }
        },
        "includedTypes": included_types,
        "maxResultCount": int(max_results),
        "languageCode": "fr",
    }

    try:
        r = requests.post(url, headers=headers, json=body, timeout=20)
    except Exception as e:
        return {"_error": True, "message": str(e)}

    try:
        data = r.json()
    except Exception:
        data = {"_error": True, "status_code": r.status_code, "text": r.text}

    if r.status_code >= 400:
        data["_error"] = True
        data["status_code"] = r.status_code

    return data

# ----------------------------
# Legacy Places Nearby (fallback simple et robuste)
# ----------------------------
@st.cache_data(show_spinner=False)
def google_places_nearby_legacy(api_key: str, lat: float, lon: float, radius_m: int, type_: str = "car_rental", max_results: int = 20) -> dict:
    """
    Endpoint classique Places Nearby Search (JSON). Retourne le JSON brut.
    """
    url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
    params = {
        "key": api_key,
        "location": f"{lat},{lon}",
        "radius": int(radius_m),
        "type": type_,
        "language": "fr",
    }
    try:
        r = requests.get(url, params=params, timeout=20)
    except Exception as e:
        return {"_error": True, "_exception": str(e)}

    try:
        j = r.json()
    except Exception:
        return {"_error": True, "status_code": r.status_code, "text": r.text}

    if r.status_code >= 400:
        j["_error"] = True
        j["_status_code"] = r.status_code
    return j

def get_car_rentals_nearby_legacy(api_key: str, lat: float, lon: float, radius_km: int, max_results: int = 20) -> list[dict]:
    data = google_places_nearby_legacy(api_key, lat, lon, int(radius_km * 1000), type_="car_rental", max_results=max_results)
    if isinstance(data, dict) and data.get("_error"):
        return []

    out = []
    for p in (data.get("results") or [])[:max_results]:
        place_id = p.get("place_id")  # ✅ ici (dans la boucle)

        name = p.get("name") or "Agence"
        geom = p.get("geometry", {}).get("location", {})
        plat = geom.get("lat")
        plon = geom.get("lng")
        addr = p.get("vicinity") or p.get("formatted_address") or ""

        oh = p.get("opening_hours") or {}
        open_now = oh.get("open_now", None)
        weekday_text = oh.get("weekday_text") or []

        today_hours = ""
        if weekday_text:
            try:
                today_hours = weekday_text[datetime.today().weekday()]
            except Exception:
                today_hours = weekday_text[0] if weekday_text else ""

        try:
            plat = float(plat) if plat is not None else None
            plon = float(plon) if plon is not None else None
        except Exception:
            plat = plon = None

        if plat is None or plon is None:
            continue

        out.append({
            "type": "car_rental",
            "place_id": place_id,   # ✅ ici
            "name": name,
            "lat": plat,
            "lon": plon,
            "address": addr,
            "open_now": open_now,
            "today_hours": today_hours,
            "weekday_text": weekday_text,
        })

    return out

# ----------------------------
# Misc helpers
# ----------------------------
def is_probably_address(s: str) -> bool:
    s = (s or "").lower()
    return any(ch.isdigit() for ch in s) and ("rue" in s or "avenue" in s or "boulevard" in s or "bd" in s or "route" in s or "impasse" in s or "chemin" in s)

def resolve_departure_to_stop_area(
    from_text: str,
    g_key: str,
    client: SncfNavitiaClient,
    fallback_radius_m: int = 15000,
):
    """
    Retourne:
      from_id (stop_area:...), dep_lat, dep_lon, dep_label, debug_msg
    """
    try:
        from_places = client.places(from_text)
    except Exception as e:
        from_places = {"_error": True, "message": str(e)}

    if isinstance(from_places, dict) and not from_places.get("_error"):
        cand = pick_best_stop_area_id(from_places) or pick_best_place_id(from_places)
        if cand and str(cand).startswith("stop_area:"):
            dep_geo = geocode_google(from_text, g_key)
            if dep_geo.get("ok"):
                return cand, dep_geo["lat"], dep_geo["lon"], dep_geo["label"], "Départ résolu via SNCF places."
            return cand, None, None, from_text, "Départ résolu via SNCF places (sans coord Google)."

    dep_geo = geocode_google(from_text, g_key)
    if not dep_geo.get("ok"):
        return None, None, None, None, f"Départ introuvable: SNCF places KO et géocodage Google KO ({dep_geo.get('error')})"

    dep_lat, dep_lon = dep_geo["lat"], dep_geo["lon"]
    dep_label = dep_geo["label"]

    sp_json = stop_points_near(dep_lat, dep_lon, fallback_radius_m, count=400)
    if sp_json.get("_error"):
        return None, dep_lat, dep_lon, dep_label, "Départ géocodé, mais stop_points_near a échoué."

    stop_points = sp_json.get("stop_points", []) or []
    best_sa = None

    for sp in stop_points:
        sa = sp.get("stop_area") or {}
        sa_id = sa.get("id", "")
        if sa_id.startswith("stop_area:"):
            best_sa = sa_id
            break

    if not best_sa:
        return None, dep_lat, dep_lon, dep_label, "Départ géocodé, mais aucune gare SNCF trouvée autour."

    return best_sa, dep_lat, dep_lon, dep_label, "Départ résolu via gare la plus proche (depuis adresse)."

def to_navitia_datetime(d: datetime) -> str:
    return d.strftime("%Y%m%dT%H%M%S")

def pick_best_place_id(places_json: dict) -> str | None:
    places = places_json.get("places", [])
    if not places:
        return None
    return places[0].get("id")

def pick_best_stop_area_id(places_json: dict) -> str | None:
    places = places_json.get("places", [])
    if not places:
        return None
    for p in places:
        pid = p.get("id", "")
        if pid.startswith("stop_area:"):
            return pid
    return places[0].get("id")

def fmt_navitia_hhmm(dt_str: str | None) -> str:
    if not dt_str:
        return ""
    return datetime.strptime(dt_str, "%Y%m%dT%H%M%S").strftime("%H:%M")

def fmt_navitia(dt_str: str | None) -> str:
    if not dt_str:
        return ""
    return datetime.strptime(dt_str, "%Y%m%dT%H%M%S").strftime("%Y-%m-%d %H:%M")

def section_label(sec: dict) -> str:
    di = sec.get("display_informations") or {}
    mode = (di.get("commercial_mode") or di.get("physical_mode") or "").strip()
    line = (di.get("label") or di.get("code") or "").strip()
    headsign = (di.get("headsign") or "").strip()
    bits = []
    if mode:
        bits.append(mode)
    if line:
        bits.append(line)
    if headsign and headsign not in bits:
        bits.append(headsign)
    return " ".join(bits).strip()

def google_maps_dir_link(o_lat, o_lon, d_lat, d_lon) -> str:
    return f"https://www.google.com/maps/dir/?api=1&origin={o_lat},{o_lon}&destination={d_lat},{d_lon}"

def slugify_simple(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")

def sncf_connect_link(from_city: str, to_city: str) -> str:
    return f"https://www.sncf-connect.com/train/trajet/{slugify_simple(from_city)}/{slugify_simple(to_city)}"

def safe_get_stop_area_coord(stop_area_id: str):
    detail = client.get(f"/coverage/sncf/stop_areas/{stop_area_id}")
    if not isinstance(detail, dict) or detail.get("_error"):
        return None, None
    sas = detail.get("stop_areas", [])
    if not sas:
        return None, None
    c = sas[0].get("coord") or {}
    if isinstance(c, dict):
        return c.get("lat"), c.get("lon")
    return None, None

def parse_float(x, default=None):
    try:
        return float(x)
    except Exception:
        return default

def extract_city_from_geocode_label(label: str) -> str:
    parts = [p.strip() for p in label.split(",") if p.strip()]
    for p in parts:
        m = re.match(r"^\d{4,5}\s+(.+)$", p)
        if m:
            return m.group(1).strip()
    if len(parts) >= 2:
        return parts[1]
    return parts[0] if parts else "Ville"

def stop_points_near(lat: float, lon: float, distance_m: int, count: int = 800) -> dict:
    # ATTENTION ordre: lon;lat
    url = f"/coverage/sncf/coords/{lon};{lat}/stop_points?distance={distance_m}&count={count}"
    return client.get(url)

def clean_station_name(name: str) -> str:
    # "Lavaur (Lavaur)" -> "Lavaur"
    return re.sub(r"\s*\([^)]*\)\s*$", "", (name or "")).strip()

@st.cache_data(show_spinner=False)
def get_full_stop_area_name(stop_area_id: str) -> str:
    d = client.get(f"/coverage/sncf/stop_areas/{stop_area_id}")
    if not isinstance(d, dict) or d.get("_error"):
        return stop_area_id
    sas = d.get("stop_areas", [])
    if not sas:
        return stop_area_id
    sa = sas[0]
    return sa.get("label") or sa.get("commercial_name") or sa.get("name") or stop_area_id

def journeys_with_offsets(from_id: str, to_id: str, base_dt: datetime, count: int = 10, mode: str = "departure") -> dict:
    last = None
    offsets_h = [0, 2, 4, 6]
    for offset_h in offsets_h:
        dt_try = base_dt + timedelta(hours=offset_h) if mode == "departure" else base_dt - timedelta(hours=offset_h)
        j = client.journeys(
            from_id,
            to_id,
            to_navitia_datetime(dt_try),
            count=count,
            datetime_represents=mode,
        )
        last = j
        if isinstance(j, dict) and j.get("_error"):
            continue
        if len(j.get("journeys", [])) > 0:
            return j
    return last or {"journeys": []}

@st.cache_data(show_spinner=False)
def google_place_details_legacy(api_key: str, place_id: str) -> dict:
    url = "https://maps.googleapis.com/maps/api/place/details/json"
    params = {
        "key": api_key,
        "place_id": place_id,
        "fields": "opening_hours",   # minimal pour ne pas être refusé sur des champs
        "language": "fr",
    }
    r = requests.get(url, params=params, timeout=20)
    try:
        return r.json()
    except Exception:
        return {"status": "PARSING_ERROR", "text": r.text}
 
def filter_major_car_rentals(rentals: list[dict]) -> list[dict]:
    """
    Garde uniquement les grandes enseignes connues.
    """
    allowed_brands = ["avis", "europcar", "enterprise", "hertz", "sixt"]

    filtered = []
    for r in rentals:
        name = (r.get("name") or "").lower()
        if any(brand in name for brand in allowed_brands):
            filtered.append(r)

    return filtered

def extract_legs_and_changes(journey: dict):
    sections = journey.get("sections", []) or []
    pt_sections = [s for s in sections if s.get("type") == "public_transport"]

    legs = []
    changes = []

    for idx, sec in enumerate(pt_sections):
        from_name = (sec.get("from") or {}).get("name", "")
        to_name = (sec.get("to") or {}).get("name", "")
        dep = fmt_navitia_hhmm(sec.get("departure_date_time"))
        arr = fmt_navitia_hhmm(sec.get("arrival_date_time"))

        label = section_label(sec)
        if label:
            legs.append(f"{dep} {from_name} → {arr} {to_name} ({label})")
        else:
            legs.append(f"{dep} {from_name} → {arr} {to_name}")

        if idx < len(pt_sections) - 1 and to_name:
            changes.append(to_name)

    return legs, changes

def add_car_rentals_to_map(m: folium.Map, car_rentals: list[dict]):
    for a in car_rentals:
        name = a.get("name", "Agence")
        lat = float(a["lat"])
        lon = float(a["lon"])
        addr = a.get("address", "")
        open_now = a.get("open_now", None)

        weekday_text = a.get("weekday_text") or []
        if weekday_text:
            week_html = "<br/>".join(weekday_text)
        else:
            week_html = "<i>Horaires semaine indisponibles</i>"

        status = "🟢 Ouvert" if open_now is True else ("🔴 Fermé" if open_now is False else "⚪ Statut inconnu")

        popup_html = (
            f"<b>🚗 {name}</b><br/>"
            f"{addr}<br/>"
            f"{status}<hr style='margin:6px 0;'/>"
            f"{week_html}"
        )

        folium.Marker(
            [lat, lon],
            tooltip=f"🚗 {name}",
            popup=folium.Popup(popup_html, max_width=380),
            icon=folium.Icon(icon="info-sign"),
        ).add_to(m)

# ----------------------------
# MAP helpers
# ----------------------------
def build_map(arr_lat: float, arr_lon: float, points: list[dict], threshold_km: float) -> folium.Map:
    """
    points elements:
      {
        "kind": "station"|"airport",
        "name": str,
        "lat": float,
        "lon": float,
        "car_km": float
      }
    """
    m = folium.Map(location=[arr_lat, arr_lon], zoom_start=7, control_scale=True)

    # Destination
    folium.Marker(
        [arr_lat, arr_lon],
        tooltip="Destination",
        icon=folium.Icon(color="blue", icon="flag"),
    ).add_to(m)

    folium.Circle(
        location=[arr_lat, arr_lon],
        radius=float(threshold_km) * 1000.0,
        color="#888",
        weight=2,
        fill=False,
        tooltip=f"Seuil {threshold_km:.0f} km",
    ).add_to(m)

    for p in points:
        kind = p.get("kind", "station")
        name = p.get("name", "Point")
        lat = float(p["lat"])
        lon = float(p["lon"])
        km = float(p.get("car_km", 0.0))

        if kind == "airport":
            popup = folium.Popup(f"<b>✈️ {name}</b><br/>Voiture → destination: {km:.1f} km", max_width=300)
            folium.Marker(
                [lat, lon],
                tooltip=f"✈️ {name} — {km:.1f} km",
                popup=popup,
                icon=folium.Icon(color="purple", icon="plane", prefix="fa"),
            ).add_to(m)
        else:
            color = "red" if km > threshold_km else "green"
            popup = folium.Popup(f"<b>🚆 {name}</b><br/>Voiture → destination: {km:.1f} km", max_width=300)
            folium.CircleMarker(
                location=[lat, lon],
                radius=6,
                color=color,
                fill=True,
                fill_opacity=0.9,
                popup=popup,
                tooltip=f"🚆 {name} — {km:.1f} km",
            ).add_to(m)

    return m

def render_map_with_fallback(m: folium.Map, key: str, height: int = 450):
    try:
        out = st_folium(m, height=height, use_container_width=True, key=key)
        if out is None:
            components.html(m._repr_html_(), height=height)
    except Exception as e:
        st.warning(f"Affichage carte via st_folium a échoué: {e}. Fallback HTML.")
        components.html(m._repr_html_(), height=height)

# ----------------------------
# Session state init
# ----------------------------
if "result" not in st.session_state:
    st.session_state["result"] = None

# ----------------------------
# UI (FORM)
# ----------------------------
with st.form("search"):
    col1, col2, col3 = st.columns(3)
    from_text = col1.text_input("Point de départ (ville/gare)", "Paris Gare de Lyon")
    to_text = col2.text_input("Point d’arrivée (adresse/lieu ou ville)", "69 chemin de la fontaine 81130 Cagnac les mines")
    date_val = col3.date_input("Date", datetime.now().date())

    col4, col5, col6 = st.columns(3)
    use_arrival_time = col4.checkbox("Fixer une heure d’arrivée ?", value=False)
    arrival_time_val = None
    if use_arrival_time:
        arrival_time_val = col4.time_input("Heure d’arrivée", time(18, 0))

    radius_km = col5.selectbox("Rayon gares (km) [fallback TER]", [30, 60, 100], index=1)
    max_car_km = col6.selectbox("Max km voiture (dernier tronçon)", [30, 60, 100, 150, 999], index=4)

    col7, col8, col9, col10 = st.columns(4)
    criterion = col7.selectbox("Critère", ["Plus rapide", "Moins de changements"])
    max_transfers = col8.selectbox("Max changements (train)", [0, 1, 2], index=2)
    max_pivot_km = col9.selectbox("Max km pivot → destination", [30, 60, 100, 150, 200], index=3)

    show_map = col7.checkbox("Afficher la carte", value=True)
    map_threshold_km = col9.slider("Seuil carte (km)", min_value=10, max_value=300, value=int(max_pivot_km), step=10)
    include_car_rentals = col7.checkbox("Inclure agences de location (Google Places)", value=True)
    car_rentals_radius_km = col9.slider("Rayon agences (km)", min_value=5, max_value=80, value=25, step=5)
    st.markdown("### ✈️ Vols (Amadeus)")
    flights_enabled = st.checkbox("Inclure vols (Vol + voiture)", value=bool(amadeus))
    flights_radius_km = st.slider("Rayon aéroports (km)", min_value=50, max_value=400, value=250, step=25)
    flights_max_airports = st.slider("Nb max aéroports à tester (par côté)", min_value=1, max_value=10, value=5, step=1)
    flights_max_offers = st.slider("Nb max offres par paire d'aéroports", min_value=1, max_value=10, value=4, step=1)
    flights_wait_min = st.slider("Marge aéroport (min) (sécurité/check-in)", min_value=30, max_value=180, value=90, step=10)

    submit = col10.form_submit_button("Chercher")

# ----------------------------
# Main compute on submit
# ----------------------------
if submit:
    st.caption(f"🔧 Debug: max_transfers sélectionné = {max_transfers}")

    # Mode SNCF
    search_mode = "arrival" if arrival_time_val else "departure"

    # Base datetime (train)
    if arrival_time_val:
        base_dt = datetime.combine(date_val, arrival_time_val)
    else:
        if date_val == datetime.now().date():
            now_t = datetime.now().time().replace(second=0, microsecond=0)
            base_dt = datetime.combine(date_val, now_t)
        else:
            base_dt = datetime.combine(date_val, time(9, 0))

    date_str = date_val.strftime("%Y-%m-%d")

    # 1) Géocoder arrivée
    with st.spinner("Géocodage de l’arrivée…"):
        geo = geocode_google(to_text, g_key)
        if not geo.get("ok"):
            st.error(f"Géocodage impossible: {geo.get('error')} {geo.get('message','')}")
            st.stop()
        arr_lat, arr_lon = geo["lat"], geo["lon"]
        car_rentals = []
        if include_car_rentals:
            with st.spinner("Recherche agences de location (Google Places)…"):
                # utilise le fallback legacy (plus simple à déboguer)
                car_rentals = get_car_rentals_nearby_legacy(
                    api_key=g_key,
                    lat=arr_lat, lon=arr_lon,
                    radius_km=int(car_rentals_radius_km),
                    max_results=30,
                )
                def enrich_car_rentals_hours(api_key: str, rentals: list[dict], limit: int = 12) -> list[dict]:
                    enriched = []
                    for i, a in enumerate(rentals):
                        a2 = dict(a)

                        pid = a.get("place_id")
                        if (not pid) or (i >= limit):
            # on garde tel quel
                            enriched.append(a2)
                            continue
                        d = google_place_details_legacy(api_key, pid)
                        if i < 3:
                            st.write("DEBUG details status =", d.get("status"))
                            st.write("DEBUG details error_message =", d.get("error_message"))
                            st.write("DEBUG details keys result =", list((d.get("result") or {}).keys()))
                        if not isinstance(d, dict) or d.get("status") != "OK":
                            enriched.append(a2)
                            continue

                        res = d.get("result", {}) or {}
                        oh = (d.get("result") or {}).get("opening_hours") or {}
                        weekday_text = oh.get("weekday_text") or []

        # ✅ Horaires semaine
                        weekday_text = oh.get("weekday_text") or []
             
        # ✅ Open now (parfois présent aussi)
                        open_now = oh.get("open_now", a2.get("open_now"))

        # ✅ Ligne du jour (optionnel)
                        today_hours = ""
                        if weekday_text:
                            try:
                                 today_hours = weekday_text[datetime.today().weekday()]
                            except Exception:
                                 today_hours = weekday_text[0]

                        a2["open_now"] = open_now
                        a2["weekday_text"] = weekday_text
                        a2["today_hours"] = today_hours or a2.get("today_hours", "")
                        enriched.append(a2)

                    return enriched
                car_rentals = enrich_car_rentals_hours(g_key, car_rentals, limit=12)
                car_rentals = filter_major_car_rentals(car_rentals)
                st.write("DEBUG filtered count =", len(car_rentals))
                st.write("DEBUG place_id sample =", [a.get("place_id") for a in car_rentals[:3]])
                st.write("DEBUG today_hours sample =", [a.get("today_hours") for a in car_rentals[:3]])

                # DEBUG: afficher la réponse brute (legacy)
                raw_places = google_places_nearby_legacy(g_key, arr_lat, arr_lon, int(car_rentals_radius_km * 1000), type_="car_rental", max_results=30)
                st.write("DEBUG raw_places status:", raw_places.get("status") if isinstance(raw_places, dict) else None)
                st.write("DEBUG raw_places error:", raw_places.get("error_message") if isinstance(raw_places, dict) else None)
                st.write("DEBUG: nb car_rentals raw =", len(car_rentals) if car_rentals is not None else None)
                st.write("DEBUG: sample car_rental (0..3) =", (car_rentals[:3] if isinstance(car_rentals, list) else car_rentals))

                # fallback test markers so tu vois la carte même si Google retourne rien
                 

            st.caption(f"Agences de location (Google): {len(car_rentals)}")

    with st.spinner("Résolution du départ (gare ou adresse)…"):
         from_id, dep_lat, dep_lon, dep_label, dep_msg = resolve_departure_to_stop_area(from_text, g_key, client)

    if not from_id:
        st.error("Départ introuvable (train). Essaye un nom de gare (ex: 'Paris Gare de Lyon') ou une autre adresse.")
        st.info(dep_msg)
        st.stop()

    st.success(dep_msg)
    st.caption(f"ID départ utilisé : `{from_id}`")
    if dep_label and dep_lat is not None:
        st.caption(f"Départ géocodé : {dep_label} ({dep_lat:.5f}, {dep_lon:.5f})")

    arr_lat, arr_lon = geo["lat"], geo["lon"]
    geo_label = geo["label"]
    st.success(f"Arrivée géocodée : {geo_label} ({arr_lat:.5f}, {arr_lon:.5f})")

    city = extract_city_from_geocode_label(geo_label)
    st.caption(f"Ville détectée : **{city}**")

    # 1bis) Géocoder départ + voiture directe
    with st.spinner("Géocodage départ + voiture directe…"):
        dep_geo = geocode_google(from_text, g_key)

    dep_lat = dep_lon = None
    if dep_geo.get("ok"):
        dep_lat, dep_lon = dep_geo["lat"], dep_geo["lon"]
        car_direct = drive_google(dep_lat, dep_lon, arr_lat, arr_lon, g_key)
    else:
        car_direct = {"ok": False}
        if dep_lat is not None and dep_lon is not None:
            car_direct = drive_google(dep_lat, dep_lon, arr_lat, arr_lon, g_key)

    if car_direct.get("ok"):
        st.info(f"🚗 Voiture directe: {car_direct['duration_min']} min — {car_direct['distance_km']} km")
    else:
        st.warning("Impossible de calculer la voiture directe (géocodage départ ou Directions).")

    # 2) Résoudre départ SNCF (train)
    with st.spinner("Résolution du départ (gare ou adresse)…"):
        from_id, dep_lat, dep_lon, dep_label, dep_msg = resolve_departure_to_stop_area(from_text, g_key, client)

    if not from_id:
         st.error("Départ introuvable (train). Essaye un nom de gare (ex: 'Paris Gare de Lyon') ou une autre adresse.")
         st.info(dep_msg)
         st.stop()
    st.success(dep_msg)
    st.caption(f"Départ utilisé (SNCF): `{from_id}`")
    if dep_label and dep_lat is not None:
        st.caption(f"Départ géocodé: {dep_label} ({dep_lat:.5f}, {dep_lon:.5f})")

    # 3) Candidates gares (hubs + TER via stop_points)
    hub_queries = [
        f"{city} gare",
        f"{city} SNCF",
        "Albi",
        "Gare d'Albi",
        "Carmaux",
        "Gare de Carmaux",
        "Toulouse Matabiau",
    ]

    hub_candidates = []
    with st.spinner("Recherche hubs gares…"):
        for q in hub_queries:
            pj = client.places(q)
            if pj.get("_error"):
                continue
            sid = pick_best_stop_area_id(pj)
            if sid and sid.startswith("stop_area:"):
                hub_candidates.append({"id": sid, "name": q, "coord": None})

    ter_candidates = []
    stop_points = []
    with st.spinner("Recherche gares TER proches via stop_points…"):
        sp_json = stop_points_near(arr_lat, arr_lon, int(radius_km * 1000), count=800)

    if not sp_json.get("_error"):
        stop_points = sp_json.get("stop_points", []) or []
        tmp = {}
        for sp in stop_points:
            sa = sp.get("stop_area") or {}
            sa_id = sa.get("id", "")
            if not sa_id.startswith("stop_area:"):
                continue
            if sa_id not in tmp:
                full_name = get_full_stop_area_name(sa_id)
                tmp[sa_id] = {
                    "id": sa_id,
                    "name": clean_station_name(full_name),
                    "coord": sa.get("coord", None),
                }
        ter_candidates = list(tmp.values())

    station_candidates = hub_candidates + ter_candidates

    # 4) Aéroports Amadeus (origin + destination)
    airports_origin = []
    airports_dest = []
    if flights_enabled:
        if not amadeus:
            st.warning("Clés Amadeus manquantes. Ajoute AMADEUS_CLIENT_ID / AMADEUS_CLIENT_SECRET.")
            flights_enabled = False
        elif dep_lat is None or dep_lon is None:
            st.warning("Départ non géocodé, impossible de chercher les aéroports côté départ.")
            flights_enabled = False
        else:
            with st.spinner("Recherche aéroports (Amadeus)…"):
                try:
                    airports_origin = amadeus.airports_near(dep_lat, dep_lon, radius_km=int(flights_radius_km), max_results=int(flights_max_airports))
                    airports_dest = amadeus.airports_near(arr_lat, arr_lon, radius_km=int(flights_radius_km), max_results=int(flights_max_airports))
                except Exception as e:
                    st.warning(f"Erreur Amadeus aéroports: {e}")
                    flights_enabled = False

    # stats debug
    st.info(
        f"Hubs connus: {len(hub_candidates)} — "
        f"Gares TER via stop_points: {len(ter_candidates)} — "
        f"Aéroports (Amadeus): {len(airports_origin) + len(airports_dest)} — "
        f"Total gares: {len(station_candidates)}"
    )

    # dédup gares par id
    seen = set()
    station_candidates = [x for x in station_candidates if x.get("id") and not (x["id"] in seen or seen.add(x["id"]))]

    if not station_candidates and not flights_enabled:
        st.warning("Aucune gare candidate et vols désactivés.")
        st.stop()

    # 5) Calcul des combinaisons
    rows = []
    points_for_map = []

    tested_stations = 0
    skipped_api = 0
    skipped_nojourney = 0
    skipped_car = 0
    skipped_no_coord = 0
    filtered_by_km = 0
    filtered_by_transfers = 0
    filtered_by_pivot_distance = 0

    # ---- A) Train + Voiture
    with st.spinner("Calcul Train + Voiture…"):
        for sa in station_candidates[:200]:
            tested_stations += 1
            sa_id = sa.get("id")
            sa_name = sa.get("name", "Gare")

            # coord gare
            sa_lat = None
            sa_lon = None
            coord = sa.get("coord")
            if isinstance(coord, dict):
                sa_lat = coord.get("lat")
                sa_lon = coord.get("lon")
            if sa_lat is None or sa_lon is None:
                sa_lat, sa_lon = safe_get_stop_area_coord(sa_id)
            if sa_lat is None or sa_lon is None:
                skipped_no_coord += 1
                continue

            # Voiture last-mile (gare -> destination)
            car = drive_google(sa_lat, sa_lon, arr_lat, arr_lon, g_key)
            if not car.get("ok"):
                skipped_car += 1
                continue

            car_km = parse_float(car.get("distance_km"), default=999999)
            car_min = int(parse_float(car.get("duration_min"), default=999999))

            # map point station
            points_for_map.append({
                "kind": "station",
                "name": clean_station_name(sa_name),
                "lat": float(sa_lat),
                "lon": float(sa_lon),
                "car_km": float(car_km),
            })

            # filtres voiture
            if car_km > float(max_pivot_km):
                filtered_by_pivot_distance += 1
                continue
            if max_car_km != 999 and car_km > float(max_car_km):
                filtered_by_km += 1
                continue

            # Journeys SNCF
            jjson = journeys_with_offsets(from_id, sa_id, base_dt, count=10, mode=search_mode)
            if isinstance(jjson, dict) and jjson.get("_error"):
                skipped_api += 1
                continue

            js = jjson.get("journeys", []) or []
            if not js:
                skipped_nojourney += 1
                continue

            # filtrer par transferts
            js_ok = [j for j in js if int(j.get("nb_transfers") or 0) <= int(max_transfers)]
            if not js_ok:
                filtered_by_transfers += 1
                continue

            # rows (on garde TOUTES les possibilités)
            for j in js_ok:
                transfers = int(j.get("nb_transfers") or 0)
                duration_min = round((j.get("duration") or 0) / 60)

                legs, changes = extract_legs_and_changes(j)

                margin_min = 10
                total_min = int(duration_min) + int(car_min) + margin_min

                rows.append({
                    "Mode": "Train + Voiture",
                    "Pivot": clean_station_name(sa_name),
                    "Départ": fmt_navitia(j.get("departure_date_time")),
                    "Arrivée": fmt_navitia(j.get("arrival_date_time")),
                    "PT (min)": duration_min,
                    "Changements": transfers,
                    "Voiture 1 (min)": None,
                    "Voiture 1 (km)": None,
                    "Voiture 2 (min)": car_min,
                    "Voiture 2 (km)": round(float(car_km), 1),
                    "Marge (min)": margin_min,
                    "Total (min)": total_min,
                    "Prix": None,
                    "Devise": None,
                    "Lien principal": sncf_connect_link(from_text, sa_name),
                    "Trajet (détails)": "\n".join(legs) if legs else "",
                    "Correspondances": " ; ".join(changes) if changes else "Direct",
                })

    # ---- B) Vol + Voiture (Amadeus)
    tested_pairs = 0
    if flights_enabled and airports_origin and airports_dest:
        with st.spinner("Calcul Vol + Voiture (Amadeus)…"):
            # map points airports: distance airport -> destination (car2)
            for ad in airports_dest:
                car2 = drive_google(ad["lat"], ad["lon"], arr_lat, arr_lon, g_key)
                if not car2.get("ok"):
                    continue
                car2_km = float(parse_float(car2.get("distance_km"), 999999))
                points_for_map.append({
                    "kind": "airport",
                    "name": f"{ad['name']} ({ad['iata']})",
                    "lat": float(ad["lat"]),
                    "lon": float(ad["lon"]),
                    "car_km": car2_km,
                })

            # rows flights
            for ao in airports_origin:
                car1 = drive_google(dep_lat, dep_lon, ao["lat"], ao["lon"], g_key)
                if not car1.get("ok"):
                    continue
                car1_min = int(parse_float(car1.get("duration_min"), 999999))
                car1_km = float(parse_float(car1.get("distance_km"), 999999))

                for ad in airports_dest:
                    tested_pairs += 1
                    car2 = drive_google(ad["lat"], ad["lon"], arr_lat, arr_lon, g_key)
                    if not car2.get("ok"):
                        continue
                    car2_min = int(parse_float(car2.get("duration_min"), 999999))
                    car2_km = float(parse_float(car2.get("distance_km"), 999999))

                    # filtre pivot -> destination (sur car2)
                    if car2_km > float(max_pivot_km):
                        continue

                    try:
                        offers = amadeus.flight_offers(
                            ao["iata"], ad["iata"], date_str,
                            adults=1, max_offers=int(flights_max_offers)
                        )
                    except Exception:
                        continue

                    for off in offers:
                        total_min = (
                            car1_min
                            + int(flights_wait_min)
                            + int(off["duration_min"])
                            + car2_min
                        )

                        rows.append({
                            "Mode": "Vol + Voiture",
                            "Pivot": f"{ao['name']} ({ao['iata']}) → {ad['name']} ({ad['iata']})",
                            "Départ": off["dep_dt"].strftime("%Y-%m-%d %H:%M"),
                            "Arrivée": off["arr_dt"].strftime("%Y-%m-%d %H:%M"),
                            "PT (min)": int(off["duration_min"]),
                            "Changements": int(off["stops"]),
                            "Voiture 1 (min)": car1_min,
                            "Voiture 1 (km)": round(car1_km, 1),
                            "Voiture 2 (min)": car2_min,
                            "Voiture 2 (km)": round(car2_km, 1),
                            "Marge (min)": int(flights_wait_min),
                            "Total (min)": int(total_min),
                            "Prix": off.get("price"),
                            "Devise": off.get("currency"),
                            "Lien principal": "",
                            "Trajet (détails)": f"✈️ {ao['iata']}→{ad['iata']} ({off['stops']} escales)",
                            "Correspondances": f"{off['stops']} escales" if off["stops"] else "Direct",
                        })

    # Debug
    st.caption(
        f"Train testées: {tested_stations} — "
        f"API SNCF erreurs: {skipped_api} — "
        f"Sans journeys: {skipped_nojourney} — "
        f"Filtrées (changements>{max_transfers}): {filtered_by_transfers} — "
        f"Filtrées (pivot>{max_pivot_km}km): {filtered_by_pivot_distance} — "
        f"Sans coord: {skipped_no_coord} — "
        f"Voiture KO: {skipped_car} — "
        f"Filtrées (max km voiture): {filtered_by_km} — "
        f"Paires aéroports testées: {tested_pairs}"
    )

    if not rows:
        st.warning("Aucune combinaison trouvée.")
        st.stop()

    df = pd.DataFrame(rows)

    # tri
    if criterion == "Plus rapide":
        df = df.sort_values(["Total (min)", "Changements", "Voiture 2 (km)"], ascending=True)
    else:
        df = df.sort_values(["Changements", "Total (min)", "Voiture 2 (km)"], ascending=True)

    st.session_state["result"] = {
        "df": df,
        "arr_lat": arr_lat,
        "arr_lon": arr_lon,
        "points_for_map": points_for_map,
        "car_rentals": car_rentals,
    }

    st.success("Résultats calculés ✅")

# ----------------------------
# Render persisted results
# ----------------------------
res = st.session_state.get("result")
if res:
    df = res["df"]

    if show_map:
        st.subheader("🗺️ Carte (gares vs aéroports)")
        m = build_map(res["arr_lat"], res["arr_lon"], res["points_for_map"], float(map_threshold_km))
        car_rentals = res.get("car_rentals") or []
        if car_rentals:
            st.subheader("🚗 Agences de location proches")
            df_ag = pd.DataFrame([{
                 "Agence": a["name"],
                 "Adresse": a.get("address", ""),
                "Ouvert maintenant": a.get("open_now"),
                 "Horaires aujourd’hui": a.get("today_hours", ""),
                "Horaires semaine": "\n".join(a.get("weekday_text") or []),
            } for a in car_rentals])

            st.dataframe(df_ag, use_container_width=True)
        add_car_rentals_to_map(m, car_rentals)
        render_map_with_fallback(m, key="map_mix", height=520)

        st.caption("Légende: 🚆 gares (vert/rouge selon seuil) — ✈️ aéroports (violet)")

    st.subheader("Top itinéraires (Multimodal)")
    st.dataframe(df, use_container_width=True)

    best_row = df.iloc[0].to_dict()
    st.markdown("### ✅ Meilleure option")
    st.write(f"**Mode:** {best_row.get('Mode')}")
    st.write(f"**Pivot:** {best_row.get('Pivot')}")
    st.write(f"**Départ:** {best_row.get('Départ')} — **Arrivée:** {best_row.get('Arrivée')}")
    st.write(f"**Total:** {best_row.get('Total (min)')} min — **PT:** {best_row.get('PT (min)')} min — **Changements:** {best_row.get('Changements')}")

    if best_row.get("Prix") is not None:
        st.write(f"**Prix:** {best_row.get('Prix')} {best_row.get('Devise') or ''}")

    if best_row.get("Trajet (détails)"):
        st.markdown("### Détails")
        for line in str(best_row["Trajet (détails)"]).split("\n"):
            st.write("•", line)