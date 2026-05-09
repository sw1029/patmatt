import os
import requests
from typing import Any
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

app = FastAPI()

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "YOUR_API_KEY_HERE")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def ensure_api_key() -> None:
        if not GOOGLE_MAPS_API_KEY or GOOGLE_MAPS_API_KEY == "YOUR_API_KEY_HERE":
                raise HTTPException(
                        status_code=400,
                        detail="GOOGLE_MAPS_API_KEY가 설정되지 않았습니다. 환경변수를 설정한 뒤 다시 시도하세요."
                )


def _parse_duration_seconds(duration_text: str | None) -> int | None:
    if not duration_text:
        return None
    # Routes duration is normally like "534s".
    if duration_text.endswith("s"):
        number = duration_text[:-1]
        if number.isdigit():
            return int(number)
    return None


def _normalize_place(place: dict[str, Any]) -> dict[str, Any]:
    location = place.get("location") or {}
    latitude = location.get("latitude")
    longitude = location.get("longitude")
    name = (place.get("displayName") or {}).get("text")
    place_types = place.get("types") or []

    return {
        "name": name,
        "address": place.get("formattedAddress"),
        "lat": latitude,
        "lng": longitude,
        "types": place_types,
        "isConveni": "convenience_store" in place_types,
    }


def _normalize_route(route: dict[str, Any]) -> dict[str, Any]:
    duration_text = route.get("duration")
    distance_m = route.get("distanceMeters")
    duration_s = _parse_duration_seconds(duration_text)
    speed_mps = None
    if isinstance(distance_m, (int, float)) and isinstance(duration_s, int) and duration_s > 0:
        speed_mps = round(distance_m / duration_s, 2)

    return {
        "distanceMeters": distance_m,
        "durationText": duration_text,
        "durationSeconds": duration_s,
        "avgSpeedMps": speed_mps,
        "encodedPolyline": ((route.get("polyline") or {}).get("encodedPolyline")),
    }


