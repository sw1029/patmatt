import configparser
import html
import importlib.metadata
import math
import os
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent
STEP_LENGTH_M = 0.72
SUPPORTED_PLACE_TYPES = {
    "convenience_store",
    "supermarket",
    "grocery_store",
    "market",
    "park",
}
PLACE_TYPE_LABELS = {
    "convenience_store": "편의점",
    "supermarket": "마트",
    "grocery_store": "식료품점",
    "market": "시장",
    "park": "공원",
}
WEATHER_CODES_KO = {
    0: "맑음",
    1: "대체로 맑음",
    2: "부분적으로 흐림",
    3: "흐림",
    45: "안개",
    48: "서리 안개",
    51: "약한 이슬비",
    53: "이슬비",
    55: "강한 이슬비",
    61: "약한 비",
    63: "비",
    65: "강한 비",
    71: "약한 눈",
    73: "눈",
    75: "강한 눈",
    80: "약한 소나기",
    81: "소나기",
    82: "강한 소나기",
    95: "뇌우",
    96: "우박 동반 뇌우",
    99: "강한 우박 동반 뇌우",
}


app = FastAPI(title="1인가구 최소동선 산책/장보기 데모")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class RecommendationRequest(BaseModel):
    lat: float
    lng: float
    radius: int = Field(default=1200, ge=250, le=3000)
    target_minutes: int = Field(default=35, ge=5, le=180)
    max_steps: int = Field(default=4500, ge=300, le=30000)
    min_sunlight: float = Field(default=0.35, ge=0, le=1)
    required_types: list[str] = Field(
        default_factory=lambda: ["supermarket", "convenience_store", "park"]
    )
    max_candidates_per_type: int = Field(default=4, ge=1, le=10)
    experimental3d: bool = False


def load_google_maps_api_key() -> str:
    env_key = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
    if env_key and env_key != "YOUR_API_KEY_HERE":
        return env_key

    ini_path = BASE_DIR / "api_key.ini"
    if not ini_path.exists():
        return ""

    parser = configparser.ConfigParser()
    raw = ini_path.read_text(encoding="utf-8").strip()
    if raw and not raw.startswith("["):
        raw = "[google]\n" + raw
    parser.read_string(raw)

    for section in parser.sections():
        for key_name in ("GOOGLE_MAPS_API_KEY", "google_maps_api_key", "key", "api_key"):
            value = parser.get(section, key_name, fallback="").strip()
            if value and value != "YOUR_API_KEY_HERE":
                return value
    return ""


def get_google_maps_api_key() -> str:
    return load_google_maps_api_key()


def ensure_api_key() -> str:
    api_key = get_google_maps_api_key()
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail=(
                "Google Maps API 키가 설정되어 있지 않습니다. "
                "환경변수 GOOGLE_MAPS_API_KEY 또는 api_key.ini의 key 값을 설정하세요."
            ),
        )
    return api_key


def _dependency_version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def _parse_duration_seconds(duration_text: str | None) -> int | None:
    if not duration_text:
        return None
    if duration_text.endswith("s"):
        number = duration_text[:-1]
        if number.isdigit():
            return int(number)
    return None


def _meters_to_steps(distance_m: int | float | None) -> int | None:
    if distance_m is None:
        return None
    return max(0, round(float(distance_m) / STEP_LENGTH_M))


def _encode_polyline(points: list[dict[str, float]]) -> str:
    result: list[str] = []
    prev_lat = 0
    prev_lng = 0

    for point in points:
        lat = int(round(point["lat"] * 1e5))
        lng = int(round(point["lng"] * 1e5))
        for value in (lat - prev_lat, lng - prev_lng):
            shifted = value << 1
            if value < 0:
                shifted = ~shifted
            while shifted >= 0x20:
                result.append(chr((0x20 | (shifted & 0x1F)) + 63))
                shifted >>= 5
            result.append(chr(shifted + 63))
        prev_lat = lat
        prev_lng = lng

    return "".join(result)


