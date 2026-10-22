"""人工接管工单（设计文档 5.3 / 6）。

一个工单 = "这段对话需要真人"。状态机刻意做得很小：

    pending --(坐席认领)--> claimed --(结束)--> resolved
       |                       |
       +----(用户取消)---------+--> cancelled

为什么不复用 conversations.status 表达进度：
- 会话状态回答"现在谁在回"（ai / pending_handoff / human / closed）
- 工单回答"这件事谁在处理、处理完了没"（坐席归属、原因、时间点）

两者生命周期不同（一次会话可以转人工多次，每次一张工单），合并到一个字段
会让"第二次转人工"历史丢失。

唯一性：同一会话同时只允许一张未结工单（`status in (pending, claimed)`），
由部分唯一索引保证——幂等由数据库兜底，不依赖应用层先查后插。
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

# handoff_tickets.status
TICKET_STATUS_PENDING = "pending"
TICKET_STATUS_CLAIMED = "claimed"
TICKET_STATUS_RESOLVED = "resolved"
TICKET_STATUS_CANCELLED = "cancelled"

OPEN_TICKET_STATUSES = (TICKET_STATUS_PENDING, TICKET_STATUS_CLAIMED)

# handoff_tickets.reason：为什么转人工（统计与质检的维度）
REASON_USER_REQUEST = "user_request"
REASON_KEYWORD = "keyword"
REASON_AI_MARKER = "ai_marker"
REASON_AGENT = "agent"
REASON_SYSTEM = "system"

# handoff_tickets.requested_by：谁发起的
REQUESTED_BY_USER = "user"
REQUESTED_BY_GUEST = "guest"
REQUESTED_BY_AI = "ai"
REQUESTED_BY_AGENT = "agent"
REQUESTED_BY_SYSTEM = "system"


class HandoffTicket(Base, TimestampMixin):
    __tablename__ = "handoff_tickets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 认领后写入；坐席离职删账号时置空，工单仍保留（审计需要）
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=TICKET_STATUS_PENDING)
    reason: Mapped[str] = mapped_column(String(20), nullable=False, default=REASON_USER_REQUEST)
    requested_by: Mapped[str] = mapped_column(String(20), nullable=False, default=REQUESTED_BY_USER)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 访客/用户侧的一句话摘要（如"我要退货"），坐席不用点开会话就知道大概
    summary: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    # 扩展位：触发关键词、AI 标记、来源渠道等。
    # 列名故意不叫 metadata——那会与 SQLAlchemy 声明式基类的 Base.metadata 撞名
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "ix_handoff_tickets_tenant_id_status_created_at",
            "tenant_id",
            "status",
            "created_at",
        ),
        Index("ix_handoff_tickets_tenant_id_conversation_id", "tenant_id", "conversation_id"),
        Index("ix_handoff_tickets_tenant_id_agent_id", "tenant_id", "agent_id"),
        # 同一会话只能有一张未结工单：重复点"转人工"由数据库兜底，不靠应用层先查后插
        Index(
            "uq_handoff_tickets_open_per_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'claimed')"),
        ),
    )
