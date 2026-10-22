"""访客会话与对话数据模型（设计文档 5.5 / 6）。

三张表的关系与约束：

    guest_sessions 1 ── N conversations 1 ── N messages

- guest_sessions：匿名访客标识，TTL 30 天；合并或过期后作废，不删除
  （保留 merged_user_id 便于审计"这段访客对话最终归谁"）
- conversations：owner 二选一（user / guest），由 owner_type + 约束保证
  两者互斥；合并时整体改为 user 所有，guest_id 置空
- messages：追加式，不改不删；tenant_id 冗余存储，使 RLS 与索引都能
  直接以 tenant_id 打头，避免多一次 join 才能过滤

三张表均启用 RLS（见迁移 0002），tenant_id 由 session 层 SET LOCAL 注入。
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

# guest_sessions.status
GUEST_STATUS_ACTIVE = "active"
GUEST_STATUS_MERGED = "merged"
GUEST_STATUS_EXPIRED = "expired"

# conversations.owner_type / status
OWNER_TYPE_USER = "user"
OWNER_TYPE_GUEST = "guest"

CONVERSATION_STATUS_AI = "ai"
CONVERSATION_STATUS_PENDING_HANDOFF = "pending_handoff"
CONVERSATION_STATUS_HUMAN = "human"
CONVERSATION_STATUS_CLOSED = "closed"


class GuestSession(Base, TimestampMixin):
    __tablename__ = "guest_sessions"

    guest_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=GUEST_STATUS_ACTIVE)
    merged_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 与 cookie Max-Age 对齐（设计文档 5.5）
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_guest_sessions_tenant_id_expires_at", "tenant_id", "expires_at"),)


class Conversation(Base, TimestampMixin):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    # web / email / wechat / dingtalk（M4 起使用）
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default="web")
    owner_type: Mapped[str] = mapped_column(String(10), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    guest_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("guest_sessions.guest_id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=CONVERSATION_STATUS_AI
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    # 语音回复开关（M5）：会话级配置，避免每轮重复询问
    voice_reply_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        CheckConstraint(
            "(owner_type = 'user' AND user_id IS NOT NULL AND guest_id IS NULL)"
            " OR (owner_type = 'guest' AND guest_id IS NOT NULL AND user_id IS NULL)",
            name="owner_consistency",
        ),
        # 列表查询（按租户 + 更新时间倒序）与 owner 过滤都以 tenant_id 打头
        Index("ix_conversations_tenant_id_updated_at", "tenant_id", "updated_at"),
        Index("ix_conversations_tenant_id_user_id", "tenant_id", "user_id"),
        Index("ix_conversations_tenant_id_guest_id", "tenant_id", "guest_id"),
    )


class Message(Base, TimestampMixin):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # text / audio / video（语音消息见设计文档 9.3）
    message_type: Mapped[str] = mapped_column(String(20), nullable=False, default="text")
    # user / assistant / system
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 消息来源渠道：web / email / wechat / dingtalk / voice
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="web")
    attachments: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    # LLM 用量与耗时（成本核算，设计文档 10）
    tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # RAG 引用来源（M3 填充）
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    # 满意度评分 1-5（会话结束评价，M8 报表用）；未评价为 NULL
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        Index(
            "ix_messages_tenant_id_conversation_id_created_at",
            "tenant_id",
            "conversation_id",
            "created_at",
        ),
    )
