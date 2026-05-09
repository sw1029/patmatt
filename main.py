import configparser
import html
import importlib.metadata
import json
import math
import os
from datetime import datetime, timedelta, timezone
from itertools import product
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent
ANONYMOUS_SIGNALS_PATH = BASE_DIR / "anonymous_route_signals.json"
STEP_LENGTH_M = 0.72
DEFAULT_WALKING_SPEED_MPS = 1.15
MIN_WALKING_SPEED_MPS = 0.3
MAX_WALKING_SPEED_MPS = 2.5
COMMUNITY_LANDMARK_THRESHOLD = 2
KMA_API_BASE_URL = "https://apihub.kma.go.kr/api/typ02/openApi/VilageFcstInfoService_2.0"
KOREA_BOUNDS = {
    "lat_min": 32.5,
    "lat_max": 39.6,
    "lng_min": 123.5,
    "lng_max": 132.2,
}
WEATHER_LABELS_KO = {
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


class ScheduleOffset(BaseModel):
    name: str = ""
    weekday: int | None = Field(default=None, ge=0, le=6)
    date: str | None = None
    radius_offset: int = 0
    target_minutes_offset: int = 0
    max_steps_offset: int = 0
    min_sunlight_offset: float = 0


class MovementSample(BaseModel):
    lat: float
    lng: float
    timestamp: str
    accuracy_m: float | None = Field(default=None, ge=0)


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
    scheduled_at: str | None = None
    schedule_offsets: list[ScheduleOffset] = Field(default_factory=list)
    collect_anonymous_usage: bool = True
    walking_speed_mps: float | None = Field(
        default=None,
        ge=MIN_WALKING_SPEED_MPS,
        le=MAX_WALKING_SPEED_MPS,
        description="User average walking speed in meters per second.",
    )
    movement_samples: list[MovementSample] = Field(default_factory=list)


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


def _read_key_from_ini(path: Path, key_names: tuple[str, ...]) -> str:
    if not path.exists():
        return ""
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return ""
    if not raw.startswith("["):
        raw = "[default]\n" + raw
    parser = configparser.ConfigParser()
    parser.read_string(raw)
    for section in parser.sections():
        for key_name in key_names:
            value = parser.get(section, key_name, fallback="").strip()
            if value and value != "YOUR_API_KEY_HERE":
                return value
    return ""


def get_kma_api_key() -> str:
    for env_name in ("KMA_API_KEY", "KMA_AUTH_KEY", "KOREA_WEATHER_API_KEY"):
        value = os.getenv(env_name, "").strip()
        if value:
            return value
    return _read_key_from_ini(
        BASE_DIR / "weather.ini",
        ("KMA_API_KEY", "KMA_AUTH_KEY", "authKey", "serviceKey", "key", "api_key"),
    )


def get_google_weather_api_key() -> str:
    for env_name in ("GOOGLE_WEATHER_API_KEY", "GOOGLE_MAPS_API_KEY"):
        value = os.getenv(env_name, "").strip()
        if value and value != "YOUR_API_KEY_HERE":
            return value
    configured = _read_key_from_ini(
        BASE_DIR / "api_key.ini",
        ("GOOGLE_WEATHER_API_KEY", "google_weather_api_key", "GOOGLE_MAPS_API_KEY", "key", "api_key"),
    )
    return configured or get_google_maps_api_key()


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


def _clamp_walking_speed(speed_mps: float) -> float:
    return max(MIN_WALKING_SPEED_MPS, min(MAX_WALKING_SPEED_MPS, speed_mps))


def _parse_sample_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)


def _speed_from_movement_samples(samples: list["MovementSample"]) -> float | None:
    parsed_samples: list[tuple[datetime, MovementSample]] = []
    for sample in samples:
        if sample.accuracy_m is not None and sample.accuracy_m > 50:
            continue
        timestamp = _parse_sample_timestamp(sample.timestamp)
        if timestamp is None:
            continue
        parsed_samples.append((timestamp, sample))

    parsed_samples.sort(key=lambda item: item[0])
    total_distance = 0.0
    total_seconds = 0.0
    for (prev_time, prev_sample), (next_time, next_sample) in zip(parsed_samples, parsed_samples[1:]):
        seconds = (next_time - prev_time).total_seconds()
        if seconds < 2 or seconds > 300:
            continue
        distance = _haversine_m(prev_sample.lat, prev_sample.lng, next_sample.lat, next_sample.lng)
        speed = distance / seconds
        if speed < MIN_WALKING_SPEED_MPS or speed > MAX_WALKING_SPEED_MPS:
            continue
        total_distance += distance
        total_seconds += seconds

    if total_seconds <= 0:
        return None
    return round(_clamp_walking_speed(total_distance / total_seconds), 3)


def _effective_walking_speed(request: "RecommendationRequest") -> dict[str, Any]:
    sample_speed = _speed_from_movement_samples(request.movement_samples)
    if sample_speed is not None:
        return {
            "speedMps": sample_speed,
            "source": "movement_samples",
            "sampleCount": len(request.movement_samples),
        }
    if request.walking_speed_mps is not None:
        return {
            "speedMps": round(_clamp_walking_speed(request.walking_speed_mps), 3),
            "source": "user_average",
            "sampleCount": len(request.movement_samples),
        }
    return {
        "speedMps": DEFAULT_WALKING_SPEED_MPS,
        "source": "default",
        "sampleCount": 0,
    }


def _apply_walking_speed(route: dict[str, Any], speed_profile: dict[str, Any]) -> dict[str, Any]:
    distance_m = route.get("distanceMeters")
    speed_mps = float(speed_profile["speedMps"])
    if not distance_m or speed_mps <= 0:
        return route

    adjusted = dict(route)
    provider_seconds = route.get("durationSeconds")
    provider_minutes = route.get("durationMinutes")
    duration_s = max(1, round(float(distance_m) / speed_mps))
    adjusted["providerDurationSeconds"] = provider_seconds
    adjusted["providerDurationMinutes"] = provider_minutes
    adjusted["durationSeconds"] = duration_s
    adjusted["durationText"] = f"{duration_s}s"
    adjusted["durationMinutes"] = round(duration_s / 60, 1)
    adjusted["durationAdjustedBySpeed"] = True
    adjusted["walkingSpeedMps"] = round(speed_mps, 3)
    adjusted["walkingSpeedSource"] = speed_profile["source"]

    adjusted_legs = []
    for leg in route.get("legs") or []:
        leg_copy = dict(leg)
        leg_distance = leg_copy.get("distanceMeters")
        if leg_distance:
            leg_duration_s = max(1, round(float(leg_distance) / speed_mps))
            leg_copy["userDuration"] = f"{leg_duration_s}s"
            leg_copy["userDurationSeconds"] = leg_duration_s
        adjusted_legs.append(leg_copy)
    adjusted["legs"] = adjusted_legs
    return adjusted


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


def _decode_polyline(encoded: str | None) -> list[dict[str, float]]:
    if not encoded:
        return []
    points: list[dict[str, float]] = []
    index = 0
    lat = 0
    lng = 0
    length = len(encoded)

    while index < length:
        values: list[int] = []
        for _ in range(2):
            shift = 0
            result = 0
            while index < length:
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            values.append(~(result >> 1) if result & 1 else result >> 1)
        if len(values) == 2:
            lat += values[0]
            lng += values[1]
            points.append({"lat": lat / 1e5, "lng": lng / 1e5})
    return points


def _sun_exposure_profile(route: dict[str, Any], sunlight_score: float) -> dict[str, Any]:
    duration_minutes = route.get("durationMinutes")
    if duration_minutes is None and route.get("durationSeconds") is not None:
        duration_minutes = float(route["durationSeconds"]) / 60
    try:
        total_minutes = max(0.0, float(duration_minutes or 0))
    except (TypeError, ValueError):
        total_minutes = 0.0
    sunlight_minutes = total_minutes * _clamp(float(sunlight_score))
    shade_minutes = max(0.0, total_minutes - sunlight_minutes)
    return {
        "totalMinutes": round(total_minutes, 1),
        "sunlightMinutes": round(sunlight_minutes, 1),
        "shadeMinutes": round(shade_minutes, 1),
        "sunlightRatio": round(_clamp(float(sunlight_score)), 3),
        "label": f"햇빛 약 {max(1, round(sunlight_minutes))}분" if sunlight_minutes > 0 else "햇빛 거의 없음",
        "detailLabel": f"햇빛 약 {round(sunlight_minutes)}분 · 그늘 약 {round(shade_minutes)}분",
    }


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


def _is_in_korea(lat: float, lng: float) -> bool:
    return (
        KOREA_BOUNDS["lat_min"] <= lat <= KOREA_BOUNDS["lat_max"]
        and KOREA_BOUNDS["lng_min"] <= lng <= KOREA_BOUNDS["lng_max"]
    )


def _latlng_to_kma_grid(lat: float, lng: float) -> dict[str, int]:
    re = 6371.00877
    grid = 5.0
    slat1 = 30.0
    slat2 = 60.0
    olon = 126.0
    olat = 38.0
    xo = 43.0
    yo = 136.0
    degrad = math.pi / 180.0

    re_grid = re / grid
    slat1_rad = slat1 * degrad
    slat2_rad = slat2 * degrad
    olon_rad = olon * degrad
    olat_rad = olat * degrad

    sn = math.tan(math.pi * 0.25 + slat2_rad * 0.5) / math.tan(math.pi * 0.25 + slat1_rad * 0.5)
    sn = math.log(math.cos(slat1_rad) / math.cos(slat2_rad)) / math.log(sn)
    sf = math.tan(math.pi * 0.25 + slat1_rad * 0.5)
    sf = math.pow(sf, sn) * math.cos(slat1_rad) / sn
    ro = math.tan(math.pi * 0.25 + olat_rad * 0.5)
    ro = re_grid * sf / math.pow(ro, sn)

    ra = math.tan(math.pi * 0.25 + lat * degrad * 0.5)
    ra = re_grid * sf / math.pow(ra, sn)
    theta = lng * degrad - olon_rad
    if theta > math.pi:
        theta -= 2.0 * math.pi
    if theta < -math.pi:
        theta += 2.0 * math.pi
    theta *= sn
    return {
        "nx": int(math.floor(ra * math.sin(theta) + xo + 0.5)),
        "ny": int(math.floor(ro - ra * math.cos(theta) + yo + 0.5)),
    }


