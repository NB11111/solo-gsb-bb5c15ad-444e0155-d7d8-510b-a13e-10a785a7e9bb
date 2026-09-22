"""FastAPI 入口：跨来源事件对齐服务。

路由
----
* ``GET  /health``             健康检查
* ``POST /alignments``         提交两组事件，执行对齐并落库
* ``GET  /alignments``         历史任务列表
* ``GET  /alignments/{id}``    取回某次任务的完整结果（含未匹配清单与逐对解释）
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import List

import sqlite3
from fastapi import Depends, FastAPI, HTTPException

from . import db
from .matching import Event, align_events
from .schemas import (
    AlignRequest,
    AlignResult,
    EventOut,
    JobSummary,
    MAX_EVENTS_PER_SIDE,
    PairOut,
)

DB_PATH = os.getenv("ALIGN_DB_PATH", "/data/alignment.db")


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect(DB_PATH)
    try:
        db.init_db(conn)
    finally:
        conn.close()
    yield


app = FastAPI(
    title="跨来源事件对齐 API",
    description=(
        "提交两组带时间戳、来源键和载荷摘要的事件；服务在容差窗口内建立候选边，"
        "用最小总时间差的一对一匹配对齐它们，平局按事件 ID 稳定决胜，未匹配事件保留返回。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


def get_conn() -> sqlite3.Connection:
    conn = db.connect(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def _to_event(raw) -> Event:
    ts = raw.timestamp
    if ts.tzinfo is None:
        # 无时区的朴素时间戳按 UTC 解释，保证差值计算语义明确
        ts = ts.replace(tzinfo=timezone.utc)
    return Event(
        id=raw.id,
        timestamp=ts,
        source_key=raw.source_key,
        payload_summary=raw.payload_summary,
    )


def _validate_side(events, side: str) -> None:
    if len(events) > MAX_EVENTS_PER_SIDE:
        raise HTTPException(
            status_code=400,
            detail=f"{side} 侧事件数 {len(events)} 超过上限 {MAX_EVENTS_PER_SIDE}",
        )
    seen = set()
    for ev in events:
        if ev.id in seen:
            raise HTTPException(
                status_code=400,
                detail=f"{side} 侧存在重复事件 ID: {ev.id}",
            )
        seen.add(ev.id)


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _event_out(row: sqlite3.Row) -> EventOut:
    return EventOut(
        id=row["event_id"],
        timestamp=_parse_iso(row["timestamp"]),
        source_key=row["source_key"],
        payload_summary=row["payload_summary"],
    )


def _build_result(conn: sqlite3.Connection, job_id: str) -> AlignResult:
    """从 SQLite 重建完整响应（证明结果已持久化）。"""
    job = db.get_job(conn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {job_id}")

    match_rows = db.get_matches(conn, job_id)
    matched_a_ids = {row["a_event_id"] for row in match_rows}
    matched_b_ids = {row["b_event_id"] for row in match_rows}

    pairs: List[PairOut] = []
    for row in match_rows:
        a_out = EventOut(
            id=row["a_event_id"],
            timestamp=_parse_iso(row["a_timestamp"]),
            source_key=row["a_source_key"],
            payload_summary=row["a_payload_summary"],
        )
        b_out = EventOut(
            id=row["b_event_id"],
            timestamp=_parse_iso(row["b_timestamp"]),
            source_key=row["b_source_key"],
            payload_summary=row["b_payload_summary"],
        )
        pairs.append(
            PairOut(
                a=a_out,
                b=b_out,
                delta_seconds=row["delta_seconds"],
                absolute_delta_seconds=row["absolute_delta"],
                explanation=row["explanation"],
            )
        )

    unmatched_a = [
        _event_out(row)
        for row in db.get_events(conn, job_id, "a")
        if row["event_id"] not in matched_a_ids
    ]
    unmatched_b = [
        _event_out(row)
        for row in db.get_events(conn, job_id, "b")
        if row["event_id"] not in matched_b_ids
    ]

    return AlignResult(
        job_id=job["job_id"],
        tolerance_seconds=job["tolerance_seconds"],
        counts=json.loads(job["counts"]),
        candidate_edges=job["candidate_edges"],
        total_absolute_delta_seconds=job["total_delta_seconds"],
        pairs=pairs,
        unmatched_a=unmatched_a,
        unmatched_b=unmatched_b,
    )


@app.post("/alignments", response_model=AlignResult, status_code=201)
def create_alignment(req: AlignRequest, conn: sqlite3.Connection = Depends(get_conn)):
    _validate_side(req.events_a, "events_a")
    _validate_side(req.events_b, "events_b")

    events_a = [_to_event(e) for e in req.events_a]
    events_b = [_to_event(e) for e in req.events_b]

    result = align_events(events_a, events_b, req.tolerance_seconds)

    job_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc)
    db.save_job(
        conn,
        job_id=job_id,
        created_at=created_at,
        tolerance_seconds=req.tolerance_seconds,
        events_a=events_a,
        events_b=events_b,
        result=result,
    )
    return _build_result(conn, job_id)


@app.get("/alignments", response_model=List[JobSummary])
def list_alignments(conn: sqlite3.Connection = Depends(get_conn)):
    summaries = []
    for row in db.list_jobs(conn):
        summaries.append(
            JobSummary(
                job_id=row["job_id"],
                created_at=_parse_iso(row["created_at"]),
                tolerance_seconds=row["tolerance_seconds"],
                counts=json.loads(row["counts"]),
            )
        )
    return summaries


@app.get("/alignments/{job_id}", response_model=AlignResult)
def get_alignment(job_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    return _build_result(conn, job_id)
