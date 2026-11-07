"""myAISky - AI Weather & Agent Monitoring Dashboard (백엔드).

실행 (프로젝트 루트에서):
    run.bat                       (Windows, 더블클릭 또는 터미널에서 실행)
    python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000

환경변수:
    KMA_SERVICE_KEY  : 공공데이터포털 기상청 단기예보 서비스(VilageFcstInfoService_2.0) 인증키.
                       요청의 serviceKey 파라미터로 사용됩니다. 저장소에 키를 넣지 말고
                       환경변수로만 설정하세요.
                       키가 없거나 호출이 실패하면 목(mock) 데이터로 대체되어 서버는 계속 동작합니다.
    WEATHER_LOCATION : 조회할 지명 (LOCATIONS 목록 중 하나, 기본값: 서울).

구조:
    backend/main.py   : FastAPI 앱, 기상청 API 연동, 라우팅
    backend/db.py     : SQLite(weather_logs.db) 적재/조회
    frontend/         : 대시보드 HTML 및 정적 이미지 (백엔드가 서빙)
"""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend import db

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"
IMAGE_DIR = FRONTEND_DIR / "images"
INDEX_HTML = FRONTEND_DIR / "index.html"

# 기상청 단기예보 서비스 2.0 - 초단기실황조회
KMA_BASE_URL = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0"
KMA_CURRENT_URL = f"{KMA_BASE_URL}/getUltraSrtNcst"
KMA_SERVICE_KEY = os.getenv("KMA_SERVICE_KEY", "").strip()

# 지명 → 기상청 격자 좌표(nx, ny) 매핑.
# 실행 시 WEATHER_LOCATION 환경변수로 지명을 고른다 (기본값: 서울).
LOCATIONS = {
    "서울": (55, 127),
    "인천": (55, 124),
    "수원": (60, 121),
    "춘천": (73, 134),
    "강릉": (92, 131),
    "청주": (69, 107),
    "대전": (67, 100),
    "세종": (66, 103),
    "전주": (63, 89),
    "광주": (58, 74),
    "여수": (73, 66),
    "대구": (89, 90),
    "포항": (102, 94),
    "울산": (102, 84),
    "창원": (90, 77),
    "부산": (98, 76),
    "제주": (52, 38),
}


def _select_location() -> dict:
    name = os.getenv("WEATHER_LOCATION", "서울").strip()
    if name not in LOCATIONS:
        raise ValueError(
            f"WEATHER_LOCATION='{name}' 은(는) 지원하지 않는 지명입니다. "
            f"사용 가능: {', '.join(LOCATIONS)}"
        )
    nx, ny = LOCATIONS[name]
    return {"name": name, "nx": nx, "ny": ny}


DEFAULT_LOCATION = _select_location()

PTY_LABEL = {
    "0": "없음",
    "1": "비",
    "2": "비/눈",
    "3": "눈",
    "5": "빗방울",
    "6": "빗방울/눈날림",
    "7": "눈날림",
}