def _kma_base_datetime(now: datetime, release_delay_minute: int) -> datetime:
    local = now.astimezone()
    if local.minute < release_delay_minute:
        local -= timedelta(hours=1)
    return local.replace(minute=0, second=0, microsecond=0)


def _kma_value_to_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {"강수없음", "없음"}:
        return 0.0
    if "미만" in text:
        return 0.1
    try:
        return float(text)
    except ValueError:
        digits = "".join(ch if ch.isdigit() or ch in ".-" else " " for ch in text).split()
        return float(digits[0]) if digits else None


def _kma_pty_to_weather_code(value: Any, sky: Any = None) -> int:
    pty = str(value or "0").strip()
    if pty in {"1", "5"}:
        return 61
    if pty in {"2", "6"}:
        return 63
    if pty in {"3", "7"}:
        return 71
    sky_text = str(sky or "1").strip()
    if sky_text == "4":
        return 3
    if sky_text == "3":
        return 2
    return 0


def _kma_sky_to_cloud_cover(value: Any) -> int:
    sky = str(value or "1").strip()
    if sky == "4":
        return 90
    if sky == "3":
        return 60
    return 10


def _kma_items_by_category(items: list[dict[str, Any]], value_key: str) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for item in items:
        category = item.get("category")
        if category:
            values[str(category)] = item.get(value_key)
    return values


def _kma_nearest_forecast_values(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        return {}
    now_key = datetime.now().strftime("%Y%m%d%H%M")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        key = f"{item.get('fcstDate', '')}{item.get('fcstTime', '')}"
        if key:
            grouped.setdefault(key, []).append(item)
    if not grouped:
        return {}
    future_keys = [key for key in grouped if key >= now_key]
    selected_key = min(future_keys) if future_keys else max(grouped)
    return _kma_items_by_category(grouped[selected_key], "fcstValue")


def _kma_request(method: str, base_dt: datetime, nx: int, ny: int, rows: int = 100) -> list[dict[str, Any]]:
    api_key = get_kma_api_key()
    if not api_key:
        raise HTTPException(status_code=400, detail="기상청 API 키가 설정되어 있지 않습니다. weather.ini 또는 KMA_API_KEY를 확인하세요.")
    response = requests.get(
        f"{KMA_API_BASE_URL}/{method}",
        params={
            "authKey": api_key,
            "numOfRows": rows,
            "pageNo": 1,
            "dataType": "JSON",
            "base_date": base_dt.strftime("%Y%m%d"),
            "base_time": base_dt.strftime("%H%M"),
            "nx": nx,
            "ny": ny,
        },
        timeout=15,
    )
    try:
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        detail = f"기상청 {method} 호출에 실패했습니다."
        try:
            error_data = response.json()
            api_message = ((error_data.get("result") or {}).get("message")) or ((error_data.get("response") or {}).get("header") or {}).get("resultMsg")
            if api_message:
                detail = f"{detail} {api_message}"
        except ValueError:
            pass
        raise HTTPException(status_code=502, detail=detail) from exc

    header = ((data.get("response") or {}).get("header") or {})
    result_code = str(header.get("resultCode", "00"))
    if result_code not in {"00", "0"}:
        raise HTTPException(status_code=502, detail=f"기상청 응답 오류: {header.get('resultMsg', result_code)}")
    items = ((((data.get("response") or {}).get("body") or {}).get("items") or {}).get("item") or [])
    return items if isinstance(items, list) else [items]


def _normalize_kma_weather(lat: float, lng: float, grid: dict[str, int], obs: dict[str, Any], fcst: dict[str, Any]) -> dict[str, Any]:
    pty = obs.get("PTY", fcst.get("PTY", "0"))
    sky = fcst.get("SKY")
    weather_code = _kma_pty_to_weather_code(pty, sky)
    precipitation = _kma_value_to_float(obs.get("RN1", fcst.get("RN1"))) or 0
    current = {
        "temperature_2m": _kma_value_to_float(obs.get("T1H", fcst.get("T1H"))),
        "relative_humidity_2m": _kma_value_to_float(obs.get("REH", fcst.get("REH"))),
        "precipitation": precipitation,
        "rain": precipitation if str(pty) not in {"3", "7"} else 0,
        "weather_code": weather_code,
        "cloud_cover": _kma_sky_to_cloud_cover(sky),
        "wind_speed_10m": _kma_value_to_float(obs.get("WSD", fcst.get("WSD"))) or 0,
        "wind_speed_unit": "METERS_PER_SECOND",
        "is_day": 1 if _sun_position(lat, lng, datetime.now().astimezone())["isDaylight"] else 0,
    }
    return {
        "source": "KMA",
        "provider": "Korea Meteorological Administration",
        "lat": lat,
        "lng": lng,
        "timezone": "Asia/Seoul",
        "grid": grid,
        "current": current,
        "conditionKo": WEATHER_LABELS_KO.get(weather_code, "확인 필요"),
        "walkability": _weather_walkability(current),
        "raw": {"observation": obs, "forecast": fcst},
    }


def _fetch_kma_weather(lat: float, lng: float) -> dict[str, Any]:
    grid = _latlng_to_kma_grid(lat, lng)
    now = datetime.now().astimezone()
    obs_dt = _kma_base_datetime(now, 10)
    fcst_dt = _kma_base_datetime(now, 45)
    warnings: list[str] = []
    obs_items = _kma_request("getUltraSrtNcst", obs_dt, grid["nx"], grid["ny"], 40)
    try:
        fcst_items = _kma_request("getUltraSrtFcst", fcst_dt, grid["nx"], grid["ny"], 100)
    except HTTPException as exc:
        fcst_items = []
        warnings.append(f"초단기예보 보강 생략: {exc.detail}")
    obs = _kma_items_by_category(obs_items, "obsrValue")
    fcst = _kma_nearest_forecast_values(fcst_items)
    result = _normalize_kma_weather(lat, lng, grid, obs, fcst)
    result["base"] = {
        "observation": {"date": obs_dt.strftime("%Y%m%d"), "time": obs_dt.strftime("%H%M")},
        "forecast": {"date": fcst_dt.strftime("%Y%m%d"), "time": fcst_dt.strftime("%H%M")},
    }
    if warnings:
        result["warnings"] = warnings
    return result


def _google_weather_type_to_code(value: str | None, cloud_cover: int | None = None) -> int:
    weather_type = (value or "").upper()
    if "THUNDER" in weather_type:
        return 95
    if "HEAVY" in weather_type and "RAIN" in weather_type:
        return 65
    if "RAIN" in weather_type or "DRIZZLE" in weather_type:
        return 61
    if "SNOW" in weather_type:
        return 71
    if "CLOUDY" in weather_type:
        return 3 if (cloud_cover or 0) >= 80 else 2
    return 0


def _fetch_google_weather(lat: float, lng: float) -> dict[str, Any]:
    api_key = get_google_weather_api_key()
    if not api_key:
        raise HTTPException(status_code=400, detail="Google Weather API 키가 설정되어 있지 않습니다.")
    response = requests.get(
        "https://weather.googleapis.com/v1/currentConditions:lookup",
        params={
            "key": api_key,
            "location.latitude": lat,
            "location.longitude": lng,
            "languageCode": "ko",
        },
        timeout=15,
    )
    try:
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        detail = "Google Weather API 호출에 실패했습니다."
        try:
            error_data = response.json()
            api_message = ((error_data.get("error") or {}).get("message"))
            if api_message:
                if "disabled" in api_message or "has not been used" in api_message:
                    detail = f"{detail} Weather API가 비활성화되어 있습니다. Google Cloud Console에서 weather.googleapis.com을 활성화하세요."
                else:
                    detail = f"{detail} {api_message}"
        except ValueError:
            pass
        raise HTTPException(status_code=502, detail=detail) from exc

    precipitation = (data.get("precipitation") or {}).get("qpf") or {}
    wind = data.get("wind") or {}
    wind_speed_data = wind.get("speed") or {}
    wind_speed = wind_speed_data.get("value") or 0
    cloud_cover = data.get("cloudCover")
    weather_condition = data.get("weatherCondition") or {}
    weather_code = _google_weather_type_to_code(weather_condition.get("type"), cloud_cover)
    current = {
        "temperature_2m": (data.get("temperature") or {}).get("degrees"),
        "relative_humidity_2m": data.get("relativeHumidity"),
        "precipitation": precipitation.get("quantity") or 0,
        "rain": precipitation.get("quantity") or 0,
        "weather_code": weather_code,
        "cloud_cover": cloud_cover or 0,
        "wind_speed_10m": wind_speed,
        "wind_speed_unit": wind_speed_data.get("unit") or "KILOMETERS_PER_HOUR",
        "is_day": 1 if data.get("isDaytime") else 0,
    }
    label = ((weather_condition.get("description") or {}).get("text")) or WEATHER_LABELS_KO.get(weather_code, "확인 필요")
    return {
        "source": "Google Weather",
        "provider": "Google Weather API",
        "lat": lat,
        "lng": lng,
        "timezone": (data.get("timeZone") or {}).get("id"),
        "current": current,
        "conditionKo": label,
        "walkability": _weather_walkability(current),
        "raw": data,
    }


def _weather_walkability(current: dict[str, Any]) -> dict[str, Any]:
    def as_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def wind_to_kmh(value: Any, unit: Any) -> float:
        wind_value = as_float(value) or 0.0
        unit_text = str(unit or "").upper()
        if "KILOMETER" in unit_text or unit_text in {"KM/H", "KPH"}:
            return wind_value
        if "MILE" in unit_text or unit_text == "MPH":
            return wind_value * 1.60934
        if "METER" in unit_text or unit_text in {"M/S", "MS", "MPS"}:
            return wind_value * 3.6
        return wind_value

    temp = as_float(current.get("temperature_2m"))
    precipitation = max(as_float(current.get("precipitation")) or 0.0, as_float(current.get("rain")) or 0.0)
    wind_kmh = wind_to_kmh(current.get("wind_speed_10m"), current.get("wind_speed_unit"))
    humidity = as_float(current.get("relative_humidity_2m"))
    code = current.get("weather_code")

    score = 1.0
    reasons: list[str] = []
    if temp is not None:
        if temp <= -10 or temp >= 35:
            score -= 0.5
            reasons.append("기온 위험")
        elif temp <= 0 or temp >= 30:
            score -= 0.25
            reasons.append("기온 주의")
        elif temp <= 5 or temp >= 28:
            score -= 0.1
            reasons.append("기온 살핌")

    if precipitation >= 10:
        score -= 0.55
        reasons.append("강한 비")
    elif precipitation >= 3:
        score -= 0.4
        reasons.append("비")
    elif precipitation > 0.5:
        score -= 0.25
        reasons.append("약한 비")
    elif precipitation > 0:
        score -= 0.1
        reasons.append("비 가능성")

    if wind_kmh >= 50:
        score -= 0.4
        reasons.append("강풍")
    elif wind_kmh >= 35:
        score -= 0.25
        reasons.append("바람 강함")
    elif wind_kmh >= 25:
        score -= 0.1
        reasons.append("바람 있음")

    if code in {95, 96, 99}:
        score -= 0.7
        reasons.append("뇌우")
    elif code in {65, 75, 82}:
        score -= 0.4
        reasons.append("강수 강함")
    elif code in {71, 73, 80, 81}:
        score -= 0.25
        reasons.append("눈 또는 소나기")
    elif code in {45, 48}:
        score -= 0.15
        reasons.append("안개")

    if temp is not None and humidity is not None and temp >= 27 and humidity >= 80:
        score -= 0.15
        reasons.append("습도 높음")

    score = _clamp(score)
    if score >= 0.75:
        summary = "산책 가능"
        level = "good"
    elif score >= 0.55:
        summary = "짧게 산책 권장"
        level = "okay"
    else:
        summary = "산책 주의"
        level = "caution"

    return {
        "score": round(score, 3),
        "walkable": score >= 0.55,
        "level": level,
        "summary": summary,
        "reasons": reasons,
        "normalized": {
            "temperatureC": round(temp, 1) if temp is not None else None,
            "precipitationMm": round(precipitation, 2),
            "windKmh": round(wind_kmh, 1),
            "humidityPercent": round(humidity, 1) if humidity is not None else None,
        },
    }

def _fetch_open_meteo_weather(lat: float, lng: float, fallback_reason: str | None = None) -> dict[str, Any]:
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
        raise HTTPException(status_code=502, detail="Open-Meteo API 호출에 실패했습니다.") from exc

    data = response.json()
    current = data.get("current") or {}
    current["wind_speed_unit"] = "KILOMETERS_PER_HOUR"
    code = current.get("weather_code")
    return {
        "source": "Open-Meteo",
        "provider": "Open-Meteo Forecast API",
        "fallbackReason": fallback_reason,
        "lat": lat,
        "lng": lng,
        "timezone": data.get("timezone"),
        "current": current,
        "conditionKo": WEATHER_LABELS_KO.get(code, WEATHER_CODES_KO.get(code, "확인 필요")),
        "walkability": _weather_walkability(current),
    }


def _fetch_weather(lat: float, lng: float) -> dict[str, Any]:
    if _is_in_korea(lat, lng):
        try:
            return _fetch_kma_weather(lat, lng)
        except HTTPException as exc:
            return _fetch_open_meteo_weather(lat, lng, f"KMA fallback: {exc.detail}")

    try:
        return _fetch_google_weather(lat, lng)
    except HTTPException as exc:
        return _fetch_open_meteo_weather(lat, lng, f"Google Weather fallback: {exc.detail}")


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
    target_minutes: int | None = None,
) -> float:
    steps = route.get("steps") or 999_999
    steps_score = _clamp(1 - steps / 12_000)
    duration_s = route.get("durationSeconds")
    if duration_s and target_minutes:
        time_score = _clamp(1 - duration_s / max(1, target_minutes * 60 * 1.5))
    else:
        time_score = steps_score
    effort_score = steps_score * 0.65 + time_score * 0.35
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
    experimental3d: bool = False,
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
            elevation_factor = _clamp(float(sun["elevationDeg"]) / 70)
            building_shadow = _clamp((1 - elevation_factor) * urban_density * 0.45) if experimental3d else 0
            sunlight = _clamp(daylight * cloud_factor * alignment * (1 - 0.35 * urban_density) - building_shadow)
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
                    "estimatedBuildingShadow": round(building_shadow, 3),
                    "isLikelyShadow": (1 - sunlight) >= 0.55,
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
            "enabled": experimental3d,
            "mode": "estimated_3d_shadow_lookup" if experimental3d else "topdown_lookup_table",
            "note": "v1은 태양 고도/방위, 구름량, 위치별 밀도 추정으로 만든 2.5D 그림자 레이어입니다. 실제 Google Photorealistic 3D Tiles 렌더링은 후속 확장 지점입니다.",
        },
    }


