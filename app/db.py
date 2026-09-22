"""SQLite 持久化层：保存每次对齐任务、原始事件与匹配结果。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .matching import Alignment, Event, Pair

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id              TEXT PRIMARY KEY,
    created_at          TEXT NOT NULL,
    tolerance_seconds   REAL NOT NULL,
    total_delta_seconds REAL NOT NULL,
    candidate_edges     INTEGER NOT NULL,
    counts              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL REFERENCES jobs(job_id),
    side            TEXT NOT NULL CHECK (side IN ('a', 'b')),
    event_id        TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    source_key      TEXT NOT NULL,
    payload_summary TEXT NOT NULL,
    ordinal         INTEGER NOT NULL,
    UNIQUE (job_id, side, event_id)
);

CREATE TABLE IF NOT EXISTS matches (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id              TEXT NOT NULL REFERENCES jobs(job_id),
    a_event_id          TEXT NOT NULL,
    b_event_id          TEXT NOT NULL,
    a_timestamp         TEXT NOT NULL,
    b_timestamp         TEXT NOT NULL,
    a_source_key        TEXT NOT NULL,
    b_source_key        TEXT NOT NULL,
    a_payload_summary   TEXT NOT NULL,
    b_payload_summary   TEXT NOT NULL,
    delta_seconds       REAL NOT NULL,
    absolute_delta      REAL NOT NULL,
    explanation         TEXT NOT NULL,
    ordinal             INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_job ON events (job_id, side);
CREATE INDEX IF NOT EXISTS idx_matches_job ON matches (job_id);
"""


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def _iso(dt: datetime) -> str:
    """统一以带 UTC 偏移的 ISO 8601 文本落库。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def save_job(
    conn: sqlite3.Connection,
    job_id: str,
    created_at: datetime,
    tolerance_seconds: float,
    events_a: List[Event],
    events_b: List[Event],
    result: Alignment,
) -> None:
    counts = {
        "events_a": len(events_a),
        "events_b": len(events_b),
        "matched": len(result.pairs),
        "unmatched_a": len(result.unmatched_a),
        "unmatched_b": len(result.unmatched_b),
    }
    conn.execute(
        "INSERT INTO jobs (job_id, created_at, tolerance_seconds, total_delta_seconds, "
        "candidate_edges, counts) VALUES (?, ?, ?, ?, ?, ?)",
        (
            job_id,
            _iso(created_at),
            tolerance_seconds,
            result.total_absolute_delta_seconds,
            result.candidate_edges,
            json.dumps(counts, ensure_ascii=False),
        ),
    )

    rows = []
    for ordinal, ev in enumerate(events_a):
        rows.append((job_id, "a", ev.id, _iso(ev.timestamp), ev.source_key,
                     ev.payload_summary, ordinal))
    for ordinal, ev in enumerate(events_b):
        rows.append((job_id, "b", ev.id, _iso(ev.timestamp), ev.source_key,
                     ev.payload_summary, ordinal))
    conn.executemany(
        "INSERT INTO events (job_id, side, event_id, timestamp, source_key, "
        "payload_summary, ordinal) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )

    match_rows = []
    for ordinal, p in enumerate(result.pairs):
        match_rows.append((
            job_id,
            p.a_id,
            p.b_id,
            _iso(p.a_timestamp),
            _iso(p.b_timestamp),
            p.a_source_key,
            p.b_source_key,
            p.a_payload_summary,
            p.b_payload_summary,
            p.delta_seconds,
            p.absolute_delta_seconds,
            explain_pair(p),
            ordinal,
        ))
    conn.executemany(
        "INSERT INTO matches (job_id, a_event_id, b_event_id, a_timestamp, b_timestamp, "
        "a_source_key, b_source_key, a_payload_summary, b_payload_summary, "
        "delta_seconds, absolute_delta, explanation, ordinal) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        match_rows,
    )
    conn.commit()


def explain_pair(pair: Pair) -> str:
    """生成某一对匹配的差值解释。"""
    d = pair.delta_seconds
    direction = "晚于" if d > 0 else ("早于" if d < 0 else "同时于")
    return (
        f"B 事件 {pair.b_id}（来源 {pair.b_source_key}）时间戳 "
        f"{pair.b_timestamp.isoformat()} {direction} A 事件 {pair.a_id}"
        f"（来源 {pair.a_source_key}）时间戳 {pair.a_timestamp.isoformat()}；"
        f"带符号差值 = {d:.3f} 秒（B - A），绝对时间差 = "
        f"{pair.absolute_delta_seconds:.3f} 秒。"
    )


def list_jobs(conn: sqlite3.Connection, limit: int = 50) -> List[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT job_id, created_at, tolerance_seconds, counts FROM jobs "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,),
        )
    )


def get_job(conn: sqlite3.Connection, job_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()


def get_events(conn: sqlite3.Connection, job_id: str, side: str) -> List[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT event_id, timestamp, source_key, payload_summary FROM events "
            "WHERE job_id = ? AND side = ? ORDER BY ordinal",
            (job_id, side),
        )
    )


def get_matches(conn: sqlite3.Connection, job_id: str) -> List[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM matches WHERE job_id = ? ORDER BY ordinal", (job_id,)
        )
    )