# 키가 없거나 API 호출이 실패했을 때 사용하는 예시 값
MOCK_RAW = {"T1H": "21.5", "REH": "48", "WSD": "2.1", "PTY": "0", "RN1": "0"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 서버 시작 시 weather_logs.db 와 테이블을 자동 생성한다.
    db.init_db()
    yield


app = FastAPI(title="myAISky", version="0.3.0", lifespan=lifespan)

# 배경 이미지 등 정적 파일 서빙: /images/sky.jpg 로 접근 가능 (실제 경로는 frontend/images)
app.mount("/images", StaticFiles(directory=IMAGE_DIR), name="images")


class WeatherResponse(BaseModel):
    location: str
    temperature_c: float | None
    humidity_pct: float | None
    wind_speed_ms: float | None
    precipitation_type: str
    rain_1h_mm: str | None
    status: str
    observed_at: str  # 기상청 관측 기준 시각 (KMA) 또는 mock 생성 시각
    source: str  # "KMA" 실데이터 | "mock" 예시 데이터
    saved_log_id: int | None = None  # 이번 호출로 DB에 새로 적재된 행 id (없으면 null)
    note: str | None = None


class WeatherLog(BaseModel):
    id: int
    temperature: float | None
    condition: str
    timestamp: str


def _redact(text: str) -> str:
    """오류 메시지에 포함될 수 있는 서비스키를 가린다 (URL 쿼리에 키가 실려 있기 때문)."""
    if KMA_SERVICE_KEY:
        text = text.replace(KMA_SERVICE_KEY, "***")
    return text


def _base_datetime(now: datetime) -> datetime:
    """초단기실황은 매시 40분 이후 발표되므로, 40분 이전이면 한 시간 전 정시를 기준으로 한다."""
    ref = now if now.minute >= 40 else now - timedelta(hours=1)
    return ref.replace(minute=0, second=0, microsecond=0)


def fetch_kma_current(nx: int, ny: int, now: datetime | None = None) -> tuple[dict[str, str], datetime]:
    """기상청 초단기실황을 호출한다.

    반환: ({카테고리: 관측값}, 관측 기준 시각)
    카테고리 예: T1H(기온), REH(습도), WSD(풍속), PTY(강수형태), RN1(1시간 강수량)
    """
    if not KMA_SERVICE_KEY:
        raise RuntimeError("KMA_SERVICE_KEY 환경변수가 설정되지 않았습니다.")

    base = _base_datetime(now or datetime.now())
    params = {
        "serviceKey": KMA_SERVICE_KEY,
        "pageNo": 1,
        "numOfRows": 100,
        "dataType": "JSON",
        "base_date": base.strftime("%Y%m%d"),
        "base_time": base.strftime("%H00"),
        "nx": nx,
        "ny": ny,
    }
    try:
        resp = requests.get(KMA_CURRENT_URL, params=params, timeout=5)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(_redact(f"KMA 요청 실패: {exc}")) from None

    try:
        body = resp.json()["response"]
    except (ValueError, KeyError):
        # 인증 실패 등은 JSON 대신 XML 오류 문서가 올 수 있다
        raise RuntimeError(f"KMA 응답 형식 오류 (HTTP {resp.status_code}) - 서비스키를 확인하세요.") from None

    if body["header"]["resultCode"] != "00":
        raise RuntimeError(f"KMA 오류: {body['header']['resultMsg']}")

    items = body["body"]["items"]["item"]
    if isinstance(items, dict):  # 항목이 1개면 리스트가 아닌 객체로 올 수 있다
        items = [items]
    values = {item["category"]: item["obsrValue"] for item in items}
    return values, base


def _to_float(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _build_response(raw: dict[str, str], source: str, observed_at: datetime, note: str | None = None) -> WeatherResponse:
    pty = str(raw.get("PTY", "0"))
    precipitation = PTY_LABEL.get(pty, "알 수 없음")
    return WeatherResponse(
        location=DEFAULT_LOCATION["name"],
        temperature_c=_to_float(raw.get("T1H")),
        humidity_pct=_to_float(raw.get("REH")),
        wind_speed_ms=_to_float(raw.get("WSD")),
        precipitation_type=precipitation,
        rain_1h_mm=raw.get("RN1"),
        status="강수 없음" if pty == "0" else f"{precipitation} 관측 중",
        observed_at=observed_at.isoformat(timespec="seconds"),
        source=source,
        note=note,
    )


def get_current_weather() -> WeatherResponse:
    """기상청 실황을 가져온다. 실패 시 mock 데이터로 대체하고 사유를 note에 담는다."""
    try:
        raw, observed_at = fetch_kma_current(DEFAULT_LOCATION["nx"], DEFAULT_LOCATION["ny"])
        return _build_response(raw, source="KMA", observed_at=observed_at)
    except Exception as exc:  # 네트워크/인증/파싱 오류 모두 대시보드가 죽지 않도록 흡수
        return _build_response(MOCK_RAW, source="mock", observed_at=datetime.now(), note=_redact(str(exc)))


def save_observation(data: WeatherResponse) -> int | None:
    """실데이터(KMA)만 weather_logs 에 적재한다. 같은 관측 시각이 이미 있으면 적재하지 않는다.

    반환: 새로 INSERT 된 행의 id, 적재하지 않았으면 None.
    """
    if data.source != "KMA":
        return None
    return db.insert_log_once(
        temperature=data.temperature_c,
        condition=data.precipitation_type,
        timestamp=data.observed_at,
    )


# ---------------------------------------------------------------------------
# 대시보드 / 현재 기상 API
# ---------------------------------------------------------------------------

@app.get("/", response_class=FileResponse)
def dashboard() -> FileResponse:
    return FileResponse(INDEX_HTML, media_type="text/html; charset=utf-8")


@app.get("/api/weather", response_model=WeatherResponse)
def api_weather() -> WeatherResponse:
    """기상청 실황을 조회하고, 새 관측분이면 DB에 자동 저장한 뒤 결과를 반환한다."""
    data = get_current_weather()
    try:
        data.saved_log_id = save_observation(data)
    except sqlite3.Error as exc:
        # DB 장애가 있어도 대시보드 조회는 계속 동작하도록 한다
        print(f"[myAISky] DB 적재 실패: {exc}", file=sys.stderr)
        data.note = f"DB 적재 실패: {exc}"
    return data


# ---------------------------------------------------------------------------
# SQLite 적재 허브 API
# ---------------------------------------------------------------------------

@app.post("/api/weather/logs", response_model=WeatherLog)
def create_weather_log() -> WeatherLog:
    """현재 실황을 지금 바로 적재한다.

    이미 같은 관측 시각의 행이 있으면 새로 만들지 않고 기존 행을 반환한다.
    mock 데이터는 실측값이 아니므로 적재하지 않고 503을 반환한다.
    """
    data = get_current_weather()
    if data.source != "KMA":
        raise HTTPException(status_code=503, detail=data.note or "실데이터를 가져오지 못했습니다.")

    log_id = save_observation(data)
    if log_id is None:
        row = db.get_log_by_timestamp(data.observed_at)
    else:
        row = db.get_log(log_id)
    assert row is not None  # 새로 저장했거나 같은 시각의 행이 이미 있으므로 존재해야 한다
    return WeatherLog(**row)


@app.get("/api/weather/logs", response_model=list[WeatherLog])
def list_weather_logs(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[WeatherLog]:
    """적재된 기상 로그를 최신순으로 조회한다."""
    return [WeatherLog(**row) for row in db.list_logs(limit=limit, offset=offset)]


@app.get("/api/weather/logs/{log_id}", response_model=WeatherLog)
def get_weather_log(log_id: int) -> WeatherLog:
    row = db.get_log(log_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"id={log_id} 로그가 없습니다.")
    return WeatherLog(**row)