@app.get("/", response_class=HTMLResponse)
def demo_page() -> str:
        html = """
<!doctype html>
<html lang="ko">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Places + Routes + Maps JS API 데모</title>
    <style>
        :root {
            --bg: #f4f7fb;
            --card: #ffffff;
            --text: #17212f;
            --muted: #5b6878;
            --line: #d8e0ec;
            --accent: #0b6bcb;
            --accent-2: #0f9d58;
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            font-family: "Segoe UI", "Noto Sans KR", sans-serif;
            color: var(--text);
            background: radial-gradient(circle at 10% 10%, #eaf4ff 0%, var(--bg) 45%);
        }
        .wrap {
            max-width: 1100px;
            margin: 24px auto;
            padding: 0 16px;
            display: grid;
            gap: 16px;
            grid-template-columns: 340px 1fr;
        }
        .card {
            background: var(--card);
            border: 1px solid var(--line);
            border-radius: 16px;
            box-shadow: 0 8px 22px rgba(16, 33, 55, 0.06);
            padding: 14px;
        }
        .title {
            margin: 0 0 8px;
            font-size: 18px;
            font-weight: 700;
        }
        .note {
            margin: 0 0 12px;
            font-size: 13px;
            color: var(--muted);
            line-height: 1.4;
        }
        .field { margin-bottom: 10px; }
        .field label {
            display: block;
            font-size: 13px;
            margin-bottom: 4px;
            color: var(--muted);
        }
        .field input {
            width: 100%;
            border: 1px solid var(--line);
            border-radius: 10px;
            padding: 10px;
            font-size: 14px;
        }
        .row {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
        }
        button {
            border: 0;
            border-radius: 10px;
            padding: 10px 12px;
            font-weight: 600;
            cursor: pointer;
        }
        .btn-primary {
            background: var(--accent);
            color: #fff;
        }
        .btn-secondary {
            background: #e9f4ff;
            color: #074a8f;
        }
        .btn-route {
            width: 100%;
            margin-top: 8px;
            background: var(--accent-2);
            color: #fff;
        }
        #map {
            width: 100%;
            height: 680px;
            border-radius: 16px;
            border: 1px solid var(--line);
            overflow: hidden;
        }
        #status {
            margin-top: 10px;
            font-size: 13px;
            color: var(--muted);
            min-height: 18px;
        }
        #places {
            margin: 10px 0 0;
            padding: 0;
            list-style: none;
            max-height: 280px;
            overflow: auto;
        }
        #places li {
            border: 1px solid var(--line);
            border-radius: 10px;
            padding: 10px;
            margin-bottom: 8px;
            cursor: pointer;
            font-size: 13px;
        }
        #places li.active {
            border-color: var(--accent);
            background: #f1f8ff;
        }
        .distance {
            margin-top: 8px;
            font-size: 13px;
            color: var(--muted);
        }
        @media (max-width: 900px) {
            .wrap { grid-template-columns: 1fr; }
            #map { height: 420px; }
        }
    </style>
</head>
<body>
    <div class="wrap">
        <section class="card">
            <h1 class="title">주변 편의점 + 도보 경로 테스트</h1>
            <p class="note">
                Places API(주변 검색), Routes API(도보 경로), Maps JavaScript API(지도 표시)를 한 화면에서 확인합니다.
            </p>

            <div class="field">
                <label for="lat">위도</label>
                <input id="lat" value="37.5665" />
            </div>
            <div class="field">
                <label for="lng">경도</label>
                <input id="lng" value="126.9780" />
            </div>
            <div class="field">
                <label for="radius">검색 반경(m)</label>
                <input id="radius" value="1000" />
            </div>

            <div class="row">
                <button class="btn-secondary" id="btnCurrent">현재 위치 사용</button>
                <button class="btn-primary" id="btnSearch">편의점 검색</button>
            </div>

            <button class="btn-route" id="btnRoute">선택한 편의점까지 도보 경로</button>
            <p class="distance" id="routeInfo"></p>
            <p id="status"></p>

            <ul id="places"></ul>
        </section>

        <section>
            <div id="map"></div>
        </section>
    </div>

    <script>
        let map;
        let centerMarker;
        let selectedPlace = null;
        let routePolyline = null;
        let placeMarkers = [];

        function setStatus(msg) {
            document.getElementById("status").textContent = msg || "";
        }

        function clearPlaceMarkers() {
            placeMarkers.forEach((m) => m.setMap(null));
            placeMarkers = [];
        }

        function clearRoute() {
            if (routePolyline) {
                routePolyline.setMap(null);
                routePolyline = null;
            }
            document.getElementById("routeInfo").textContent = "";
        }

        function initMap() {
            const start = { lat: 37.5665, lng: 126.9780 };
            map = new google.maps.Map(document.getElementById("map"), {
                center: start,
                zoom: 15,
                mapTypeControl: false,
            });
            centerMarker = new google.maps.Marker({
                position: start,
                map,
                title: "기준 위치",
                icon: "http://maps.google.com/mapfiles/ms/icons/blue-dot.png",
            });
        }

        function readCenter() {
            return {
                lat: Number(document.getElementById("lat").value),
                lng: Number(document.getElementById("lng").value),
                radius: Number(document.getElementById("radius").value || 1000),
            };
        }

        async function fetchJsonSafe(url) {
            let res;
            try {
                res = await fetch(url);
            } catch (e) {
                throw new Error("네트워크 오류로 서버에 연결하지 못했습니다.");
            }

            const raw = await res.text();
            let data = null;
            try {
                data = raw ? JSON.parse(raw) : {};
            } catch (e) {
                data = { detail: raw || "서버가 JSON이 아닌 응답을 반환했습니다." };
            }

            if (!res.ok) {
                throw new Error(data?.detail || `요청 실패 (${res.status})`);
            }
            return data;
        }

        function moveCenter(lat, lng) {
            const pos = { lat, lng };
            map.setCenter(pos);
            centerMarker.setPosition(pos);
            clearRoute();
            document.getElementById("lat").value = lat.toFixed(6);
            document.getElementById("lng").value = lng.toFixed(6);
        }

        async function searchPlaces() {
            const { lat, lng, radius } = readCenter();
            if (!Number.isFinite(lat) || !Number.isFinite(lng) || !Number.isFinite(radius)) {
                setStatus("위도/경도/반경 값을 확인하세요.");
                return;
            }

            selectedPlace = null;
            clearRoute();
            clearPlaceMarkers();
            setStatus("주변 편의점 검색 중...");

            let data;
            try {
                data = await fetchJsonSafe(`/api/nearby/convenience?lat=${lat}&lng=${lng}&radius=${radius}`);
            } catch (e) {
                setStatus(e.message || "편의점 검색 실패");
                return;
            }

            const places = data.places || [];
            const ul = document.getElementById("places");
            ul.innerHTML = "";

            if (!places.length) {
                setStatus("결과가 없습니다. 반경을 늘려보세요.");
                return;
            }

            places.forEach((p, idx) => {
                const lat = p.location?.latitude;
                const lng = p.location?.longitude;
                if (typeof lat !== "number" || typeof lng !== "number") return;

                const marker = new google.maps.Marker({
                    position: { lat, lng },
                    map,
                    title: p.displayName?.text || "편의점",
                });
                placeMarkers.push(marker);

                const li = document.createElement("li");
                li.innerHTML = `<strong>${p.displayName?.text || "이름 없음"}</strong><br>${p.formattedAddress || "주소 없음"}`;
                li.addEventListener("click", () => {
                    document.querySelectorAll("#places li").forEach((x) => x.classList.remove("active"));
                    li.classList.add("active");
                    selectedPlace = p;
                    map.panTo({ lat, lng });
                    map.setZoom(16);
                });
                ul.appendChild(li);

                if (idx === 0) {
                    li.classList.add("active");
                    selectedPlace = p;
                }
            });

            setStatus(`${places.length}개 편의점을 찾았습니다. 목록에서 하나를 선택하세요.`);
        }

        async function drawRoute() {
            const { lat: startLat, lng: startLng } = readCenter();
            if (!selectedPlace?.location) {
                setStatus("먼저 편의점을 검색하고 하나를 선택하세요.");
                return;
            }
            const endLat = selectedPlace.location.latitude;
            const endLng = selectedPlace.location.longitude;

            setStatus("도보 경로 계산 중...");
            clearRoute();

            const qs = new URLSearchParams({
                start_lat: String(startLat),
                start_lng: String(startLng),
                end_lat: String(endLat),
                end_lng: String(endLng),
            });
            let data;
            try {
                data = await fetchJsonSafe(`/api/route/walk?${qs.toString()}`);
            } catch (e) {
                setStatus(e.message || "경로 계산 실패");
                return;
            }

            const route = data.routes?.[0];
            const encoded = route?.polyline?.encodedPolyline;
            if (!encoded) {
                setStatus("경로 데이터가 없습니다.");
                return;
            }

            const path = google.maps.geometry.encoding.decodePath(encoded);
            routePolyline = new google.maps.Polyline({
                path,
                map,
                strokeColor: "#0f9d58",
                strokeOpacity: 0.95,
                strokeWeight: 6,
            });

            const bounds = new google.maps.LatLngBounds();
            path.forEach((p) => bounds.extend(p));
            map.fitBounds(bounds);

            const distance = route.distanceMeters ? `${route.distanceMeters}m` : "거리 없음";
            const duration = route.duration || "시간 없음";
            document.getElementById("routeInfo").textContent = `거리: ${distance}, 소요: ${duration}`;
            setStatus("경로 표시 완료");
        }

        document.getElementById("btnSearch").addEventListener("click", searchPlaces);
        document.getElementById("btnRoute").addEventListener("click", drawRoute);
        document.getElementById("btnCurrent").addEventListener("click", () => {
            if (!navigator.geolocation) {
                setStatus("브라우저에서 위치 정보를 지원하지 않습니다.");
                return;
            }
            navigator.geolocation.getCurrentPosition(
                (pos) => {
                    moveCenter(pos.coords.latitude, pos.coords.longitude);
                    setStatus("현재 위치를 기준점으로 설정했습니다.");
                },
                () => setStatus("위치 권한이 없거나 현재 위치를 가져오지 못했습니다.")
            );
        });
    </script>
    <script async defer src="https://maps.googleapis.com/maps/api/js?key=__MAPS_API_KEY__&libraries=geometry&callback=initMap"></script>
</body>
</html>
        """
        return html.replace("__MAPS_API_KEY__", GOOGLE_MAPS_API_KEY)


