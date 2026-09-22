"""FastAPI 端到端测试（使用内存/临时 SQLite）。"""

import os

import pytest

# 在导入 app.main 之前指定临时数据库路径
os.environ.setdefault("ALIGN_DB_PATH", "/tmp/test_alignment_api.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app, DB_PATH  # noqa: E402


@pytest.fixture(scope="module")
def client():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    with TestClient(app) as c:
        yield c


def _event(eid, ts, source, summary=""):
    return {
        "id": eid,
        "timestamp": ts,
        "source_key": source,
        "payload_summary": summary,
    }


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_create_and_persist_alignment(client):
    body = {
        "events_a": [
            _event("a1", "2026-09-22T10:00:00Z", "sensor-a", "door open"),
            _event("a2", "2026-09-22T10:00:10Z", "sensor-a", "door close"),
            _event("a3", "2026-09-22T10:05:00Z", "sensor-a", "lonely"),
        ],
        "events_b": [
            _event("b1", "2026-09-22T10:00:02Z", "camera-b", "motion"),
            _event("b2", "2026-09-22T10:00:09Z", "camera-b", "motion ends"),
        ],
        "tolerance_seconds": 5,
    }
    r = client.post("/alignments", json=body)
    assert r.status_code == 201, r.text
    data = r.json()

    assert data["job_id"]
    assert data["counts"] == {
        "events_a": 3,
        "events_b": 2,
        "matched": 2,
        "unmatched_a": 1,
        "unmatched_b": 0,
    }
    assert {(p["a"]["id"], p["b"]["id"]) for p in data["pairs"]} == {
        ("a1", "b1"),
        ("a2", "b2"),
    }
    pair = next(p for p in data["pairs"] if p["a"]["id"] == "a1")
    assert pair["delta_seconds"] == 2.0
    assert pair["absolute_delta_seconds"] == 2.0
    assert "b1" in pair["explanation"]
    assert pair["a"]["payload_summary"] == "door open"

    assert [e["id"] for e in data["unmatched_a"]] == ["a3"]
    assert data["unmatched_b"] == []
    assert data["total_absolute_delta_seconds"] == 3.0
    assert data["candidate_edges"] == 2

    # 通过 GET 取回（验证持久化）
    job_id = data["job_id"]
    r2 = client.get(f"/alignments/{job_id}")
    assert r2.status_code == 200
    assert r2.json()["pairs"] == data["pairs"]

    r3 = client.get("/alignments")
    assert r3.status_code == 200
    assert any(j["job_id"] == job_id for j in r3.json())


def test_get_unknown_job_404(client):
    r = client.get("/alignments/does-not-exist")
    assert r.status_code == 404


def test_duplicate_id_rejected(client):
    body = {
        "events_a": [
            _event("dup", "2026-09-22T10:00:00Z", "s"),
            _event("dup", "2026-09-22T10:00:01Z", "s"),
        ],
        "events_b": [],
        "tolerance_seconds": 1,
    }
    r = client.post("/alignments", json=body)
    assert r.status_code == 400
    assert "重复事件 ID" in r.json()["detail"]


def test_validation_error(client):
    r = client.post(
        "/alignments",
        json={"events_a": [], "events_b": [], "tolerance_seconds": -1},
    )
    assert r.status_code == 422


def test_naive_timestamp_treated_as_utc(client):
    body = {
        "events_a": [_event("a1", "2026-09-22T10:00:00", "s")],
        "events_b": [_event("b1", "2026-09-22T10:00:03Z", "s")],
        "tolerance_seconds": 5,
    }
    r = client.post("/alignments", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["pairs"][0]["delta_seconds"] == 3.0


def test_timezone_aware_diff(client):
    # +08:00 的 18:00:00 与 UTC 10:00:00 是同一时刻
    body = {
        "events_a": [_event("a1", "2026-09-22T18:00:00+08:00", "s")],
        "events_b": [_event("b1", "2026-09-22T10:00:00Z", "s")],
        "tolerance_seconds": 1,
    }
    r = client.post("/alignments", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["pairs"][0]["absolute_delta_seconds"] == 0.0
