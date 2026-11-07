"""myAISky - AI Weather & Agent Monitoring Dashboard (1교시 백엔드).

실행:
    uvicorn app:app --reload --host 127.0.0.1 --port 8000

환경변수:
    KMA_API_KEY : 공공데이터포털(data.go.kr)에서 발급받은 기상청 단기예보 API 인증키.
                  키가 없거나 호출이 실패하면 목(mock) 데이터로 대체되어 서버는 계속 동작합니다.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
IMAGE_DIR = BASE_DIR / "images"

# 기상청 초단기실황조회 (공공데이터포털 단기예보 서비스 2.0)
KMA_URL = "http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getUltraSrtNcst"
KMA_API_KEY = os.getenv("KMA_API_KEY", "")

# 지명 → 기상청 격자 좌표(nx, ny) 매핑.
# 실행 시 WEATHER_LOCATION 환경변수로 지명을 고른다 (기본값: 서울).
# 격자값은 기상청 격자표 기준이며, 목록에 없는 지역은 격자 번호를 추가하면 된다.
LOCATIONS = {
    "서울": (60, 127),
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

app = FastAPI(title="myAISky", version="0.1.0")

# 배경 이미지 등 정적 파일 서빙: /images/sky.jpg 로 접근 가능
app.mount("/images", StaticFiles(directory=IMAGE_DIR), name="images")


class WeatherResponse(BaseModel):
    location: str
    temperature_c: float | None
    humidity_pct: float | None
    wind_speed_ms: float | None
    precipitation_type: str
    rain_1h_mm: str | None
    status: str
    observed_at: str
    source: str  # "KMA" 실데이터 | "mock" 예시 데이터
    note: str | None = None


def _base_datetime(now: datetime) -> tuple[str, str]:
    """초단기실황은 매시 40분 이후 발표되므로, 40분 이전이면 한 시간 전 정시를 조회한다."""
    ref = now if now.minute >= 40 else now - timedelta(hours=1)
    return ref.strftime("%Y%m%d"), ref.strftime("%H00")


def fetch_kma_current(nx: int, ny: int, now: datetime | None = None) -> dict[str, str]:
    """기상청 초단기실황을 호출해 {카테고리: 관측값} 딕셔너리를 반환한다.

    주의: 공공데이터포털에서 복사한 인증키가 이미 URL 인코딩된 형태라면
    requests가 한 번 더 인코딩하므로, 그 경우 디코딩된 키를 사용해야 한다.
    """
    if not KMA_API_KEY:
        raise RuntimeError("KMA_API_KEY 환경변수가 설정되지 않았습니다.")

    base_date, base_time = _base_datetime(now or datetime.now())
    params = {
        "serviceKey": KMA_API_KEY,
        "pageNo": 1,
        "numOfRows": 100,
        "dataType": "JSON",
        "base_date": base_date,
        "base_time": base_time,
        "nx": nx,
        "ny": ny,
    }
    resp = requests.get(KMA_URL, params=params, timeout=5)
    resp.raise_for_status()

    body = resp.json()["response"]
    if body["header"]["resultCode"] != "00":
        raise RuntimeError(f"KMA 오류: {body['header']['resultMsg']}")

    items = body["body"]["items"]["item"]
    return {item["category"]: item["obsrValue"] for item in items}


def _to_float(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _build_response(raw: dict[str, str], source: str, note: str | None = None) -> WeatherResponse:
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
        observed_at=datetime.now().isoformat(timespec="seconds"),
        source=source,
        note=note,
    )


def get_current_weather() -> WeatherResponse:
    """현재 기상 데이터를 가져온다. 실패 시 mock 데이터로 대체하고 사유를 note에 담는다."""
    try:
        raw = fetch_kma_current(DEFAULT_LOCATION["nx"], DEFAULT_LOCATION["ny"])
        return _build_response(raw, source="KMA")
    except Exception as exc:  # 네트워크/인증/파싱 오류 모두 대시보드가 죽지 않도록 흡수
        return _build_response(MOCK_RAW, source="mock", note=f"실데이터 사용 불가: {exc}")


@app.get("/api/weather", response_model=WeatherResponse)
def api_weather() -> WeatherResponse:
    return get_current_weather()


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>myAISky · AI Weather & Agent Monitoring Dashboard</title>
<style>
  :root {
    --cream: #F8F5EE;
    --ink: #2B2A27;
    --muted: #7A766C;
    --accent: #C8A96A;
    --charcoal: #1E1F24;
    --green: #2E7D32;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    min-height: 100vh;
    font-family: "Pretendard", "Malgun Gothic", "Apple SD Gothic Neo", sans-serif;
    color: var(--ink);
    background-image: url('/images/sky.jpg');
    background-size: cover;
    background-position: center;
    background-attachment: fixed;
    background-repeat: no-repeat;
  }
  .container {
    max-width: 1200px;
    margin: 32px auto;
    padding: 28px 32px 32px;
    background: rgba(248, 245, 238, 0.94);
    border-radius: 24px;
    box-shadow: 0 20px 50px rgba(0, 0, 0, 0.25);
  }
  header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 12px;
    padding-bottom: 20px;
    border-bottom: 1px solid rgba(43, 42, 39, 0.12);
  }
  h1 { margin: 0; font-size: 34px; letter-spacing: -0.5px; }
  .subtitle { margin: 4px 0 0; color: var(--muted); font-size: 15px; }
  .badge {
    padding: 8px 16px;
    border-radius: 999px;
    background: #E8F5E9;
    color: var(--green);
    font-weight: 600;
    font-size: 14px;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
  }
  .stats {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 16px;
    margin: 24px 0;
  }
  .card {
    background: #FFFFFF;
    border-radius: 18px;
    padding: 18px 20px;
    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.08);
  }
  .stat-label { color: var(--muted); font-size: 13px; }
  .stat-value { font-size: 26px; font-weight: 700; margin-top: 6px; }
  .main {
    display: grid;
    grid-template-columns: 1.4fr 1fr;
    gap: 20px;
  }
  .section-title { margin: 0 0 14px; font-size: 17px; }
  .detail-row {
    display: flex;
    justify-content: space-between;
    padding: 10px 0;
    border-bottom: 1px dashed rgba(43, 42, 39, 0.12);
    font-size: 15px;
  }
  .detail-row span:first-child { color: var(--muted); }
  #trend { margin-top: 16px; min-height: 160px; }
  #trend svg { width: 100%; height: 160px; }
  .trend-empty { color: var(--muted); font-size: 14px; padding: 60px 0; text-align: center; }
  .agent-panel {
    background: var(--charcoal);
    color: #E6E6E6;
    border-radius: 18px;
    padding: 18px 20px;
    height: 100%;
    min-height: 320px;
    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.25);
    display: flex;
    flex-direction: column;
  }
  .agent-panel h2 { margin: 0 0 12px; font-size: 17px; color: var(--accent); }
  #log {
    flex: 1;
    overflow-y: auto;
    font-family: Consolas, "D2Coding", monospace;
    font-size: 13px;
    line-height: 1.7;
    max-height: 280px;
  }
  .log-line { white-space: pre-wrap; word-break: break-all; }
  .ts { color: #8A8F98; }
  .lv { display: inline-block; min-width: 56px; font-weight: 700; }
  .lv-INFO { color: #7FB7FF; }
  .lv-AGENT { color: #C9A2FF; }
  .lv-OK { color: #7FD99A; }
  .lv-ERR { color: #FF8A8A; }
  .lv-REPORT { color: var(--accent); }
  .control {
    margin-top: 20px;
    padding: 20px 22px;
    background: #FFFFFF;
    border-radius: 18px;
    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.08);
  }
  .control-buttons { display: flex; gap: 12px; flex-wrap: wrap; }
  button {
    border: none;
    padding: 11px 20px;
    border-radius: 12px;
    font-size: 15px;
    font-weight: 600;
    cursor: pointer;
    background: var(--ink);
    color: var(--cream);
    transition: transform 0.1s ease, opacity 0.2s ease;
  }
  button.secondary { background: var(--accent); color: var(--ink); }
  button:hover { transform: translateY(-1px); }
  button:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }
  #report {
    margin: 16px 0 0;
    padding: 14px;
    background: #F2EEE4;
    border-radius: 12px;
    font-size: 14px;
    white-space: pre-wrap;
    display: none;
  }
  footer { margin-top: 20px; text-align: center; color: var(--muted); font-size: 13px; }
  @media (max-width: 900px) {
    .stats { grid-template-columns: repeat(2, 1fr); }
    .main { grid-template-columns: 1fr; }
    .container { margin: 12px; padding: 20px; }
  }
</style>
</head>
<body>
<div class="container">
  <header>
    <div>
      <h1>myAISky</h1>
      <p class="subtitle">AI Weather &amp; Agent Monitoring Dashboard</p>
    </div>
    <div class="badge">🟢 24/7 Sandbox Active</div>
  </header>

  <section class="stats">
    <div class="card"><div class="stat-label">현재 기온</div><div class="stat-value" id="s-temp">--</div></div>
    <div class="card"><div class="stat-label">습도</div><div class="stat-value" id="s-humid">--</div></div>
    <div class="card"><div class="stat-label">풍속</div><div class="stat-value" id="s-wind">--</div></div>
    <div class="card"><div class="stat-label">데이터 소스</div><div class="stat-value" id="s-source">--</div></div>
  </section>

  <section class="main">
    <div class="card">
      <h2 class="section-title">실시간 기상 데이터 분석 · 온도 트렌드</h2>
      <div class="detail-row"><span>지역</span><span id="d-loc">--</span></div>
      <div class="detail-row"><span>상태</span><span id="d-status">--</span></div>
      <div class="detail-row"><span>강수 형태</span><span id="d-pty">--</span></div>
      <div class="detail-row"><span>1시간 강수량</span><span id="d-rn1">--</span></div>
      <div class="detail-row"><span>관측 시각</span><span id="d-time">--</span></div>
      <div id="trend"><div class="trend-empty">동기화를 두 번 이상 하면 온도 추세가 표시됩니다.</div></div>
    </div>

    <div class="agent-panel">
      <h2>AI 에이전트 실시간 로그</h2>
      <div id="log"></div>
    </div>
  </section>

  <section class="control">
    <h2 class="section-title">Manual Control Panel</h2>
    <div class="control-buttons">
      <button id="btn-sync" onclick="syncWeather()">데이터 동기화</button>
      <button id="btn-report" class="secondary" onclick="generateReport()">리포트 생성</button>
    </div>
    <pre id="report"></pre>
  </section>

  <footer>myAISky · FastAPI backend · /api/weather</footer>
</div>

<script>
  const $ = (id) => document.getElementById(id);
  const readings = [];
  let lastData = null;

  function log(message, level = "INFO") {
    const box = $("log");
    const line = document.createElement("div");
    line.className = "log-line";

    const ts = document.createElement("span");
    ts.className = "ts";
    ts.textContent = new Date().toLocaleTimeString("ko-KR");

    const lv = document.createElement("span");
    lv.className = "lv lv-" + level;
    lv.textContent = " " + level.padEnd(6, " ") + " ";

    line.append(ts, lv, document.createTextNode(message));
    box.appendChild(line);
    box.scrollTop = box.scrollHeight;
  }

  function fmt(value, unit) {
    return value === null || value === undefined ? "--" : value + unit;
  }

  function setBusy(busy) {
    $("btn-sync").disabled = busy;
  }

  function render(d) {
    lastData = d;
    $("s-temp").textContent = fmt(d.temperature_c, "°C");
    $("s-humid").textContent = fmt(d.humidity_pct, "%");
    $("s-wind").textContent = fmt(d.wind_speed_ms, " m/s");
    $("s-source").textContent = d.source === "KMA" ? "KMA 실황" : "Mock";

    $("d-loc").textContent = d.location;
    $("d-status").textContent = d.status;
    $("d-pty").textContent = d.precipitation_type;
    $("d-rn1").textContent = d.rain_1h_mm ?? "--";
    $("d-time").textContent = d.observed_at.replace("T", " ");

    if (d.temperature_c !== null) readings.push(d.temperature_c);
    drawTrend();
  }

  function drawTrend() {
    const box = $("trend");
    if (readings.length < 2) return;

    const W = 600, H = 160, P = 20;
    const min = Math.min(...readings) - 1;
    const max = Math.max(...readings) + 1;
    const step = (W - P * 2) / (readings.length - 1);
    const points = readings.map((t, i) => {
      const x = P + i * step;
      const y = H - P - ((t - min) / (max - min)) * (H - P * 2);
      return x.toFixed(1) + "," + y.toFixed(1);
    });

    box.innerHTML =
      '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none">' +
      '<polyline fill="none" stroke="#C8A96A" stroke-width="3" points="' + points.join(" ") + '"/>' +
      readings.map((t, i) => {
        const [x, y] = points[i].split(",");
        return '<circle cx="' + x + '" cy="' + y + '" r="4" fill="#2B2A27"/>';
      }).join("") +
      "</svg>";
  }

  async function syncWeather() {
    setBusy(true);
    log("기상 데이터 동기화 요청 → /api/weather", "AGENT");
    try {
      const res = await fetch("/api/weather");
      if (!res.ok) throw new Error("HTTP " + res.status);
      const d = await res.json();
      render(d);
      log("동기화 완료 · " + d.location + " " + fmt(d.temperature_c, "°C") + " (" + d.source + ")", "OK");
      if (d.note) log(d.note, "INFO");
    } catch (err) {
      log("동기화 실패: " + err.message, "ERR");
    } finally {
      setBusy(false);
    }
  }

  function generateReport() {
    if (!lastData) {
      log("리포트를 만들 데이터가 없습니다. 먼저 동기화하세요.", "ERR");
      return;
    }
    const d = lastData;
    const text = [
      "[myAISky 기상 리포트]",
      "지역: " + d.location,
      "관측 시각: " + d.observed_at.replace("T", " "),
      "기온: " + fmt(d.temperature_c, "°C"),
      "습도: " + fmt(d.humidity_pct, "%"),
      "풍속: " + fmt(d.wind_speed_ms, " m/s"),
      "상태: " + d.status + " (" + d.precipitation_type + ")",
      "데이터 소스: " + d.source,
    ].join("\\n");
    const report = $("report");
    report.textContent = text;
    report.style.display = "block";
    log("리포트 생성 완료", "REPORT");
  }

  window.addEventListener("DOMContentLoaded", () => {
    log("대시보드 로드 · 백엔드 연결 확인", "INFO");
    syncWeather();
  });
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return DASHBOARD_HTML
