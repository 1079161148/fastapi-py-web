"""转人工工单的请求/响应模型（M6，设计文档 5.3）。"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class HandoffTicketInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    conversation_id: uuid.UUID
    agent_id: uuid.UUID | None
    status: str
    reason: str
    requested_by: str
    note: str
    # 访客最后一句话的摘要：坐席不用点开会话就知道大概
    summary: str
    claimed_at: datetime | None
    resolved_at: datetime | None
    created_at: datetime


class HandoffRequestPayload(BaseModel):
    """用户侧转人工：可选备注（例如"我的订单号是 123"）。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    note: str = Field(default="", max_length=500)


class HandoffResponse(BaseModel):
    ticket: HandoffTicketInfo
    # false 表示该会话已在队列中，本次只是回读（前端提示"已在排队"）
    created: bool
    conversation_status: str


class HandoffCancelResponse(BaseModel):
    """取消排队的结果。cancelled=false 表示本来就不在队列中（幂等，不是错误）。"""

    cancelled: bool
    conversation_status: str