def _haversine_m(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    radius_m = 6_371_000
    phi1 = math.radians(a_lat)
    phi2 = math.radians(b_lat)
    d_phi = math.radians(b_lat - a_lat)
    d_lambda = math.radians(b_lng - a_lng)
    hav = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * radius_m * math.atan2(math.sqrt(hav), math.sqrt(1 - hav))


def _bearing_deg(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    phi1 = math.radians(a_lat)
    phi2 = math.radians(b_lat)
    d_lambda = math.radians(b_lng - a_lng)
    y = math.sin(d_lambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(d_lambda)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _angle_diff_deg(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _parse_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now().astimezone()
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="datetime은 ISO-8601 형식이어야 합니다. 예: 2026-05-09T14:30:00+09:00",
        ) from exc
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed


def _sun_position(lat: float, lng: float, at: datetime) -> dict[str, Any]:
    local = at.astimezone()
    day_of_year = local.timetuple().tm_yday
    hour = local.hour + local.minute / 60 + local.second / 3600
    gamma = 2 * math.pi / 365 * (day_of_year - 1 + (hour - 12) / 24)

    equation_of_time = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    declination = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )

    utc_offset_hours = local.utcoffset().total_seconds() / 3600 if local.utcoffset() else 0
    time_offset = equation_of_time + 4 * lng - 60 * utc_offset_hours
    true_solar_time = (hour * 60 + time_offset) % 1440
    hour_angle = true_solar_time / 4 - 180
    hour_angle_rad = math.radians(hour_angle)
    lat_rad = math.radians(lat)

    cos_zenith = (
        math.sin(lat_rad) * math.sin(declination)
        + math.cos(lat_rad) * math.cos(declination) * math.cos(hour_angle_rad)
    )
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    zenith = math.degrees(math.acos(cos_zenith))
    elevation = 90 - zenith

    azimuth = (
        math.degrees(
            math.atan2(
                math.sin(hour_angle_rad),
                math.cos(hour_angle_rad) * math.sin(lat_rad)
                - math.tan(declination) * math.cos(lat_rad),
            )
        )
        + 180
    ) % 360

    daylight_score = 0 if elevation <= 0 else _clamp(elevation / 45)
    return {
        "datetime": local.isoformat(),
        "azimuthDeg": round(azimuth, 2),
        "elevationDeg": round(elevation, 2),
        "isDaylight": elevation > 0,
        "daylightScore": round(daylight_score, 3),
    }


def _weather_walkability(current: dict[str, Any]) -> dict[str, Any]:
    temp = current.get("temperature_2m")
    precipitation = current.get("precipitation") or 0
    rain = current.get("rain") or 0
    wind = current.get("wind_speed_10m") or 0
    code = current.get("weather_code")

    score = 1.0
    reasons: list[str] = []
    if isinstance(temp, (int, float)):
        if temp < -5 or temp > 34:
            score -= 0.45
            reasons.append("기온 부담")
        elif temp < 0 or temp > 30:
            score -= 0.2
            reasons.append("기온 주의")
    if precipitation > 0.5 or rain > 0.5:
        score -= 0.35
        reasons.append("비")
    if wind > 35:
        score -= 0.25
        reasons.append("강풍")
    if code in {95, 96, 99}:
        score -= 0.7
        reasons.append("뇌우")
    elif code in {65, 75, 82}:
        score -= 0.35
        reasons.append("강수 강함")

    score = _clamp(score)
    return {
        "score": round(score, 3),
        "walkable": score >= 0.55,
        "summary": "산책 가능" if score >= 0.55 else "산책 주의",
        "reasons": reasons,
    }


def _fetch_weather(lat: float, lng: float) -> dict[str, Any]:
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lng,
        "current": ",".join(
            [
                "temperature_2m",
                "relative_humidity_2m",
                "precipitation",
                "rain",
                "weather_code",
                "cloud_cover",
                "wind_speed_10m",
                "is_day",
            ]
        ),
        "timezone": "auto",
    }
    try:
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="날씨 API 호출에 실패했습니다.") from exc

    data = response.json()
    current = data.get("current") or {}
    code = current.get("weather_code")
    walkability = _weather_walkability(current)
    return {
        "source": "Open-Meteo",
        "lat": lat,
        "lng": lng,
        "timezone": data.get("timezone"),
        "current": current,
        "conditionKo": WEATHER_CODES_KO.get(code, "알 수 없음"),
        "walkability": walkability,
    }


def _sunlight_score_for_segment(
    start: dict[str, float],
    end: dict[str, float],
    sun: dict[str, Any],
    cloud_cover: float = 0,
) -> float:
    if not sun.get("isDaylight"):
        return 0
    bearing = _bearing_deg(start["lat"], start["lng"], end["lat"], end["lng"])
    diff = _angle_diff_deg(bearing, float(sun["azimuthDeg"]))
    alignment = 0.55 + 0.45 * max(0, math.cos(math.radians(diff)))
    elevation_factor = _clamp(float(sun["elevationDeg"]) / 45)
    cloud_factor = _clamp(1 - (cloud_cover / 100) * 0.7)
    return round(_clamp(alignment * elevation_factor * cloud_factor), 3)


def _estimate_path_sunlight(points: list[dict[str, float]], sun: dict[str, Any], cloud_cover: float) -> float:
    if len(points) < 2:
        return 0
    total_distance = 0.0
    weighted = 0.0
    for start, end in zip(points, points[1:]):
        distance = _haversine_m(start["lat"], start["lng"], end["lat"], end["lng"])
        total_distance += distance
        weighted += distance * _sunlight_score_for_segment(start, end, sun, cloud_cover)
    if total_distance == 0:
        return 0
    return round(weighted / total_distance, 3)


def _normalize_place(place: dict[str, Any], requested_type: str | None = None) -> dict[str, Any]:
    location = place.get("location") or {}
    place_types = place.get("types") or []
    primary_type = place.get("primaryType")
    category = requested_type or primary_type
    if category not in SUPPORTED_PLACE_TYPES:
        category = next((item for item in place_types if item in SUPPORTED_PLACE_TYPES), "place")

    lat = location.get("latitude")
    lng = location.get("longitude")
    name = (place.get("displayName") or {}).get("text") or "이름 없음"
    return {
        "id": place.get("id") or place.get("name") or f"{name}:{lat}:{lng}",
        "name": name,
        "address": place.get("formattedAddress") or "주소 없음",
        "lat": lat,
        "lng": lng,
        "types": place_types,
        "primaryType": primary_type,
        "category": category,
        "categoryLabel": PLACE_TYPE_LABELS.get(category, "장소"),
    }


