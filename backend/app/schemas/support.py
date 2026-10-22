"""M8 运营支撑模型：满意度评价、数据报表、客户管理。"""

import uuid

from pydantic import BaseModel, ConfigDict, Field


# ---- 满意度评价 ----
class RatingRequest(BaseModel):
    """对一条 assistant 消息打分。消息 id 在路径里，这里只带分数。"""

    score: int = Field(ge=1, le=5)


class RatingInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    message_id: uuid.UUID
    score: int


# ---- 数据报表 ----
class TrendPoint(BaseModel):
    date: str
    conversations: int
    messages: int
    ratings: int


class StatsOverview(BaseModel):
    """工作台 / 报表页的核心指标。

    时间窗固定为近 30 天：与 usage 概览一致，避免两处口径不同。
    """

    total_conversations: int
    ai_resolved: int
    human_handled: int
    pending_handoff: int
    handoff_rate: float
    avg_first_reply_ms: int | None
    satisfaction_avg: float | None
    satisfaction_count: int
    new_leave_messages: int
    trend: list[TrendPoint]


# ---- 客户管理 ----
class CustomerInfo(BaseModel):
    """按 owner（user / guest）聚合的客户视图。"""

    owner_type: str
    owner_id: str
    # 展示名：登录用户取账号信息，访客取"访客-<id 前 8 位>"
    display_name: str
    email: str | None = None
    conversation_count: int
    last_active_at: str | None = None
    # 最近一条会话的状态，便于直接看出"这个客户还在等人工"
    last_conversation_status: str = ""
    total_ratings: int
    avg_rating: float | None = None


class CustomerListResponse(BaseModel):
    items: list[CustomerInfo]
    total: int
