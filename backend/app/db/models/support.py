"""运营支撑模型：快捷回复、审计日志、质检与敏感词命中（M8 / M9）。

所有表都启用 RLS：tenant_id 由 session 层 SET LOCAL 注入，
未设置上下文时查询返回空行（fail-closed）。
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

# 快捷回复分组：固定几种，避免运营随意建分组后前端筛选失控
QUICK_REPLY_CATEGORIES = ("greeting", "faq", "closing", "other")

# 质检状态
REVIEW_STATUS_PENDING = "pending"
REVIEW_STATUS_DONE = "done"
# 质检问题标签：固定词表，"其他"兜底但不鼓励用
QUALITY_TAGS = (
    "unresolved",  # 未解答
    "off_topic",  # 答非所问
    "tone",  # 语气/态度问题
    "slow",  # 响应太慢
    "policy",  # 话术违规/承诺越界
    "sensitive",  # 敏感信息处置不当
    "other",
)
# 敏感词命中后的处置
SENSITIVE_ACTION_WARN = "warn"
SENSITIVE_ACTION_HANDOFF = "handoff"
SENSITIVE_ACTION_BLOCK = "block"


class QuickReply(Base, TimestampMixin):
    """坐席常用话术：回复时一键插入，按分组筛选。"""

    __tablename__ = "quick_replies"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(20), nullable=False, default="other")
    # 排序权重：同分组内越小越靠前
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class AuditLog(Base, TimestampMixin):
    """租户内敏感操作留痕：成员变更、配置修改、快捷回复增删等。

    只记录"谁改了什么"，不记录内容明细（密码、令牌不入库）；
    detail 仅放可读的摘要（如角色从 agent 改为 admin）。
    """

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    # 操作人；系统任务（cron）为 NULL
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    # 如 member.add / member.role / tenant.settings / quick_reply.delete
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    # 被操作对象类型与 ID（可空，如租户自身设置没有具体对象）
    target_type: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    target_id: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class QualityReview(Base, TimestampMixin):
    """质检抽检（M9）：一条会话一条，抽样时建 pending，评分后转 done。

    为什么评分与消息分开存：抽检是"事后复核"，对象是整段会话而不是单条消息，
    同一会话重复抽检没有意义（conversation_id 上有唯一索引）。
    """

    __tablename__ = "quality_reviews"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    # 抽样发起人（自动抽样为 None）；评分人单独记，便于追溯"谁评的"
    sampled_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=REVIEW_STATUS_PENDING)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sampled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "ix_quality_reviews_tenant_id_status_created_at",
            "tenant_id",
            "status",
            "created_at",
        ),
        Index("uq_quality_reviews_conversation", "conversation_id", unique=True),
    )


class SensitiveHit(Base, TimestampMixin):
    """敏感词命中留痕（M9）。

    独立成表而不是给 messages 加字段：block 策略下消息根本没落库，
    挂在消息上的字段会漏掉最该看的那部分；命中是运营事件，与消息生命周期不同。
    """

    __tablename__ = "sensitive_hits"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    # block 时消息未落库：可空
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    words: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    action: Mapped[str] = mapped_column(String(20), nullable=False, default=SENSITIVE_ACTION_WARN)
    # 命中片段（截断）：排查时不用翻聊天记录也能看出为什么命中
    snippet: Mapped[str] = mapped_column(String(500), nullable=False, default="")

    __table_args__ = (
        Index("ix_sensitive_hits_tenant_id_created_at", "tenant_id", "created_at"),
        Index("ix_sensitive_hits_tenant_id_action", "tenant_id", "action"),
    )
