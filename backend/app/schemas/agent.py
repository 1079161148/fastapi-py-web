"""坐席工作台的请求/响应模型（M6，设计文档 5.4）。"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.chat import MessageInfo
from app.schemas.handoff import HandoffTicketInfo


class AgentConversationInfo(BaseModel):
    """坐席列表项：会话本身 + 进行中的工单（没有则 null）。"""

    id: uuid.UUID
    title: str
    channel: str
    owner_type: str
    status: str
    updated_at: datetime
    ticket: HandoffTicketInfo | None = None


class AgentConversationListResponse(BaseModel):
    items: list[AgentConversationInfo]
    next_cursor: str | None = None


class AgentTicketListResponse(BaseModel):
    items: list[HandoffTicketInfo]
    next_cursor: str | None = None


class AgentReplyRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    content: str = Field(min_length=1, max_length=8000)


class AgentReplyResult(BaseModel):
    """坐席回复结果。

    delivered=False 表示消息已落库但没能送到原渠道（微信窗口关闭、SMTP 故障等）：
    坐席需要看到这个事实，而不是以为用户已经收到了。
    """

    message: MessageInfo
    delivered: bool
    delivery_error: str = ""