@app.get("/api/nearby/convenience")
def get_nearby_convenience(
    lat: float = Query(...),
    lng: float = Query(...),
    radius: float = 1000
):
    ensure_api_key()
    url = "https://places.googleapis.com/v1/places:searchNearby"

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
        "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.location,places.types"
    }

    body = {
        "includedTypes": ["convenience_store"],
        "maxResultCount": 10,
        "locationRestriction": {
            "circle": {
                "center": {
                    "latitude": lat,
                    "longitude": lng
                },
                "radius": radius
            }
        },
        "languageCode": "ko"
    }

    try:
        response = requests.post(url, headers=headers, json=body, timeout=20)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        detail = "Places API 호출 실패"
        if getattr(e, "response", None) is not None:
            detail = f"{detail}: {e.response.status_code} {e.response.text[:300]}"
        raise HTTPException(status_code=502, detail=detail) from e


@app.get("/api/nearby/convenience/normalized")
def get_nearby_convenience_normalized(
    lat: float = Query(...),
    lng: float = Query(...),
    radius: float = 1000
):
    raw = get_nearby_convenience(lat=lat, lng=lng, radius=radius)
    places = raw.get("places") or []
    items = [_normalize_place(p) for p in places]
    return {
        "count": len(items),
        "items": items,
    }


@app.get("/api/route/walk")
def get_walk_route(
    start_lat: float,
    start_lng: float,
    end_lat: float,
    end_lng: float
):
    ensure_api_key()
    url = "https://routes.googleapis.com/directions/v2:computeRoutes"

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
        "X-Goog-FieldMask": "routes.duration,routes.distanceMeters,routes.polyline.encodedPolyline"
    }

    body = {
        "origin": {
            "location": {
                "latLng": {
                    "latitude": start_lat,
                    "longitude": start_lng
                }
            }
        },
        "destination": {
            "location": {
                "latLng": {
                    "latitude": end_lat,
                    "longitude": end_lng
                }
            }
        },
        "travelMode": "WALK"
    }

    try:
        response = requests.post(url, headers=headers, json=body, timeout=20)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        detail = "Routes API 호출 실패"
        if getattr(e, "response", None) is not None:
            detail = f"{detail}: {e.response.status_code} {e.response.text[:300]}"
        raise HTTPException(status_code=502, detail=detail) from e


@app.get("/api/route/walk/normalized")
def get_walk_route_normalized(
    start_lat: float,
    start_lng: float,
    end_lat: float,
    end_lng: float
):
    raw = get_walk_route(
        start_lat=start_lat,
        start_lng=start_lng,
        end_lat=end_lat,
        end_lng=end_lng,
    )
    routes = raw.get("routes") or []
    first = routes[0] if routes else {}
    normalized = _normalize_route(first)
    return {
        "route": normalized,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)