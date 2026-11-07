"""SQLite 기상 데이터 적재소.

DB 파일은 backend/weather_logs.db 에 자동 생성된다.
모든 쿼리는 파라미터 바인딩(?)을 사용하여 SQL 인젝션을 방지한다.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DB_PATH = Path(__file__).resolve().parent / "weather_logs.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS weather_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    temperature REAL,
    condition   TEXT NOT NULL,
    timestamp   TEXT NOT NULL
);
-- 같은 관측 시각(timestamp)은 한 번만 저장되도록 유일 인덱스를 둔다.
CREATE UNIQUE INDEX IF NOT EXISTS ux_weather_logs_timestamp ON weather_logs (timestamp);
"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """커넥션을 열고, 블록이 정상 종료되면 commit / 예외면 rollback 후 항상 close 한다."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    """DB 파일과 테이블이 없으면 생성한다. 이미 있으면 아무 것도 하지 않는다."""
    with connect() as conn:
        conn.executescript(SCHEMA)


def insert_log_once(temperature: float | None, condition: str, timestamp: str) -> int | None:
    """같은 timestamp 가 없을 때만 INSERT 한다.

    반환: 새로 INSERT 된 행의 id, 이미 같은 timestamp 가 있어 건너뛰었으면 None.
    """
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO weather_logs (temperature, condition, timestamp) VALUES (?, ?, ?)",
            (temperature, condition, timestamp),
        )
        if cur.rowcount == 0:
            return None
        return int(cur.lastrowid)


def list_logs(limit: int = 50, offset: int = 0) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, temperature, condition, timestamp FROM weather_logs "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [dict(row) for row in rows]


def get_log(log_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT id, temperature, condition, timestamp FROM weather_logs WHERE id = ?",
            (log_id,),
        ).fetchone()
    return dict(row) if row else None


def get_log_by_timestamp(timestamp: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT id, temperature, condition, timestamp FROM weather_logs WHERE timestamp = ?",
            (timestamp,),
        ).fetchone()
    return dict(row) if row else None