def _search_places_by_type(
    lat: float,
    lng: float,
    radius: int,
    place_type: str,
    max_result_count: int,
) -> list[dict[str, Any]]:
    api_key = ensure_api_key()
    if place_type not in SUPPORTED_PLACE_TYPES:
        raise HTTPException(status_code=400, detail=f"지원하지 않는 장소 타입입니다: {place_type}")

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": (
            "places.id,places.displayName,places.formattedAddress,"
            "places.location,places.types,places.primaryType"
        ),
    }
    body = {
        "includedTypes": [place_type],
        "maxResultCount": max(1, min(max_result_count, 20)),
        "rankPreference": "DISTANCE",
        "locationRestriction": {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": radius,
            }
        },
        "languageCode": "ko",
        "regionCode": "KR",
    }

    try:
        response = requests.post(
            "https://places.googleapis.com/v1/places:searchNearby",
            headers=headers,
            json=body,
            timeout=20,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        detail = "Places API 호출에 실패했습니다."
        if getattr(exc, "response", None) is not None:
            detail = f"{detail} {exc.response.status_code}: {exc.response.text[:300]}"
        raise HTTPException(status_code=502, detail=detail) from exc

    places = response.json().get("places") or []
    normalized = [_normalize_place(place, place_type) for place in places]
    return [
        item
        for item in normalized
        if isinstance(item.get("lat"), (int, float)) and isinstance(item.get("lng"), (int, float))
    ]


def _search_places(
    lat: float,
    lng: float,
    radius: int,
    place_types: list[str],
    max_result_count: int = 10,
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    all_items: list[dict[str, Any]] = []
    seen: set[str] = set()

    for place_type in place_types:
        items = _search_places_by_type(lat, lng, radius, place_type, max_result_count)
        grouped[place_type] = items
        for item in items:
            key = str(item["id"])
            if key in seen:
                continue
            seen.add(key)
            all_items.append(item)

    return {
        "count": len(all_items),
        "items": all_items,
        "grouped": grouped,
    }


def _normalize_route(route: dict[str, Any]) -> dict[str, Any]:
    duration_text = route.get("duration")
    distance_m = route.get("distanceMeters")
    duration_s = _parse_duration_seconds(duration_text)
    return {
        "distanceMeters": distance_m,
        "durationText": duration_text,
        "durationSeconds": duration_s,
        "durationMinutes": round(duration_s / 60, 1) if duration_s else None,
        "steps": _meters_to_steps(distance_m),
        "encodedPolyline": ((route.get("polyline") or {}).get("encodedPolyline")),
        "legs": route.get("legs") or [],
    }


def _fallback_walk_route(points: list[dict[str, float]]) -> dict[str, Any]:
    distance = 0.0
    legs: list[dict[str, Any]] = []
    for start, end in zip(points, points[1:]):
        leg_distance = _haversine_m(start["lat"], start["lng"], end["lat"], end["lng"]) * 1.25
        leg_duration = round(leg_distance / 1.15)
        distance += leg_distance
        legs.append(
            {
                "distanceMeters": round(leg_distance),
                "duration": f"{leg_duration}s",
                "fallback": True,
            }
        )

    distance_m = round(distance)
    duration_s = round(distance / 1.15)
    return {
        "distanceMeters": distance_m,
        "durationText": f"{duration_s}s",
        "durationSeconds": duration_s,
        "durationMinutes": round(duration_s / 60, 1),
        "steps": _meters_to_steps(distance_m),
        "encodedPolyline": _encode_polyline(points),
        "legs": legs,
        "fallback": True,
    }


def _compute_walk_route(
    origin: dict[str, float],
    destination: dict[str, float],
    intermediates: list[dict[str, float]] | None = None,
) -> dict[str, Any]:
    api_key = ensure_api_key()
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": (
            "routes.duration,routes.distanceMeters,routes.polyline.encodedPolyline,"
            "routes.legs.duration,routes.legs.distanceMeters"
        ),
    }
    route_points = [origin] + (intermediates or []) + [destination]
    body: dict[str, Any] = {
        "origin": {"location": {"latLng": {"latitude": origin["lat"], "longitude": origin["lng"]}}},
        "destination": {
            "location": {
                "latLng": {"latitude": destination["lat"], "longitude": destination["lng"]}
            }
        },
        "travelMode": "WALK",
        "polylineQuality": "HIGH_QUALITY",
    }
    if intermediates:
        body["intermediates"] = [
            {"location": {"latLng": {"latitude": point["lat"], "longitude": point["lng"]}}}
            for point in intermediates
        ]

    try:
        response = requests.post(
            "https://routes.googleapis.com/directions/v2:computeRoutes",
            headers=headers,
            json=body,
            timeout=25,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        detail = "Routes API 호출에 실패했습니다."
        if getattr(exc, "response", None) is not None:
            detail = f"{detail} {exc.response.status_code}: {exc.response.text[:300]}"
        raise HTTPException(status_code=502, detail=detail) from exc

    routes = response.json().get("routes") or []
    if not routes:
        return _fallback_walk_route(route_points)
    return _normalize_route(routes[0])


def _nearest_neighbor_order(home: dict[str, float], nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    remaining = nodes[:]
    ordered: list[dict[str, Any]] = []
    cursor = home
    while remaining:
        remaining.sort(
            key=lambda node: _haversine_m(cursor["lat"], cursor["lng"], node["lat"], node["lng"])
        )
        cursor = remaining.pop(0)
        ordered.append(cursor)
    return ordered


def _route_label(nodes: list[dict[str, Any]], meets_all: bool) -> str:
    if meets_all:
        return "최소 에너지 코스"
    if any(node.get("category") == "park" for node in nodes):
        return "조건 근접 산책 코스"
    return "조건 근접 생활 코스"


def _score_recommendation(
    route: dict[str, Any],
    sunlight_score: float,
    weather_score: float,
    node_count: int,
    meets_all: bool,
) -> float:
    steps = route.get("steps") or 999_999
    effort_score = _clamp(1 - steps / 12_000)
    score = effort_score * 0.45 + weather_score * 0.2 + sunlight_score * 0.25 + min(node_count, 5) / 5 * 0.1
    if meets_all:
        score += 0.25
    return round(score, 4)


def _violations(
    route: dict[str, Any],
    sunlight_score: float,
    request: RecommendationRequest,
    weather_walkable: bool,
) -> list[str]:
    result: list[str] = []
    if route.get("durationSeconds") and route["durationSeconds"] > request.target_minutes * 60:
        result.append("목표 시간 초과")
    if route.get("steps") and route["steps"] > request.max_steps:
        result.append("최대 걸음 수 초과")
    if sunlight_score < request.min_sunlight:
        result.append("최소 일조량 미달")
    if not weather_walkable:
        result.append("날씨 주의")
    return result


def _make_shadow_lookup(
    lat: float,
    lng: float,
    radius: int,
    grid_size: int,
    at: datetime,
) -> dict[str, Any]:
    grid_size = max(3, min(grid_size, 20))
    weather = _fetch_weather(lat, lng)
    current = weather["current"]
    cloud_cover = float(current.get("cloud_cover") or 0)
    sun = _sun_position(lat, lng, at)
    meters_per_lat = 111_320
    meters_per_lng = max(1, 111_320 * math.cos(math.radians(lat)))
    cell_size = (radius * 2) / grid_size
    cells: list[dict[str, Any]] = []

    for row in range(grid_size):
        for col in range(grid_size):
            north_m = radius - (row + 0.5) * cell_size
            east_m = -radius + (col + 0.5) * cell_size
            cell_lat = lat + north_m / meters_per_lat
            cell_lng = lng + east_m / meters_per_lng
            pseudo_orientation = (math.degrees(math.atan2(east_m, north_m)) + 360) % 360
            diff = _angle_diff_deg(pseudo_orientation, sun["azimuthDeg"])
            alignment = 0.55 + 0.45 * max(0, math.cos(math.radians(diff)))
            density_seed = math.sin((row + 1) * 12.9898 + (col + 1) * 78.233) * 43758.5453
            urban_density = density_seed - math.floor(density_seed)
            daylight = float(sun["daylightScore"])
            cloud_factor = _clamp(1 - (cloud_cover / 100) * 0.7)
            sunlight = _clamp(daylight * cloud_factor * alignment * (1 - 0.35 * urban_density))
            half_lat = (cell_size / 2) / meters_per_lat
            half_lng = (cell_size / 2) / meters_per_lng
            cells.append(
                {
                    "row": row,
                    "col": col,
                    "lat": round(cell_lat, 7),
                    "lng": round(cell_lng, 7),
                    "north": round(cell_lat + half_lat, 7),
                    "south": round(cell_lat - half_lat, 7),
                    "east": round(cell_lng + half_lng, 7),
                    "west": round(cell_lng - half_lng, 7),
                    "sunlightScore": round(sunlight, 3),
                    "shadowScore": round(1 - sunlight, 3),
                }
            )

    return {
        "lat": lat,
        "lng": lng,
        "radius": radius,
        "gridSize": grid_size,
        "cellSizeMeters": round(cell_size, 1),
        "sun": sun,
        "weather": {
            "cloudCover": cloud_cover,
            "conditionKo": weather["conditionKo"],
        },
        "cells": cells,
        "experimental3d": {
            "enabled": False,
            "mode": "topdown_lookup_table",
            "note": "v1은 2D LUT 추정치입니다. 실제 Google Photorealistic 3D Tiles 렌더링은 후속 실험 플래그에서 연결합니다.",
        },
    }


def _build_recommendations(request: RecommendationRequest) -> dict[str, Any]:
    selected_types = [item for item in request.required_types if item in SUPPORTED_PLACE_TYPES]
    if not selected_types:
        selected_types = ["supermarket", "park"]

    home = {"lat": request.lat, "lng": request.lng}
    weather = _fetch_weather(request.lat, request.lng)
    sun = _sun_position(request.lat, request.lng, datetime.now().astimezone())
    cloud_cover = float((weather.get("current") or {}).get("cloud_cover") or 0)
    places = _search_places(
        request.lat,
        request.lng,
        request.radius,
        selected_types,
        max_result_count=request.max_candidates_per_type,
    )

    candidate_lists: list[list[dict[str, Any]]] = []
    missing_types: list[str] = []
    for place_type in selected_types:
        grouped_items = places["grouped"].get(place_type) or []
        if not grouped_items:
            missing_types.append(place_type)
            continue
        candidate_lists.append(grouped_items[: request.max_candidates_per_type])

    if not candidate_lists:
        raise HTTPException(status_code=404, detail="조건에 맞는 주변 장소를 찾지 못했습니다.")

    recommendations: list[dict[str, Any]] = []
    seen_node_sets: set[tuple[str, ...]] = set()
    combos = list(product(*candidate_lists))[:24]

    for combo in combos:
        unique_nodes: list[dict[str, Any]] = []
        seen_places: set[str] = set()
        for node in combo:
            if node["id"] in seen_places:
                continue
            seen_places.add(node["id"])
            unique_nodes.append(node)
        if not unique_nodes:
            continue
        signature = tuple(sorted(str(node["id"]) for node in unique_nodes))
        if signature in seen_node_sets:
            continue
        seen_node_sets.add(signature)

        ordered_nodes = _nearest_neighbor_order(home, unique_nodes)
        route_points = [home] + [{"lat": node["lat"], "lng": node["lng"]} for node in ordered_nodes] + [home]
        try:
            route = _compute_walk_route(home, home, route_points[1:-1])
        except HTTPException:
            continue

        sunlight_score = _estimate_path_sunlight(route_points, sun, cloud_cover)
        violation_list = _violations(
            route,
            sunlight_score,
            request,
            weather["walkability"]["walkable"],
        )
        meets_all = not violation_list and not missing_types
        score = _score_recommendation(
            route,
            sunlight_score,
            weather["walkability"]["score"],
            len(ordered_nodes),
            meets_all,
        )
        recommendations.append(
            {
                "title": _route_label(ordered_nodes, meets_all),
                "score": score,
                "meetsAllConditions": meets_all,
                "violations": violation_list,
                "missingTypes": missing_types,
                "route": route,
                "sunlightScore": sunlight_score,
                "weatherScore": weather["walkability"]["score"],
                "nodes": ordered_nodes,
                "orderedPoints": route_points,
            }
        )

    recommendations.sort(
        key=lambda item: (
            not item["meetsAllConditions"],
            item["route"].get("steps") or 999_999,
            -item["weatherScore"],
            -item["sunlightScore"],
            -item["score"],
        )
    )

    return {
        "input": request.model_dump(),
        "weather": weather,
        "sun": sun,
        "places": {
            "count": places["count"],
            "missingTypes": missing_types,
            "typeLabels": PLACE_TYPE_LABELS,
        },
        "recommendations": recommendations[:5],
    }


@app.get("/", response_class=HTMLResponse)
def demo_page() -> str:
    maps_key = html.escape(get_google_maps_api_key())
    return f"""
<!doctype html>
<html lang="ko">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>1인가구 최소동선 산책/장보기</title>
    <style>
        :root {{
            --bg: #f5f6f2;
            --panel: #ffffff;
            --panel-2: #f0f4ec;
            --text: #18201b;
            --muted: #657069;
            --line: #d5ddd1;
            --blue: #2668a6;
            --green: #2f7d4e;
            --yellow: #d99d22;
            --red: #b34545;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            font-family: "Segoe UI", "Noto Sans KR", Arial, sans-serif;
            color: var(--text);
            background: var(--bg);
        }}
        .app {{
            min-height: 100vh;
            display: grid;
            grid-template-columns: minmax(340px, 420px) 1fr;
        }}
        aside {{
            background: var(--panel);
            border-right: 1px solid var(--line);
            padding: 18px;
            overflow-y: auto;
        }}
        main {{
            min-width: 0;
            display: grid;
            grid-template-rows: 1fr;
        }}
        h1 {{
            margin: 0 0 4px;
            font-size: 22px;
            line-height: 1.25;
            letter-spacing: 0;
        }}
        .subtitle {{
            margin: 0 0 16px;
            color: var(--muted);
            font-size: 13px;
            line-height: 1.5;
        }}
        .section {{
            border: 1px solid var(--line);
            border-radius: 8px;
            padding: 12px;
            margin-bottom: 12px;
            background: #fff;
        }}
        .section-title {{
            margin: 0 0 10px;
            font-size: 14px;
            font-weight: 700;
        }}
        label {{
            display: block;
            color: var(--muted);
            font-size: 12px;
            margin-bottom: 5px;
        }}
        input[type="number"], input[type="text"] {{
            width: 100%;
            border: 1px solid var(--line);
            border-radius: 6px;
            padding: 9px 10px;
            font-size: 14px;
            background: #fff;
            color: var(--text);
        }}
        input[type="range"] {{
            width: 100%;
        }}
        .grid-2 {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
        }}
        .field {{
            margin-bottom: 10px;
        }}
        .range-row {{
            display: grid;
            grid-template-columns: 1fr auto;
            align-items: center;
            gap: 8px;
        }}
        .range-value {{
            min-width: 62px;
            text-align: right;
            color: var(--text);
            font-weight: 700;
            font-size: 13px;
        }}
        .checks {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
        }}
        .check {{
            display: flex;
            align-items: center;
            gap: 8px;
            min-height: 36px;
            border: 1px solid var(--line);
            border-radius: 6px;
            padding: 8px;
            color: var(--text);
            font-size: 13px;
        }}
        .actions {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
        }}
        button {{
            border: 0;
            border-radius: 6px;
            padding: 10px 12px;
            font-weight: 700;
            cursor: pointer;
            min-height: 40px;
        }}
        button:disabled {{
            opacity: 0.55;
            cursor: wait;
        }}
        .primary {{
            background: var(--green);
            color: #fff;
            width: 100%;
            margin-top: 8px;
        }}
        .secondary {{
            background: #e7eef5;
            color: #173b5d;
        }}
        .ghost {{
            background: var(--panel-2);
            color: #243127;
        }}
        #status {{
            min-height: 20px;
            margin: 10px 0 0;
            color: var(--muted);
            font-size: 13px;
            line-height: 1.45;
        }}
        #map {{
            width: 100%;
            height: 100vh;
        }}
        #results {{
            display: grid;
            gap: 8px;
        }}
        .result {{
            border: 1px solid var(--line);
            border-radius: 8px;
            background: #fff;
            padding: 11px;
            cursor: pointer;
        }}
        .result.active {{
            border-color: var(--green);
            box-shadow: inset 0 0 0 1px var(--green);
        }}
        .result h2 {{
            margin: 0 0 8px;
            font-size: 15px;
            line-height: 1.3;
        }}
        .badges {{
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
            margin-bottom: 8px;
        }}
        .badge {{
            border-radius: 999px;
            padding: 4px 8px;
            font-size: 12px;
            background: var(--panel-2);
            color: #314237;
        }}
        .badge.sun {{ background: #fff3cf; color: #6f4c00; }}
        .badge.warn {{ background: #f9e1df; color: #7c2929; }}
        .metrics {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 6px;
            color: var(--muted);
            font-size: 12px;
        }}
        .nodes {{
            margin-top: 8px;
            color: var(--text);
            font-size: 12px;
            line-height: 1.45;
        }}
        .empty {{
            color: var(--muted);
            font-size: 13px;
            line-height: 1.45;
        }}
        @media (max-width: 920px) {{
            .app {{
                grid-template-columns: 1fr;
            }}
            aside {{
                border-right: 0;
                border-bottom: 1px solid var(--line);
            }}
            #map {{
                height: 58vh;
            }}
        }}
    </style>
</head>
<body>
    <div class="app">
        <aside>
            <h1>1인가구 최소동선</h1>
            <p class="subtitle">장보기, 편의점, 산책, 날씨와 일조 조건을 한 번에 묶어 걷기 동선을 추천합니다.</p>

            <section class="section">
                <p class="section-title">출발 위치</p>
                <div class="grid-2">
                    <div class="field">
                        <label for="lat">위도</label>
                        <input id="lat" type="number" step="0.000001" value="37.566500" />
                    </div>
                    <div class="field">
                        <label for="lng">경도</label>
                        <input id="lng" type="number" step="0.000001" value="126.978000" />
                    </div>
                </div>
                <div class="actions">
                    <button class="secondary" id="btnCurrent" type="button">현재 위치</button>
                    <button class="ghost" id="btnMove" type="button">지도 이동</button>
                </div>
            </section>

            <section class="section">
                <p class="section-title">조건</p>
                <div class="field">
                    <div class="range-row">
                        <label for="radius">검색 반경</label>
                        <span class="range-value" id="radiusValue">1200m</span>
                    </div>
                    <input id="radius" type="range" min="500" max="3000" step="100" value="1200" />
                </div>
                <div class="field">
                    <div class="range-row">
                        <label for="targetMinutes">목표 산책 시간</label>
                        <span class="range-value" id="targetMinutesValue">35분</span>
                    </div>
                    <input id="targetMinutes" type="range" min="10" max="90" step="5" value="35" />
                </div>
                <div class="field">
                    <div class="range-row">
                        <label for="maxSteps">최대 걸음 수</label>
                        <span class="range-value" id="maxStepsValue">4500보</span>
                    </div>
                    <input id="maxSteps" type="range" min="1000" max="12000" step="500" value="4500" />
                </div>
                <div class="field">
                    <div class="range-row">
                        <label for="minSunlight">최소 일조량</label>
                        <span class="range-value" id="minSunlightValue">35%</span>
                    </div>
                    <input id="minSunlight" type="range" min="0" max="100" step="5" value="35" />
                </div>
                <div class="checks">
                    <label class="check"><input type="checkbox" name="nodeType" value="convenience_store" checked /> 편의점</label>
                    <label class="check"><input type="checkbox" name="nodeType" value="supermarket" checked /> 마트</label>
                    <label class="check"><input type="checkbox" name="nodeType" value="grocery_store" /> 식료품점</label>
                    <label class="check"><input type="checkbox" name="nodeType" value="market" /> 시장</label>
                    <label class="check"><input type="checkbox" name="nodeType" value="park" checked /> 공원</label>
                    <label class="check"><input id="experimental3d" type="checkbox" /> 3D 실험</label>
                </div>
                <button class="primary" id="btnRecommend" type="button">동선 추천</button>
                <p id="status">조건을 조정한 뒤 동선 추천을 눌러주세요.</p>
            </section>

            <section class="section">
                <p class="section-title">추천 결과</p>
                <div id="results">
                    <p class="empty">아직 추천 결과가 없습니다.</p>
                </div>
            </section>
        </aside>
        <main>
            <div id="map"></div>
        </main>
    </div>

    <script>
        let map;
        let centerMarker;
        let routePolyline;
        let nodeMarkers = [];
        let shadowRects = [];
        let recommendations = [];

        const labels = {{
            convenience_store: "편의점",
            supermarket: "마트",
            grocery_store: "식료품점",
            market: "시장",
            park: "공원"
        }};

        function setStatus(message) {{
            document.getElementById("status").textContent = message || "";
        }}

        function readCenter() {{
            return {{
                lat: Number(document.getElementById("lat").value),
                lng: Number(document.getElementById("lng").value)
            }};
        }}

        function selectedTypes() {{
            return Array.from(document.querySelectorAll('input[name="nodeType"]:checked')).map((item) => item.value);
        }}

        function syncRange(id, suffix, formatter) {{
            const input = document.getElementById(id);
            const output = document.getElementById(id + "Value");
            const update = () => {{
                output.textContent = formatter ? formatter(Number(input.value)) : `${{input.value}}${{suffix}}`;
            }};
            input.addEventListener("input", update);
            update();
        }}

        function initMap() {{
            const start = readCenter();
            map = new google.maps.Map(document.getElementById("map"), {{
                center: start,
                zoom: 15,
                mapTypeControl: false,
                streetViewControl: false,
                fullscreenControl: true,
            }});
            centerMarker = new google.maps.Marker({{
                position: start,
                map,
                title: "출발 위치",
                icon: "https://maps.google.com/mapfiles/ms/icons/blue-dot.png",
            }});
        }}

        function moveCenter(lat, lng) {{
            const pos = {{ lat, lng }};
            document.getElementById("lat").value = lat.toFixed(6);
            document.getElementById("lng").value = lng.toFixed(6);
            if (map) {{
                map.setCenter(pos);
                centerMarker.setPosition(pos);
            }}
        }}

        function clearMapLayers() {{
            clearRouteAndMarkers();
            shadowRects.forEach((rect) => rect.setMap(null));
            shadowRects = [];
        }}

        function clearRouteAndMarkers() {{
            if (routePolyline) {{
                routePolyline.setMap(null);
                routePolyline = null;
            }}
            nodeMarkers.forEach((marker) => marker.setMap(null));
            nodeMarkers = [];
        }}

        async function fetchJson(url, options) {{
            const res = await fetch(url, options);
            const raw = await res.text();
            let data = {{}};
            try {{
                data = raw ? JSON.parse(raw) : {{}};
            }} catch (err) {{
                data = {{ detail: raw || "서버가 JSON이 아닌 응답을 반환했습니다." }};
            }}
            if (!res.ok) {{
                throw new Error(data.detail || `요청 실패 (${{res.status}})`);
            }}
            return data;
        }}

        function colorForSunlight(score) {{
            if (score >= 0.65) return "#f3bc3d";
            if (score >= 0.35) return "#89b95b";
            return "#53636d";
        }}

        function drawShadowLookup(data) {{
            data.cells.forEach((cell) => {{
                const rect = new google.maps.Rectangle({{
                    bounds: {{
                        north: cell.north,
                        south: cell.south,
                        east: cell.east,
                        west: cell.west
                    }},
                    strokeOpacity: 0,
                    fillColor: colorForSunlight(cell.sunlightScore),
                    fillOpacity: 0.18,
                    map
                }});
                shadowRects.push(rect);
            }});
        }}

        function drawRecommendation(item) {{
            clearRouteAndMarkers();
            if (item.route.encodedPolyline) {{
                const path = google.maps.geometry.encoding.decodePath(item.route.encodedPolyline);
                routePolyline = new google.maps.Polyline({{
                    path,
                    map,
                    strokeColor: "#2f7d4e",
                    strokeOpacity: 0.95,
                    strokeWeight: 6,
                }});
                const bounds = new google.maps.LatLngBounds();
                path.forEach((point) => bounds.extend(point));
                map.fitBounds(bounds);
            }}

            item.nodes.forEach((node, index) => {{
                const marker = new google.maps.Marker({{
                    position: {{ lat: node.lat, lng: node.lng }},
                    map,
                    label: String(index + 1),
                    title: `${{node.categoryLabel}} · ${{node.name}}`,
                }});
                nodeMarkers.push(marker);
            }});
        }}

        function renderResults(data) {{
            recommendations = data.recommendations || [];
            const container = document.getElementById("results");
            container.innerHTML = "";
            if (!recommendations.length) {{
                container.innerHTML = '<p class="empty">추천 가능한 동선을 만들지 못했습니다. 반경이나 조건을 완화해보세요.</p>';
                return;
            }}
            recommendations.forEach((item, index) => {{
                const route = item.route;
                const div = document.createElement("div");
                div.className = "result" + (index === 0 ? " active" : "");
                div.innerHTML = `
                    <h2>${{item.title}}</h2>
                    <div class="badges">
                        <span class="badge">${{item.meetsAllConditions ? "조건 충족" : "조건 근접"}}</span>
                        <span class="badge sun">일조 ${{Math.round(item.sunlightScore * 100)}}%</span>
                        ${{item.violations.length ? `<span class="badge warn">${{item.violations.join(", ")}}</span>` : ""}}
                    </div>
                    <div class="metrics">
                        <span>거리 ${{route.distanceMeters || "-"}}m</span>
                        <span>시간 ${{route.durationMinutes || "-"}}분</span>
                        <span>걸음 약 ${{route.steps || "-"}}보</span>
                        <span>날씨 점수 ${{Math.round(item.weatherScore * 100)}}%</span>
                    </div>
                    <div class="nodes">${{item.nodes.map((node) => `${{node.categoryLabel}}: ${{node.name}}`).join("<br>")}}</div>
                `;
                div.addEventListener("click", () => {{
                    document.querySelectorAll(".result").forEach((el) => el.classList.remove("active"));
                    div.classList.add("active");
                    drawRecommendation(item);
                }});
                container.appendChild(div);
            }});
            drawRecommendation(recommendations[0]);
        }}

        async function recommend() {{
            const btn = document.getElementById("btnRecommend");
            const center = readCenter();
            if (!Number.isFinite(center.lat) || !Number.isFinite(center.lng)) {{
                setStatus("위도와 경도를 확인해주세요.");
                return;
            }}
            const requiredTypes = selectedTypes();
            if (!requiredTypes.length) {{
                setStatus("경유 노드를 하나 이상 선택해주세요.");
                return;
            }}

            btn.disabled = true;
            setStatus("주변 장소, 날씨, 일조 조건을 계산하는 중입니다.");
            clearMapLayers();
            try {{
                const shadow = await fetchJson(`/api/shadow-lookup?lat=${{center.lat}}&lng=${{center.lng}}&radius=${{Number(document.getElementById("radius").value)}}&grid_size=9`);
                drawShadowLookup(shadow);

                const payload = {{
                    lat: center.lat,
                    lng: center.lng,
                    radius: Number(document.getElementById("radius").value),
                    target_minutes: Number(document.getElementById("targetMinutes").value),
                    max_steps: Number(document.getElementById("maxSteps").value),
                    min_sunlight: Number(document.getElementById("minSunlight").value) / 100,
                    required_types: requiredTypes,
                    experimental3d: document.getElementById("experimental3d").checked
                }};
                const data = await fetchJson("/api/recommendations", {{
                    method: "POST",
                    headers: {{ "Content-Type": "application/json" }},
                    body: JSON.stringify(payload)
                }});
                renderResults(data);
                const weather = data.weather;
                setStatus(`${{weather.conditionKo}} · ${{weather.walkability.summary}} · 추천 ${{data.recommendations.length}}개`);
            }} catch (err) {{
                setStatus(err.message || "추천을 만들지 못했습니다.");
            }} finally {{
                btn.disabled = false;
            }}
        }}

        document.getElementById("btnMove").addEventListener("click", () => {{
            const center = readCenter();
            moveCenter(center.lat, center.lng);
            setStatus("지도를 입력한 위치로 이동했습니다.");
        }});

        document.getElementById("btnCurrent").addEventListener("click", () => {{
            if (!navigator.geolocation) {{
                setStatus("브라우저가 현재 위치를 지원하지 않습니다.");
                return;
            }}
            navigator.geolocation.getCurrentPosition(
                (pos) => {{
                    moveCenter(pos.coords.latitude, pos.coords.longitude);
                    setStatus("현재 위치를 출발점으로 설정했습니다.");
                }},
                () => setStatus("위치 권한이 없거나 현재 위치를 가져오지 못했습니다.")
            );
        }});

        document.getElementById("btnRecommend").addEventListener("click", recommend);
        syncRange("radius", "m");
        syncRange("targetMinutes", "분");
        syncRange("maxSteps", "보");
        syncRange("minSunlight", "%");
    </script>
    <script async defer src="https://maps.googleapis.com/maps/api/js?key={maps_key}&libraries=geometry&callback=initMap"></script>
</body>
</html>
    """


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "googleMapsApiKeyConfigured": bool(get_google_maps_api_key()),
        "dependencies": {
            "fastapi": _dependency_version("fastapi"),
            "uvicorn": _dependency_version("uvicorn"),
            "requests": _dependency_version("requests"),
            "pydantic": _dependency_version("pydantic"),
        },
    }


@app.get("/api/weather")
def get_weather(lat: float = Query(...), lng: float = Query(...)) -> dict[str, Any]:
    return _fetch_weather(lat, lng)


@app.get("/api/sun")
def get_sun(
    lat: float = Query(...),
    lng: float = Query(...),
    datetime_value: str | None = Query(default=None, alias="datetime"),
) -> dict[str, Any]:
    return _sun_position(lat, lng, _parse_datetime(datetime_value))


@app.get("/api/shadow-lookup")
def get_shadow_lookup(
    lat: float = Query(...),
    lng: float = Query(...),
    radius: int = Query(default=1200, ge=100, le=3000),
    grid_size: int = Query(default=9, ge=3, le=20),
    datetime_value: str | None = Query(default=None, alias="datetime"),
) -> dict[str, Any]:
    return _make_shadow_lookup(lat, lng, radius, grid_size, _parse_datetime(datetime_value))


@app.get("/api/places")
def get_places(
    lat: float = Query(...),
    lng: float = Query(...),
    radius: int = Query(default=1200, ge=100, le=3000),
    types: str = Query(default="convenience_store,supermarket,grocery_store,market,park"),
    max_result_count: int = Query(default=10, ge=1, le=20),
) -> dict[str, Any]:
    place_types = [item.strip() for item in types.split(",") if item.strip()]
    unknown = [item for item in place_types if item not in SUPPORTED_PLACE_TYPES]
    if unknown:
        raise HTTPException(status_code=400, detail=f"지원하지 않는 장소 타입입니다: {', '.join(unknown)}")
    return _search_places(lat, lng, radius, place_types, max_result_count)


@app.post("/api/recommendations")
def get_recommendations(request: RecommendationRequest) -> dict[str, Any]:
    return _build_recommendations(request)


@app.get("/api/route/walk")
def get_walk_route(
    start_lat: float = Query(...),
    start_lng: float = Query(...),
    end_lat: float = Query(...),
    end_lng: float = Query(...),
) -> dict[str, Any]:
    route = _compute_walk_route(
        {"lat": start_lat, "lng": start_lng},
        {"lat": end_lat, "lng": end_lng},
    )
    return {"route": route}


@app.get("/api/nearby/convenience")
def get_nearby_convenience(
    lat: float = Query(...),
    lng: float = Query(...),
    radius: int = Query(default=1000, ge=100, le=3000),
) -> dict[str, Any]:
    return {
        "places": _search_places_by_type(lat, lng, radius, "convenience_store", 10),
    }


@app.get("/api/nearby/convenience/normalized")
def get_nearby_convenience_normalized(
    lat: float = Query(...),
    lng: float = Query(...),
    radius: int = Query(default=1000, ge=100, le=3000),
) -> dict[str, Any]:
    places = _search_places_by_type(lat, lng, radius, "convenience_store", 10)
    return {
        "count": len(places),
        "items": places,
    }


@app.get("/api/route/walk/normalized")
def get_walk_route_normalized(
    start_lat: float = Query(...),
    start_lng: float = Query(...),
    end_lat: float = Query(...),
    end_lng: float = Query(...),
) -> dict[str, Any]:
    return get_walk_route(start_lat, start_lng, end_lat, end_lng)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
