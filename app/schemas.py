"""请求/响应的 Pydantic 模型。"""

from __future__ import annotations

from datetime import datetime
from typing import List

from pydantic import BaseModel, Field, field_validator

# 每一侧允许提交的最大事件数（纯 Python MCMF 的规模保护）
MAX_EVENTS_PER_SIDE = 250


class EventIn(BaseModel):
    id: str = Field(..., min_length=1, description="事件 ID，在各自分组内唯一")
    timestamp: datetime = Field(..., description="RFC 3339 时间戳，建议带时区（如 2026-09-22T10:00:00Z）")
    source_key: str = Field(..., min_length=1, description="来源键，如 sensor-a / feed-b")
    payload_summary: str = Field("", description="载荷摘要（自由文本，原样保存并随结果返回）")

    @field_validator("id", "source_key")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空字符串")
        return v


class AlignRequest(BaseModel):
    events_a: List[EventIn] = Field(
        ..., description="第一组（A 侧）事件，组内 id 必须唯一"
    )
    events_b: List[EventIn] = Field(
        ..., description="第二组（B 侧）事件，组内 id 必须唯一"
    )
    tolerance_seconds: float = Field(
        ..., ge=0, description="容差窗口（秒），|Δt| 不超过该值才建立候选边"
    )


class EventOut(BaseModel):
    id: str
    timestamp: datetime
    source_key: str
    payload_summary: str


class PairOut(BaseModel):
    a: EventOut
    b: EventOut
    delta_seconds: float = Field(..., description="带符号差值（B - A，秒），负数表示 B 早于 A")
    absolute_delta_seconds: float = Field(..., description="绝对时间差（秒），即匹配费用")
    explanation: str = Field(
        ..., description="该对差值的人类可读解释"
    )


class AlignResult(BaseModel):
    job_id: str
    tolerance_seconds: float
    counts: dict
    candidate_edges: int
    total_absolute_delta_seconds: float
    pairs: List[PairOut]
    unmatched_a: List[EventOut]
    unmatched_b: List[EventOut]


class JobSummary(BaseModel):
    job_id: str
    created_at: datetime
    tolerance_seconds: float
    counts: dict