def _empty_anonymous_signals() -> dict[str, Any]:
    return {
        "version": 1,
        "buckets": {},
    }


def _load_anonymous_signals() -> dict[str, Any]:
    if not ANONYMOUS_SIGNALS_PATH.exists():
        return _empty_anonymous_signals()
    try:
        data = json.loads(ANONYMOUS_SIGNALS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_anonymous_signals()
    if not isinstance(data, dict) or not isinstance(data.get("buckets"), dict):
        return _empty_anonymous_signals()
    return data


def _save_anonymous_signals(data: dict[str, Any]) -> None:
    tmp_path = ANONYMOUS_SIGNALS_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(ANONYMOUS_SIGNALS_PATH)


def _location_bucket(lat: float, lng: float) -> str:
    return f"{round(lat, 2):.2f},{round(lng, 2):.2f}"


def _bucket_label(bucket_key: str) -> dict[str, float | str]:
    lat_text, lng_text = bucket_key.split(",", 1)
    return {
        "bucket": bucket_key,
        "lat": float(lat_text),
        "lng": float(lng_text),
    }


def _get_bucket(data: dict[str, Any], lat: float, lng: float) -> dict[str, Any]:
    bucket_key = _location_bucket(lat, lng)
    buckets = data.setdefault("buckets", {})
    bucket = buckets.setdefault(
        bucket_key,
        {
            "requests": 0,
            "type_counts": {},
            "target_minutes_total": 0,
            "max_steps_total": 0,
            "min_sunlight_total": 0,
            "landmarks": {},
            "updated_at": None,
        },
    )
    return bucket


def _parse_schedule_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now().astimezone()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now().astimezone()
    return parsed.astimezone() if parsed.tzinfo else parsed.astimezone()


def _apply_schedule_offsets(request: RecommendationRequest) -> tuple[RecommendationRequest, dict[str, Any]]:
    scheduled_at = _parse_schedule_datetime(request.scheduled_at)
    date_key = scheduled_at.date().isoformat()
    weekday = scheduled_at.weekday()
    matched: list[dict[str, Any]] = []

    radius = request.radius
    target_minutes = request.target_minutes
    max_steps = request.max_steps
    min_sunlight = request.min_sunlight

    for offset in request.schedule_offsets:
        date_match = bool(offset.date and offset.date == date_key)
        weekday_match = offset.weekday is not None and offset.weekday == weekday
        if not date_match and not weekday_match:
            continue
        radius += offset.radius_offset
        target_minutes += offset.target_minutes_offset
        max_steps += offset.max_steps_offset
        min_sunlight += offset.min_sunlight_offset
        matched.append(offset.model_dump())

    adjusted = request.model_copy(
        update={
            "radius": max(250, min(3000, radius)),
            "target_minutes": max(5, min(180, target_minutes)),
            "max_steps": max(300, min(30000, max_steps)),
            "min_sunlight": _clamp(min_sunlight),
        }
    )
    return adjusted, {
        "scheduledAt": scheduled_at.isoformat(),
        "date": date_key,
        "weekday": weekday,
        "matchedOffsets": matched,
        "applied": bool(matched),
        "effective": {
            "radius": adjusted.radius,
            "target_minutes": adjusted.target_minutes,
            "max_steps": adjusted.max_steps,
            "min_sunlight": adjusted.min_sunlight,
        },
    }


def _community_landmarks(lat: float, lng: float, threshold: int = COMMUNITY_LANDMARK_THRESHOLD) -> list[dict[str, Any]]:
    data = _load_anonymous_signals()
    bucket = _get_bucket(data, lat, lng)
    landmarks = []
    for item in (bucket.get("landmarks") or {}).values():
        count = int(item.get("count") or 0)
        if count < threshold:
            continue
        landmarks.append(
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "category": item.get("category"),
                "categoryLabel": item.get("categoryLabel"),
                "lat": item.get("lat"),
                "lng": item.get("lng"),
                "count": count,
            }
        )
    landmarks.sort(key=lambda item: (-item["count"], item.get("name") or ""))
    return landmarks


