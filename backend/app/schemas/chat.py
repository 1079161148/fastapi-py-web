"""访客 / 会话 / 消息的请求响应模型（设计文档 5.5 / 第 7 章）。"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.knowledge import SourceRef


class GuestSessionInfo(BaseModel):
    """POST /guest/sessions 的响应。

    注意：guest_id 同时经 Set-Cookie 下发（HttpOnly），
    响应体里的 guest_id 仅供前端展示/调试，不构成凭证。
    """

    guest_id: uuid.UUID
    expires_at: datetime
    status: str
    # 租户欢迎语（M8）：访客未登录，拿不到 /tenants/current，只能随访客会话下发
    welcome_message: str = ""


class GuestSessionCurrent(BaseModel):
    """GET /guest/sessions/current：无访客会话时 guest_id 为 null（不返回 404）。"""

    guest_id: uuid.UUID | None = None
    expires_at: datetime | None = None
    # 有未合并的访客对话时前端提示"登录后可合并"
    conversation_count: int = 0


class MergeGuestResult(BaseModel):
    """POST /auth/merge-guest 的响应。

    status: merged（本次完成合并）/ already_merged（重复提交，幂等成功）
    / no_session（没有可合并的访客会话）
    """

    merged_conversations: int
    status: str


class ConversationCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(default="", max_length=200)


class ConversationUpdateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str | None = Field(default=None, max_length=200)
    status: str | None = Field(default=None, pattern="^(ai|pending_handoff|human|closed)$")
    # 会话级语音回复开关（M5）：跟着会话走，避免每轮重复询问
    voice_reply_enabled: bool | None = None


class ConversationInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    channel: str
    owner_type: str
    status: str
    voice_reply_enabled: bool = False
    created_at: datetime
    updated_at: datetime


class ConversationListResponse(BaseModel):
    items: list[ConversationInfo]
    next_cursor: str | None = None


class MessageAttachment(BaseModel):
    """消息附件（M5 语音 / M8 图片）。

    object_key 不外泄：库里存的是存储路径，下发给前端的一律是短时签名 URL。
    """

    type: str
    mime_type: str = ""
    duration_seconds: float | None = None
    size_bytes: int | None = None
    # 图片的短时访问地址（签名令牌在 query 里，见 core/security.create_asset_token）
    url: str | None = None


class MessageInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: str
    content: str
    message_type: str
    source: str
    tokens: int | None
    latency_ms: int | None
    created_at: datetime
    # 该条回答引用的知识片段（M3 起）；非 assistant 消息为空列表
    sources: list[SourceRef] = Field(default_factory=list)
    attachments: list[MessageAttachment] = Field(default_factory=list)
    # 短时有效的音频播放地址（M5）；无语音时为 null
    audio_url: str | None = None


class MessageListResponse(BaseModel):
    """按时间正序返回；next_cursor 用于向前翻更早的消息。"""

    items: list[MessageInfo]
    next_cursor: str | None = None


class SendMessageRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    content: str = Field(min_length=1, max_length=8000)
