"""质检抽检与敏感词命中的模型（M9）。"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class QualityReviewInfo(BaseModel):
    """一条质检记录：抽检的会话 + 评分结果。"""

    id: uuid.UUID
    conversation_id: uuid.UUID
    # 会话摘要（列表里直接显示，不用再点进会话才知道评的是什么）
    conversation_title: str = ""
    conversation_status: str = ""
    channel: str = ""
    conversation_updated_at: datetime | None = None
    status: str
    score: int | None = None
    tags: list[str] = Field(default_factory=list)
    comment: str = ""
    reviewer_id: uuid.UUID | None = None
    sampled_at: datetime | None = None
    reviewed_at: datetime | None = None
    created_at: datetime


class QualityReviewSubmit(BaseModel):
    """提交评分。comment 可空：没问题的会话不需要硬编评语。"""

    score: int = Field(ge=1, le=5)
    tags: list[str] = Field(default_factory=list, max_length=10)
    comment: str = Field(default="", max_length=2000)


class SensitiveHitInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    conversation_id: uuid.UUID
    conversation_title: str = ""
    message_id: uuid.UUID | None = None
    words: list[str]
    action: str
    snippet: str = ""
    created_at: datetime


class QualityOverview(BaseModel):
    """质检概览（近 30 天）。"""

    window_days: int
    reviewed_count: int
    pending_count: int
    avg_score: float | None
    conversations_total: int
    # 已抽检 / 窗口内会话数：0.2 = 20% 的会话被看过
    coverage: float
    sensitive_hits: int
    # 问题标签分布：{"unresolved": 3, "tone": 1}
    tag_counts: dict[str, int] = Field(default_factory=dict)