def _community_summary(lat: float, lng: float, threshold: int = COMMUNITY_LANDMARK_THRESHOLD) -> dict[str, Any]:
    data = _load_anonymous_signals()
    bucket_key = _location_bucket(lat, lng)
    bucket = (data.get("buckets") or {}).get(bucket_key) or {}
    requests_count = int(bucket.get("requests") or 0)
    type_counts = bucket.get("type_counts") or {}
    preferences = {
        "sampleCount": requests_count,
        "nodeTypeCounts": type_counts,
        "averageTargetMinutes": round((bucket.get("target_minutes_total") or 0) / requests_count, 1)
        if requests_count
        else None,
        "averageMaxSteps": round((bucket.get("max_steps_total") or 0) / requests_count)
        if requests_count
        else None,
        "averageMinSunlight": round((bucket.get("min_sunlight_total") or 0) / requests_count, 3)
        if requests_count
        else None,
    }
    return {
        **_bucket_label(bucket_key),
        "threshold": threshold,
        "preferences": preferences,
        "landmarks": _community_landmarks(lat, lng, threshold),
        "privacy": "anonymous_bucketed_no_login",
    }


def _social_landmark_bonus(nodes: list[dict[str, Any]], community_landmarks: list[dict[str, Any]]) -> float:
    landmark_counts = {str(item.get("id")): int(item.get("count") or 0) for item in community_landmarks}
    bonus = 0.0
    for node in nodes:
        count = landmark_counts.get(str(node.get("id")), 0)
        if count:
            bonus += min(0.15, 0.03 * count)
    return round(bonus, 4)


def _community_badges(nodes: list[dict[str, Any]], community_landmarks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    landmark_by_id = {str(item.get("id")): item for item in community_landmarks}
    badges = []
    for node in nodes:
        landmark = landmark_by_id.get(str(node.get("id")))
        if landmark:
            badges.append(
                {
                    "name": node.get("name"),
                    "categoryLabel": node.get("categoryLabel"),
                    "count": landmark.get("count"),
                }
            )
    return badges


def _record_anonymous_usage(request: RecommendationRequest, recommendations: list[dict[str, Any]]) -> None:
    if not request.collect_anonymous_usage:
        return
    data = _load_anonymous_signals()
    bucket = _get_bucket(data, request.lat, request.lng)
    bucket["requests"] = int(bucket.get("requests") or 0) + 1
    bucket["target_minutes_total"] = int(bucket.get("target_minutes_total") or 0) + request.target_minutes
    bucket["max_steps_total"] = int(bucket.get("max_steps_total") or 0) + request.max_steps
    bucket["min_sunlight_total"] = float(bucket.get("min_sunlight_total") or 0) + request.min_sunlight
    bucket["updated_at"] = datetime.now(timezone.utc).isoformat()

    type_counts = bucket.setdefault("type_counts", {})
    for place_type in request.required_types:
        type_counts[place_type] = int(type_counts.get(place_type) or 0) + 1

    landmarks = bucket.setdefault("landmarks", {})
    for recommendation in recommendations[:3]:
        for node in recommendation.get("nodes") or []:
            node_id = str(node.get("id"))
            if not node_id:
                continue
            item = landmarks.setdefault(
                node_id,
                {
                    "id": node_id,
                    "name": node.get("name"),
                    "category": node.get("category"),
                    "categoryLabel": node.get("categoryLabel"),
                    "lat": node.get("lat"),
                    "lng": node.get("lng"),
                    "count": 0,
                },
            )
            item["count"] = int(item.get("count") or 0) + 1
    _save_anonymous_signals(data)


def _build_recommendations(request: RecommendationRequest) -> dict[str, Any]:
    original_request = request
    request, schedule_info = _apply_schedule_offsets(request)
    speed_profile = _effective_walking_speed(request)
    selected_types = [item for item in request.required_types if item in SUPPORTED_PLACE_TYPES]
    if not selected_types:
        selected_types = ["supermarket", "park"]

    home = {"lat": request.lat, "lng": request.lng}
    weather = _fetch_weather(request.lat, request.lng)
    sun = _sun_position(request.lat, request.lng, datetime.now().astimezone())
    cloud_cover = float((weather.get("current") or {}).get("cloud_cover") or 0)
    community = _community_summary(request.lat, request.lng)
    community_landmarks = community["landmarks"]
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
            route = _apply_walking_speed(
                _compute_walk_route(home, home, route_points[1:-1]),
                speed_profile,
            )
        except HTTPException:
            continue

        route_sun_points = _decode_polyline(route.get("encodedPolyline")) or route_points
        sunlight_score = _estimate_path_sunlight(route_sun_points, sun, cloud_cover)
        sun_exposure = _sun_exposure_profile(route, sunlight_score)
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
            request.target_minutes,
        )
        social_bonus = _social_landmark_bonus(ordered_nodes, community_landmarks)
        score = round(score + social_bonus, 4)
        recommendations.append(
            {
                "title": _route_label(ordered_nodes, meets_all),
                "score": score,
                "socialBonus": social_bonus,
                "meetsAllConditions": meets_all,
                "violations": violation_list,
                "missingTypes": missing_types,
                "route": route,
                "sunlightScore": sunlight_score,
                "sunExposure": sun_exposure,
                "weatherScore": weather["walkability"]["score"],
                "nodes": ordered_nodes,
                "orderedPoints": route_points,
                "communityBadges": _community_badges(ordered_nodes, community_landmarks),
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

    recommendations = recommendations[:5]
    _record_anonymous_usage(request, recommendations)
    community = _community_summary(request.lat, request.lng)
    for recommendation in recommendations:
        recommendation["communityBadges"] = _community_badges(
            recommendation.get("nodes") or [],
            community["landmarks"],
        )

    return {
        "input": request.model_dump(),
        "originalInput": original_request.model_dump(),
        "schedule": schedule_info,
        "walkingSpeed": speed_profile,
        "community": community,
        "weather": weather,
        "sun": sun,
        "places": {
            "count": places["count"],
            "missingTypes": missing_types,
            "typeLabels": PLACE_TYPE_LABELS,
        },
        "recommendations": recommendations,
    }




@app.get("/assets/logo.png")
def logo_asset() -> FileResponse:
    logo_path = BASE_DIR / "logo.png"
    if not logo_path.exists():
        raise HTTPException(status_code=404, detail="logo.png 파일을 찾을 수 없습니다.")
    return FileResponse(logo_path)


@app.get("/", response_class=HTMLResponse)
def demo_page() -> str:
    maps_key = html.escape(get_google_maps_api_key())
    page = """
<!doctype html>
<html lang="ko">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>햇살동선 - 1인가구 산책 장보기 추천</title>
<style>
:root{--ink:#1c241c;--muted:#65705f;--soft:#f7f4e8;--surface:#fffdf4;--line:#d8dfcf;--leaf:#4e7f50;--leaf-dark:#315f39;--mint:#dbeee1;--sun:#f2b75b;--sun-soft:#fff0cf;--sky:#d8edf0;--shadow:0 14px 34px rgba(49,71,47,.14)}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:linear-gradient(160deg,var(--soft),#eef6ec 54%,var(--sky));color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;letter-spacing:0}button,input,textarea,summary{font:inherit}.app{width:min(100%,1180px);margin:0 auto;min-height:100vh;display:grid;grid-template-columns:minmax(340px,420px) minmax(0,1fr);gap:18px;padding:18px}.panel{min-height:calc(100vh - 36px);overflow:auto;border:1px solid rgba(78,127,80,.16);border-radius:26px;background:rgba(255,253,244,.95);box-shadow:var(--shadow)}.panel-inner{display:flex;flex-direction:column;gap:14px;padding:18px}.brand{display:grid;grid-template-columns:62px 1fr;gap:13px;align-items:center}.brand img{width:62px;height:62px;object-fit:contain;border-radius:18px;background:#fff7dc}.eyebrow{margin:0 0 3px;color:var(--leaf-dark);font-size:13px;font-weight:800}h1{margin:0;font-size:30px;line-height:1.08;letter-spacing:0}.lead{margin:6px 0 0;color:var(--muted);line-height:1.55;font-size:15px}.status-pill{display:flex;align-items:center;gap:9px;min-height:44px;padding:11px 13px;border-radius:16px;background:var(--mint);color:#2f5d37;font-weight:750;line-height:1.35}.status-dot{width:10px;height:10px;flex:0 0 auto;border-radius:999px;background:var(--sun);box-shadow:0 0 0 5px rgba(242,183,91,.22)}.today-weather{display:grid;gap:7px;padding:14px 15px;border:1px solid rgba(242,183,91,.36);border-radius:18px;background:linear-gradient(135deg,#fff8df,#eef7eb);box-shadow:inset 0 0 0 1px rgba(255,255,255,.45)}.today-date{color:#6b5930;font-size:13px;font-weight:850}.today-message{margin:0;color:#24452b;font-size:17px;font-weight:900;line-height:1.35}.today-meta{color:#65705f;font-size:12px;font-weight:750;line-height:1.4}.section{padding:15px;border:1px solid var(--line);border-radius:20px;background:rgba(255,255,255,.72)}.section-title{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:12px}.section-title h2{margin:0;font-size:17px}.hint{margin:0;color:var(--muted);font-size:13px;line-height:1.45}.field-grid,.button-row,.test-locations,.chips,.schedule-grid,.schedule-modes{display:grid;grid-template-columns:1fr 1fr;gap:10px}label.field{display:grid;gap:7px;color:var(--muted);font-size:13px;font-weight:700}input[type=text],input[type=datetime-local],textarea{width:100%;min-height:48px;border:1px solid var(--line);border-radius:14px;background:white;color:var(--ink);padding:12px 13px;outline:0}input:focus,textarea:focus{border-color:var(--leaf);box-shadow:0 0 0 4px rgba(78,127,80,.13)}.button-row{margin-top:12px}.btn{min-height:50px;border:0;border-radius:15px;display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:0 14px;cursor:pointer;font-weight:850;color:#1f3322;background:#eef3ea}.btn:active{transform:translateY(1px)}.btn.primary{background:var(--leaf);color:white}.btn.sun{background:var(--sun-soft);color:#724719}.btn.full{width:100%}.icon{font-size:18px;line-height:1}.range-list{display:grid;gap:15px}.range-field{display:grid;gap:8px}.range-head{display:flex;align-items:baseline;justify-content:space-between;gap:12px;color:var(--muted);font-size:14px;font-weight:750}.range-value{color:var(--ink);font-size:16px;font-weight:900}.range-note{margin:-2px 0 0;color:#8a8f83;font-size:12px;font-weight:700;line-height:1.45}input[type=range]{--range-progress:0%;--range-fill:var(--leaf);width:100%;height:30px;appearance:none;background:transparent;cursor:pointer}input[type=range]::-webkit-slider-runnable-track{height:8px;border-radius:999px;background:linear-gradient(to right,var(--range-fill) 0 var(--range-progress),#d8dfcf var(--range-progress) 100%)}input[type=range]::-webkit-slider-thumb{appearance:none;width:28px;height:28px;margin-top:-10px;border:2px solid #f2b75b;border-radius:999px;background-color:#fff8df;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23d99125' stroke-width='2.4' stroke-linecap='round'%3E%3Ccircle cx='12' cy='12' r='4.2' fill='%23f2b75b' stroke='%23d99125'/%3E%3Cpath d='M12 2.5v2.2M12 19.3v2.2M4.7 4.7l1.6 1.6M17.7 17.7l1.6 1.6M2.5 12h2.2M19.3 12h2.2M4.7 19.3l1.6-1.6M17.7 6.3l1.6-1.6'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:center;background-size:22px;box-shadow:0 2px 8px rgba(79,91,54,.22)}input[type=range]::-moz-range-track{height:8px;border-radius:999px;background:#d8dfcf}input[type=range]::-moz-range-progress{height:8px;border-radius:999px;background:var(--range-fill)}input[type=range]::-moz-range-thumb{width:26px;height:26px;border:2px solid #f2b75b;border-radius:999px;background:#fff8df}.chip{min-height:52px;display:flex;align-items:center;gap:10px;padding:10px 12px;border:1px solid var(--line);border-radius:15px;background:#fffef8;cursor:pointer;font-weight:780}.chip input{width:19px;height:19px;accent-color:var(--leaf)}.chip span:first-of-type{width:24px;text-align:center;color:var(--leaf-dark)}.chip:has(input:checked){border-color:rgba(78,127,80,.5);background:#eef7eb;box-shadow:inset 0 0 0 1px rgba(78,127,80,.18)}.shadow-tools{display:grid;gap:12px}.shadow-toggle{display:grid;grid-template-columns:34px 1fr;gap:10px;align-items:center;padding:12px;border:1px solid rgba(242,183,91,.45);border-radius:16px;background:#fff8df;color:#5c5032;cursor:pointer}.shadow-toggle input{position:absolute;opacity:0;pointer-events:none}.shadow-icon{width:34px;height:34px;border-radius:999px;display:grid;place-items:center;background:#f2b75b;color:#56380f;font-weight:900}.shadow-toggle strong{display:block;color:#24452b;font-size:15px}.shadow-toggle small{display:block;margin-top:3px;color:#65705f;font-size:12px;line-height:1.35}.shadow-toggle:has(input:checked){background:#eef7eb;border-color:rgba(78,127,80,.5);box-shadow:inset 0 0 0 1px rgba(78,127,80,.18)}.button-row.compact{margin-top:0}.shadow-summary{min-height:48px;padding:11px 12px;border-radius:14px;background:#f4f0dc;color:#5c5032;font-size:13px;font-weight:760;line-height:1.45}.shadow-legend{display:flex;flex-wrap:wrap;gap:8px;color:#65705f;font-size:12px;font-weight:750}.legend-item{display:inline-flex;align-items:center;gap:6px}.swatch{width:18px;height:10px;border-radius:999px;display:inline-block}.swatch.deep{background:#2f3940}.swatch.mid{background:#7fae67}.swatch.sun{background:#f2b75b}details{border:1px solid var(--line);border-radius:17px;background:rgba(255,255,255,.62);overflow:hidden}summary{min-height:48px;display:flex;align-items:center;cursor:pointer;padding:0 14px;color:var(--leaf-dark);font-weight:850}.details-body{display:grid;gap:12px;padding:0 14px 14px}.schedule-grid{grid-template-columns:repeat(4,1fr)}.schedule-modes{grid-template-columns:1fr 1fr}.schedule-option{min-height:44px;border:1px solid var(--line);border-radius:14px;background:#fffef8;color:#315f39;font-weight:820;cursor:pointer}.schedule-option.active{border-color:rgba(78,127,80,.58);background:#eef7eb;box-shadow:inset 0 0 0 1px rgba(78,127,80,.2)}.schedule-note{margin:0;padding:11px 12px;border-radius:14px;background:#f7f2df;color:#5c5032;font-size:13px;line-height:1.45}textarea{min-height:132px;resize:vertical;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;line-height:1.5}.privacy{display:flex;align-items:flex-start;gap:10px;padding:12px;border-radius:16px;background:#f4f0dc;color:#625332;font-size:13px;line-height:1.45}.privacy input{width:18px;height:18px;margin-top:2px;accent-color:var(--leaf)}.results{display:grid;gap:11px}.result-card{border:1px solid var(--line);border-radius:18px;background:#fffef8;padding:14px;cursor:pointer}.result-card:hover,.result-card.active{border-color:rgba(78,127,80,.56);box-shadow:0 10px 24px rgba(78,127,80,.12)}.result-top{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:10px}.result-title{margin:0;font-size:16px;line-height:1.35}.badge{display:inline-flex;align-items:center;min-height:27px;border-radius:999px;padding:0 9px;background:var(--mint);color:var(--leaf-dark);font-size:12px;font-weight:850;white-space:nowrap}.badge.warn{background:#ffe7d6;color:#8b4b2d}.badge.sunny{background:var(--sun-soft);color:#7a501a}.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:10px 0}.metric{min-height:58px;border-radius:14px;background:#f4f7ef;padding:9px}.metric small{display:block;color:var(--muted);font-size:11px;font-weight:750}.metric strong{display:block;margin-top:4px;font-size:15px}.node-list{display:flex;flex-wrap:wrap;gap:7px;margin-top:10px}.node{display:inline-flex;align-items:center;max-width:100%;border-radius:999px;padding:7px 9px;background:#f7f2df;color:#5c5032;font-size:12px;font-weight:760}.empty{padding:16px;border:1px dashed var(--line);border-radius:16px;color:var(--muted);line-height:1.5;background:rgba(255,255,255,.55)}.ad-placeholder{min-height:86px;border:1px dashed #c6c6c6;border-radius:16px;background:#eeeeee;color:#777;display:grid;place-items:center;text-align:center;font-size:13px;font-weight:800;line-height:1.45}.ad-placeholder small{display:block;margin-top:4px;font-size:11px;font-weight:700;color:#8a8a8a}.map-shell{position:sticky;top:18px;height:calc(100vh - 36px);min-height:620px;border-radius:28px;overflow:hidden;border:1px solid rgba(78,127,80,.16);box-shadow:var(--shadow);background:#dfe8d8}#map{width:100%;height:100%}.map-caption{position:absolute;left:16px;right:16px;bottom:16px;z-index:2;display:flex;justify-content:space-between;align-items:center;gap:10px;padding:11px 13px;border-radius:17px;background:rgba(255,253,244,.92);color:var(--leaf-dark);font-size:13px;font-weight:800;box-shadow:0 12px 24px rgba(37,55,35,.12);pointer-events:none}.splash-screen{position:fixed;inset:0;z-index:9999;display:grid;place-items:center;background:linear-gradient(160deg,#b7dfbd,#8ecf98);transition:opacity .28s ease,visibility .28s ease}.splash-screen.is-hidden{opacity:0;visibility:hidden;pointer-events:none}.splash-content{display:grid;justify-items:center;gap:16px;padding:24px;text-align:center;transform:translateY(-10px)}.splash-logo{width:96px;height:96px;object-fit:contain;border-radius:24px;background:rgba(255,255,255,.22);box-shadow:0 18px 36px rgba(34,76,40,.16)}.splash-text{margin:0;color:white;font-size:18px;font-weight:900;line-height:1.35;text-shadow:0 1px 8px rgba(37,71,41,.16)}.splash-loading{position:absolute;left:0;right:0;bottom:0;height:5px;background:rgba(255,255,255,.26);overflow:hidden}.splash-loading::before{content:"";display:block;height:100%;width:100%;background:white;transform-origin:left;animation:splashLoad 1s ease-out forwards}@keyframes splashLoad{from{transform:scaleX(0)}to{transform:scaleX(1)}}.spinner{display:inline-block;width:16px;height:16px;border:2px solid rgba(255,255,255,.5);border-top-color:white;border-radius:50%;animation:spin .9s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}
@media(max-width:900px){body{background:var(--surface)}.app{display:flex;flex-direction:column;padding:0;gap:0}.panel{min-height:auto;border-radius:0;border-width:0 0 1px;box-shadow:none}.panel-inner{padding:15px}.map-shell{position:relative;top:auto;height:58vh;min-height:420px;border-radius:0;border-width:1px 0 0;box-shadow:none;order:-1}.brand{grid-template-columns:54px 1fr}.brand img{width:54px;height:54px;border-radius:16px}h1{font-size:27px}}
@media(max-width:430px){.panel-inner{padding:13px;gap:12px}.section{padding:13px;border-radius:18px}.field-grid,.button-row,.test-locations,.chips,.schedule-modes{grid-template-columns:1fr}.schedule-grid{grid-template-columns:1fr 1fr}.metrics{grid-template-columns:1fr 1fr}.map-shell{height:52vh;min-height:360px}}
</style>
</head>
<body>
<div id="splashScreen" class="splash-screen" role="status" aria-live="polite"><div class="splash-content"><img class="splash-logo" src="/assets/logo.png" alt="햇살동선 로고" /><p class="splash-text">오늘의 산책로를 알려드릴게요</p></div><div class="splash-loading" aria-hidden="true"></div></div>
<main class="app"><section class="panel" aria-label="코스 추천 조건"><div class="panel-inner">
<header class="brand"><img src="/assets/logo.png" alt="햇살동선 로고" /><div><p class="eyebrow">힘 덜 쓰는 주말 루틴</p><h1>햇살동선</h1><p class="lead">장보기, 편의점, 산책, 햇빛 보기까지 한 번에 묶어 오늘 걸을 길을 골라드려요.</p></div></header>
<div class="status-pill" aria-live="polite"><span class="status-dot"></span><span id="status">위치를 확인한 뒤 바로 코스를 추천할 수 있어요.</span></div>
<section class="today-weather" aria-live="polite"><div class="today-date" id="todayDate">오늘</div><p class="today-message" id="todayWeatherMessage">선택한 위치의 날씨를 확인하고 있어요.</p><div class="today-meta" id="todayWeatherMeta">날씨 확인 중</div></section>
<section class="section"><div class="section-title"><h2>출발 위치</h2><p class="hint">버튼으로 빠르게 설정</p></div><input id="lat" type="hidden" value="36.366113" /><input id="lng" type="hidden" value="127.345172" /><div class="button-row"><button id="btnCurrent" class="btn sun" type="button"><span class="icon">⌖</span>현재 위치</button><button id="btnMove" class="btn" type="button"><span class="icon">↗</span>지도 이동</button></div><div class="test-locations" aria-label="테스트용 위치"><button class="btn" type="button" data-test-lat="37.774900" data-test-lng="-122.419400" data-test-name="샌프란시스코"><span class="icon">1</span>테스트용 위치</button><button class="btn" type="button" data-test-lat="51.507400" data-test-lng="-0.127800" data-test-name="런던"><span class="icon">2</span>테스트용 위치</button><button class="btn" type="button" data-test-lat="35.676200" data-test-lng="139.650300" data-test-name="도쿄"><span class="icon">3</span>테스트용 위치</button></div></section>
<section class="section"><div class="section-title"><h2>오늘의 여유</h2><p class="hint">힘이 남는 만큼만</p></div><div class="range-list"><label class="range-field"><span class="range-head">찾을 범위 <strong id="radiusValue" class="range-value">1200m</strong></span><input id="radius" type="range" min="500" max="3000" step="100" value="1200" /></label><label class="range-field"><span class="range-head">걷고 싶은 시간 <strong id="targetMinutesValue" class="range-value">35분</strong></span><input id="targetMinutes" type="range" min="10" max="90" step="5" value="35" /></label><label class="range-field"><span class="range-head">오늘 가능한 걸음 <strong id="maxStepsValue" class="range-value">4500보</strong></span><input id="maxSteps" type="range" min="1000" max="12000" step="500" value="4500" /></label><label class="range-field"><span class="range-head">내 걸음 속도 <strong id="walkingSpeedValue" class="range-value">보통 걸음</strong></span><input id="walkingSpeed" type="range" min="0.6" max="1.8" step="0.1" value="1.1" /></label><label class="range-field"><span class="range-head">햇빛은 이 정도 <strong id="minSunlightValue" class="range-value">햇빛 조금</strong></span><input id="minSunlight" type="range" min="0" max="100" step="5" value="35" /><p class="range-note">성인 기준 햇빛 노출은 보통 5~15분씩 주 2~3회 정도로 안내돼요.</p></label></div></section>
<section class="section"><div class="section-title"><h2>들르고 싶은 곳</h2><p class="hint">필요한 일만 체크</p></div><div class="chips"><label class="chip"><input type="checkbox" name="nodeType" value="convenience_store" checked /><span>⌂</span>편의점</label><label class="chip"><input type="checkbox" name="nodeType" value="supermarket" checked /><span>▦</span>마트</label><label class="chip"><input type="checkbox" name="nodeType" value="grocery_store" /><span>▤</span>식료품점</label><label class="chip"><input type="checkbox" name="nodeType" value="market" /><span>◇</span>시장</label><label class="chip"><input type="checkbox" name="nodeType" value="park" checked /><span>◎</span>공원</label></div></section>
<section class="section"><div class="section-title"><h2>햇빛과 그늘</h2><p class="hint">지도에서 바로 확인</p></div><div class="shadow-tools"><label class="shadow-toggle"><input id="experimental3d" type="checkbox" checked /><span class="shadow-icon">◫</span><span><strong>입체 그림자 기준</strong><small>태양 높이, 방향, 구름량으로 그늘 후보를 더 진하게 보여줘요.</small></span></label><div class="button-row compact"><button id="btnShadow3d" class="btn sun" type="button"><span class="icon">◐</span>그늘 지도 보기</button><button id="btnClearShadow" class="btn" type="button"><span class="icon">×</span>표시 지우기</button></div><div id="shadowSummary" class="shadow-summary">그늘 지도 보기를 누르면 현재 위치 주변의 햇빛과 그림자 후보를 지도에 표시해요.</div><div class="shadow-legend" aria-label="그늘 범례"><span class="legend-item"><span class="swatch deep"></span>그늘 진 곳</span><span class="legend-item"><span class="swatch mid"></span>반그늘</span><span class="legend-item"><span class="swatch sun"></span>햇빛 많은 곳</span></div></div></section>
<details><summary>반복 일정과 함께 쓰기</summary><div class="details-body"><label class="field">오늘 적용할 날짜와 시간<input id="scheduledAt" type="datetime-local" /></label><div class="schedule-grid" aria-label="요일 선택"><button class="schedule-option active" type="button" data-weekday="5">토요일</button><button class="schedule-option" type="button" data-weekday="6">일요일</button><button class="schedule-option" type="button" data-weekday="0">월요일</button><button class="schedule-option" type="button" data-weekday="all">매일</button></div><div class="schedule-modes" aria-label="일정 모드"><button class="schedule-option active" type="button" data-schedule-mode="easy">가볍게 걷기</button><button class="schedule-option" type="button" data-schedule-mode="relaxed">여유 있게 걷기</button></div><p class="schedule-note" id="schedulePreview">토요일에는 평소보다 조금 여유 있게 추천해요.</p></div></details>
<label class="privacy"><input id="collectAnonymousUsage" type="checkbox" checked /><span>로그인 없이 익명으로만 설정을 모아, 다른 사용자가 자주 고른 산책명소를 추천에 반영합니다.</span></label>
<button id="btnRecommend" class="btn primary full" type="button"><span class="icon">◎</span>오늘 코스 추천받기</button><section class="results" id="results" aria-label="추천 코스 결과"><div class="empty">추천을 누르면 가장 덜 피곤한 코스부터 보여드릴게요.</div></section><aside class="ad-placeholder" aria-label="광고 영역">광고 영역<small>배너 placeholder</small></aside>
</div></section><section class="map-shell" aria-label="추천 경로 지도"><div id="map"></div><div class="map-caption"><span>초록 선은 추천 동선, 실제 경로가 없으면 추정선으로 표시</span><span>Google Maps</span></div></section></main>
<script>
window.addEventListener("load",()=>{const splash=document.getElementById("splashScreen");if(!splash)return;setTimeout(()=>{splash.classList.add("is-hidden");setTimeout(()=>splash.remove(),320)},1000)});
let map,centerMarker,routePolyline,nodeMarkers=[],shadowRects=[],recommendations=[],weatherCardRequestId=0;const typeIcons={convenience_store:"⌂",supermarket:"▦",grocery_store:"▤",market:"◇",park:"◎"},typeLabels={convenience_store:"편의점",supermarket:"마트",grocery_store:"식료품점",market:"시장",park:"공원"};
function setStatus(m,l=false){document.getElementById("status").innerHTML=l?`<span class="spinner" aria-hidden="true"></span> ${m}`:m}function providerLabel(source){return source==="KMA"?"기상청 실황":source==="Google Weather"?"Google Weather":source==="Open-Meteo"?"Open-Meteo 보조":"날씨 확인"}function formatTodayLabel(){return new Date().toLocaleDateString("ko-KR",{year:"numeric",month:"long",day:"numeric",weekday:"long"})}function weatherWalkMessage(weather){const c=weather.current||{},walk=weather.walkability||{},normal=walk.normalized||{},condition=weather.conditionKo||"날씨",temp=Number(normal.temperatureC??c.temperature_2m),rain=Number(normal.precipitationMm??c.rain??c.precipitation??0),wind=Number(normal.windKmh??c.wind_speed_10m??0);if(!walk.walkable)return"산책하기에 조금 조심스러운 날씨예요";if(rain>0.5)return"산책하기에 비를 피하며 짧게 걷기 좋은 날씨예요";if(wind>35)return"산책하기에 바람을 살피며 걷기 좋은 날씨예요";if(Number.isFinite(temp)&&temp>=30)return"산책하기에 그늘 위주가 좋은 날씨예요";if(Number.isFinite(temp)&&temp<=0)return"산책하기에 따뜻하게 입으면 괜찮은 날씨예요";if(String(condition).includes("맑")||String(condition).includes("구름 없음"))return"산책하기에 맑고 가벼운 날씨예요";if(String(condition).includes("흐림")||String(condition).includes("구름"))return"산책하기에 차분하게 걷기 좋은 날씨예요";return`산책하기에 ${walk.summary||"괜찮은"} 날씨예요`}function setWeatherCardLoading(){document.getElementById("todayDate").textContent=formatTodayLabel();document.getElementById("todayWeatherMessage").textContent="선택한 위치의 날씨를 확인하고 있어요.";document.getElementById("todayWeatherMeta").textContent="날씨 확인 중"}async function updateWeatherCard(){const requestId=++weatherCardRequestId,center=readCenter();document.getElementById("todayDate").textContent=formatTodayLabel();try{const weather=await fetchJson(`/api/weather?lat=${center.lat}&lng=${center.lng}`);if(requestId!==weatherCardRequestId)return;const c=weather.current||{},temp=Number(c.temperature_2m);document.getElementById("todayWeatherMessage").textContent=weatherWalkMessage(weather);document.getElementById("todayWeatherMeta").textContent=`${weather.conditionKo||"날씨 확인"} · ${providerLabel(weather.source)}${Number.isFinite(temp)?` · ${Math.round(temp)}°C`:""}`}catch(e){if(requestId!==weatherCardRequestId)return;document.getElementById("todayWeatherMessage").textContent="날씨를 불러오지 못했지만 코스 추천은 가능해요.";document.getElementById("todayWeatherMeta").textContent="잠시 후 다시 확인"}}function num(id){return Number(String(document.getElementById(id).value).trim())}function readCenter(){return{lat:num("lat"),lng:num("lng")}}function selectedTypes(){return Array.from(document.querySelectorAll('input[name="nodeType"]:checked')).map(i=>i.value)}function activeScheduleValue(){return document.querySelector("[data-weekday].active")?.dataset.weekday||"5"}function activeScheduleWeekdays(){const value=activeScheduleValue();return value==="all"?[0,1,2,3,4,5,6]:[Number(value)]}function setScheduledAtFromActiveDay(){const input=document.getElementById("scheduledAt"),value=activeScheduleValue(),now=new Date(),target=new Date(now);if(value!=="all"){const wanted=(Number(value)+1)%7,diff=(wanted-now.getDay()+7)%7;target.setDate(now.getDate()+diff)}const pad=(n)=>String(n).padStart(2,"0");input.value=`${target.getFullYear()}-${pad(target.getMonth()+1)}-${pad(target.getDate())}T${pad(target.getHours())}:${pad(target.getMinutes())}`}function readScheduleOffsets(){const mode=document.querySelector("[data-schedule-mode].active")?.dataset.scheduleMode||"easy";const values=mode==="relaxed"?{name:"여유 있게 걷기",target_minutes_offset:20,max_steps_offset:2000}:{name:"가볍게 걷기",target_minutes_offset:-10,max_steps_offset:-1000};const date=(document.getElementById("scheduledAt").value||"").slice(0,10)||null;return activeScheduleWeekdays().map((weekday)=>({weekday,date,...values}))}function updateSchedulePreview(){const day=document.querySelector("[data-weekday].active")?.textContent?.trim()||"토요일",mode=document.querySelector("[data-schedule-mode].active")?.dataset.scheduleMode||"easy",text=mode==="relaxed"?`${day}에는 평소보다 조금 더 여유 있게 추천해요.`:`${day}에는 부담을 줄여 짧고 가볍게 추천해요.`;document.getElementById("schedulePreview").textContent=text}function friendlyRangeText(id,value,suffix){if(id==="walkingSpeed"){if(value<0.9)return"천천히 걷기";if(value<1.4)return"보통 걸음";return"빠르게 걷기"}if(id==="minSunlight"){if(value<25)return"그늘 위주";if(value<55)return"햇빛 조금";if(value<80)return"햇빛 충분";return"햇빛 많이"}return`${Number(value).toLocaleString("ko-KR")}${suffix}`}function rangeFillColor(ratio){const start=[78,127,80],end=[242,183,91],rgb=start.map((v,i)=>Math.round(v+(end[i]-v)*ratio));return`rgb(${rgb[0]}, ${rgb[1]}, ${rgb[2]})`}function updateRangeStyle(input){const min=Number(input.min||0),max=Number(input.max||100),value=Number(input.value),ratio=Math.max(0,Math.min(1,(value-min)/(max-min||1)));input.style.setProperty("--range-progress",`${ratio*100}%`);input.style.setProperty("--range-fill",rangeFillColor(ratio))}function syncRange(id,suffix){const input=document.getElementById(id),target=document.getElementById(`${id}Value`),render=()=>{target.textContent=friendlyRangeText(id,Number(input.value),suffix);updateRangeStyle(input)};input.addEventListener("input",render);render()}
window.initMap=function(){const center=readCenter();map=new google.maps.Map(document.getElementById("map"),{center,zoom:17,mapTypeControl:false,fullscreenControl:true,streetViewControl:false,clickableIcons:true,gestureHandling:"greedy"});centerMarker=new google.maps.Marker({position:center,map,title:"출발 위치",label:"집"});map.addListener("click",e=>moveCenter(e.latLng.lat(),e.latLng.lng()));setStatus("지도에서 출발 위치를 누르거나 현재 위치를 사용할 수 있어요.");setWeatherCardLoading();updateWeatherCard()};
function moveCenter(lat,lng){const pos={lat,lng};document.getElementById("lat").value=lat.toFixed(6);document.getElementById("lng").value=lng.toFixed(6);if(map&&centerMarker){map.setCenter(pos);centerMarker.setPosition(pos)}updateWeatherCard()}function clearRouteAndMarkers(){if(routePolyline){routePolyline.setMap(null);routePolyline=null}nodeMarkers.forEach(m=>m.setMap(null));nodeMarkers=[]}function clearMapLayers(){clearRouteAndMarkers();shadowRects.forEach(r=>r.setMap(null));shadowRects=[]}
async function fetchJson(url,opt){const res=await fetch(url,opt),raw=await res.text();let data={};try{data=raw?JSON.parse(raw):{}}catch{data={detail:raw||"서버가 JSON이 아닌 응답을 반환했습니다."}}if(!res.ok)throw new Error(data.detail||`요청 실패 (${res.status})`);return data}function colorForSunlight(s){return s>=.65?"#f2b75b":s>=.35?"#7fae67":"#53636d"}function colorForShadow(s){return s>=.65?"#2f3940":s>=.35?"#7fae67":"#f2b75b"}function updateShadowSummary(data){const cells=data.cells||[],avg=cells.reduce((sum,c)=>sum+Number(c.shadowScore||0),0)/(cells.length||1),deep=cells.filter(c=>Number(c.shadowScore||0)>=.55).length,mode=data.experimental3d?.enabled?"입체 그림자 기준":"기본 햇빛 기준",sun=data.sun||{};document.getElementById("shadowSummary").textContent=`${mode}으로 주변 ${cells.length}칸을 확인했어요. 그늘 후보 ${deep}칸, 평균 그늘감 ${Math.round(avg*100)}%, 태양 방향 ${Math.round(sun.azimuthDeg||0)}°입니다.`}function drawShadowLookup(data,mode="sunlight"){shadowRects.forEach(r=>r.setMap(null));shadowRects=[];if(!map||!data.cells)return;data.cells.forEach(c=>{const score=mode==="shadow"?Number(c.shadowScore||0):Number(c.sunlightScore||0),r=new google.maps.Rectangle({bounds:{north:c.north,south:c.south,east:c.east,west:c.west},strokeOpacity:mode==="shadow"?.18:0,strokeColor:"#fff8df",strokeWeight:1,fillColor:mode==="shadow"?colorForShadow(score):colorForSunlight(score),fillOpacity:mode==="shadow"?.14+score*.34:.18,map});shadowRects.push(r)});updateShadowSummary(data)}async function inspectShadows(){const btn=document.getElementById("btnShadow3d"),center=readCenter(),use3d=document.getElementById("experimental3d").checked;try{btn.disabled=true;btn.innerHTML='<span class="spinner" aria-hidden="true"></span>그늘 확인 중';setStatus("태양 방향과 구름량으로 그림자 후보를 계산하고 있어요.",true);const data=await fetchJson(`/api/shadow-lookup?lat=${center.lat}&lng=${center.lng}&radius=${num("radius")}&grid_size=12&experimental3d=${use3d}`);drawShadowLookup(data,"shadow");if(map){map.setMapTypeId("satellite");map.setTilt(use3d?45:0);map.setHeading(data.sun?.azimuthDeg||0)}setStatus(`${use3d?"입체 그림자 기준":"기본 햇빛 기준"}으로 그늘 후보를 지도에 표시했어요.`)}catch(e){setStatus(e.message||"그늘 정보를 확인하지 못했습니다.")}finally{btn.disabled=false;btn.innerHTML='<span class="icon">◐</span>그늘 지도 보기'}}function clearShadowView(){shadowRects.forEach(r=>r.setMap(null));shadowRects=[];document.getElementById("shadowSummary").textContent="그늘 지도 보기를 누르면 현재 위치 주변의 햇빛과 그림자 후보를 지도에 표시해요.";if(map){map.setMapTypeId("roadmap");map.setTilt(0)}setStatus("그늘 표시를 지웠어요.")}
function drawRecommendation(item,index=0){clearRouteAndMarkers();if(item.route&&item.route.encodedPolyline&&window.google?.maps?.geometry?.encoding){const path=google.maps.geometry.encoding.decodePath(item.route.encodedPolyline),isFallback=Boolean(item.route.fallback);routePolyline=new google.maps.Polyline({path,map,strokeColor:"#4e7f50",strokeOpacity:isFallback ? .72 : .96,strokeWeight:isFallback?5:6});const b=new google.maps.LatLngBounds();path.forEach(p=>b.extend(p));if(!b.isEmpty())map.fitBounds(b,52)}(item.nodes||[]).forEach((n,i)=>nodeMarkers.push(new google.maps.Marker({position:{lat:n.lat,lng:n.lng},map,label:String(i+1),title:`${n.categoryLabel||typeLabels[n.type]||"장소"} · ${n.name}`})));document.querySelectorAll(".result-card").forEach((c,i)=>c.classList.toggle("active",i===index))}
function fmtMeters(m){if(!Number.isFinite(Number(m)))return"-";return Number(m)>=1000?`${(Number(m)/1000).toFixed(1)}km`:`${Math.round(Number(m))}m`}function renderResults(data){recommendations=data.recommendations||[];const box=document.getElementById("results"),weatherSourceLabel=providerLabel(data.weather?.source);box.innerHTML="";if(!recommendations.length){box.innerHTML='<div class="empty">조건에 맞는 코스를 찾지 못했어요. 범위를 조금 넓히거나 들를 곳을 줄여보세요.</div>';return}recommendations.forEach((item,index)=>{const card=document.createElement("article");card.className="result-card";card.tabIndex=0;const title=index===0?"가장 가벼운 코스":`후보 코스 ${index+1}`,exposure=item.sunExposure||{},fallbackSunMinutes=Math.max(0,Math.round((item.route?.durationMinutes||0)*(item.sunlightScore||0))),sunLabel=exposure.label||`${fallbackSunMinutes?`햇빛 약 ${fallbackSunMinutes}분`:"햇빛 거의 없음"}`,sunDetail=exposure.detailLabel||`${sunLabel} · 그늘 약 ${Math.max(0,Math.round((item.route?.durationMinutes||0)-fallbackSunMinutes))}분`,weather=Math.round((item.weatherScore||0)*100),bonus=Math.round(item.socialBonus||0),fallbackBadge=item.route?.fallback?'<span class="badge warn">추정 경로</span>':"",nodes=(item.nodes||[]).map(n=>`<span class="node">${typeIcons[n.type]||"·"} ${n.categoryLabel||typeLabels[n.type]||"장소"} · ${n.name}</span>`).join(""),badges=(item.communityBadges||[]).map(b=>`<span class="node">추천 산책명소 · ${b.name}</span>`).join("");card.innerHTML=`<div class="result-top"><div><h3 class="result-title">${title}</h3><div class="node-list"><span class="badge ${item.meetsAllConditions?"":"warn"}">${item.meetsAllConditions?"조건에 맞아요":"조건에 가까워요"}</span>${fallbackBadge}<span class="badge sunny">${sunLabel}</span>${bonus?`<span class="badge">함께 고른 장소 +${bonus}점</span>`:""}</div></div></div><div class="metrics"><div class="metric"><small>거리</small><strong>${fmtMeters(item.route?.distanceMeters)}</strong></div><div class="metric"><small>시간</small><strong>${Math.round(item.route?.durationMinutes||0)}분</strong></div><div class="metric"><small>걸음 약</small><strong>${Number(item.route?.steps||0).toLocaleString("ko-KR")}보</strong></div></div><div class="node-list"><span class="node">날씨 ${weather}점 · ${weatherSourceLabel}</span><span class="node">${sunDetail}</span>${badges}${nodes}</div>`;card.addEventListener("click",()=>drawRecommendation(item,index));card.addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();drawRecommendation(item,index)}});box.appendChild(card)});drawRecommendation(recommendations[0],0)}
async function recommend(){const btn=document.getElementById("btnRecommend");try{const center=readCenter();if(!Number.isFinite(center.lat)||!Number.isFinite(center.lng))throw new Error("위도와 경도를 숫자로 입력해주세요.");const requiredTypes=selectedTypes();if(!requiredTypes.length)throw new Error("들르고 싶은 곳을 하나 이상 선택해주세요.");const scheduleOffsets=readScheduleOffsets();clearMapLayers();btn.disabled=true;btn.innerHTML='<span class="spinner" aria-hidden="true"></span>코스를 찾는 중';setStatus("날씨, 햇빛, 생활 장소를 함께 살펴보고 있어요.",true);const shadow=await fetchJson(`/api/shadow-lookup?lat=${center.lat}&lng=${center.lng}&radius=${num("radius")}&grid_size=9&experimental3d=${document.getElementById("experimental3d").checked}`);drawShadowLookup(shadow,"sunlight");const payload={lat:center.lat,lng:center.lng,radius:num("radius"),target_minutes:num("targetMinutes"),max_steps:num("maxSteps"),min_sunlight:num("minSunlight")/100,walking_speed_mps:num("walkingSpeed"),required_types:requiredTypes,experimental3d:document.getElementById("experimental3d").checked,scheduled_at:document.getElementById("scheduledAt").value||null,schedule_offsets:scheduleOffsets,collect_anonymous_usage:document.getElementById("collectAnonymousUsage").checked};const data=await fetchJson("/api/recommendations",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});renderResults(data);const weather=data.weather||{},walk=weather.walkability||{},statusPrefix=walk.walkable?"오늘 걷기 좋은 코스를 찾았어요":"날씨를 살피며 걸을 코스를 찾았어요";setStatus(`${statusPrefix}. ${recommendations.length}개 중 가장 가벼운 길부터 보여드릴게요.`)}catch(e){setStatus(e.message||"추천 중 문제가 생겼습니다.")}finally{btn.disabled=false;btn.innerHTML='<span class="icon">◎</span>오늘 코스 추천받기'}}
document.getElementById("btnMove").addEventListener("click",()=>{const c=readCenter();moveCenter(c.lat,c.lng);setStatus("선택한 위치로 지도를 이동했어요.")});document.getElementById("btnCurrent").addEventListener("click",()=>{if(!navigator.geolocation){setStatus("이 브라우저에서는 현재 위치를 사용할 수 없어요.");return}setStatus("현재 위치를 확인하고 있어요.",true);navigator.geolocation.getCurrentPosition(p=>{moveCenter(p.coords.latitude,p.coords.longitude);setStatus("현재 위치를 출발점으로 설정했어요.")},()=>setStatus("현재 위치 권한이 없어 기본 위치를 유지합니다."),{enableHighAccuracy:true,timeout:7000})});document.querySelectorAll("[data-test-lat]").forEach((button)=>button.addEventListener("click",()=>{moveCenter(Number(button.dataset.testLat),Number(button.dataset.testLng));setStatus(`${button.dataset.testName} 테스트 위치로 설정했어요.`)}));document.querySelectorAll("[data-weekday],[data-schedule-mode]").forEach((button)=>button.addEventListener("click",()=>{const group=button.dataset.weekday!==undefined?"[data-weekday]":"[data-schedule-mode]";document.querySelectorAll(group).forEach((item)=>item.classList.remove("active"));button.classList.add("active");if(button.dataset.weekday!==undefined)setScheduledAtFromActiveDay();updateSchedulePreview()}));setScheduledAtFromActiveDay();updateSchedulePreview();document.getElementById("btnShadow3d").addEventListener("click",inspectShadows);document.getElementById("btnClearShadow").addEventListener("click",clearShadowView);document.getElementById("btnRecommend").addEventListener("click",recommend);[["radius","m"],["targetMinutes","분"],["maxSteps","보"],["walkingSpeed",""],["minSunlight",""]].forEach(([id,s])=>syncRange(id,s));
</script>
<script async defer src="https://maps.googleapis.com/maps/api/js?key=__MAPS_KEY__&libraries=geometry&callback=initMap"></script>
</body>
</html>
"""
    return page.replace("__MAPS_KEY__", maps_key)


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "googleMapsApiKeyConfigured": bool(get_google_maps_api_key()),
        "googleWeatherApiKeyConfigured": bool(get_google_weather_api_key()),
        "kmaApiKeyConfigured": bool(get_kma_api_key()),
        "weatherProviderRouting": {
            "domestic": "KMA",
            "international": "Google Weather",
            "fallback": "Open-Meteo",
        },
        "anonymousSignalsConfigured": True,
        "anonymousSignalsPath": str(ANONYMOUS_SIGNALS_PATH),
        "dependencies": {
            "fastapi": _dependency_version("fastapi"),
            "uvicorn": _dependency_version("uvicorn"),
            "requests": _dependency_version("requests"),
            "pydantic": _dependency_version("pydantic"),
        },
    }


@app.get("/api/community/preferences")
def get_community_preferences(
    lat: float = Query(...),
    lng: float = Query(...),
    threshold: int = Query(default=COMMUNITY_LANDMARK_THRESHOLD, ge=1, le=100),
) -> dict[str, Any]:
    return _community_summary(lat, lng, threshold)


@app.get("/api/community/landmarks")
def get_community_landmarks(
    lat: float = Query(...),
    lng: float = Query(...),
    threshold: int = Query(default=COMMUNITY_LANDMARK_THRESHOLD, ge=1, le=100),
) -> dict[str, Any]:
    return {
        **_bucket_label(_location_bucket(lat, lng)),
        "threshold": threshold,
        "landmarks": _community_landmarks(lat, lng, threshold),
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
    experimental3d: bool = Query(default=False),
) -> dict[str, Any]:
    return _make_shadow_lookup(lat, lng, radius, grid_size, _parse_datetime(datetime_value), experimental3d)


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
    walking_speed_mps: float | None = Query(
        default=None,
        ge=MIN_WALKING_SPEED_MPS,
        le=MAX_WALKING_SPEED_MPS,
    ),
) -> dict[str, Any]:
    speed_profile = {
        "speedMps": walking_speed_mps or DEFAULT_WALKING_SPEED_MPS,
        "source": "user_average" if walking_speed_mps is not None else "default",
        "sampleCount": 0,
    }
    route = _apply_walking_speed(
        _compute_walk_route(
            {"lat": start_lat, "lng": start_lng},
            {"lat": end_lat, "lng": end_lng},
        ),
        speed_profile,
    )
    return {"walkingSpeed": speed_profile, "route": route}


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
    walking_speed_mps: float | None = Query(
        default=None,
        ge=MIN_WALKING_SPEED_MPS,
        le=MAX_WALKING_SPEED_MPS,
    ),
) -> dict[str, Any]:
    return get_walk_route(start_lat, start_lng, end_lat, end_lng, walking_speed_mps)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
