"""离线留言（M8）。

为什么单独一张表而不是复用 messages：
留言的核心是"**联系方式 + 待办状态**"，它要在坐席下班后依然可查、可标记处理；
塞进对话消息里就会被"已读/未读"与上下文长度搅在一起。

conversation_id 可空：留言允许发生在会话被关闭之后（坐席不在线、用户已离开），
但正常路径都是带着会话提交的——那样坐席能点回去看上下文。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

# leave_messages.status
LEAVE_STATUS_NEW = "new"
LEAVE_STATUS_HANDLED = "handled"


class LeaveMessage(Base, TimestampMixin):
    __tablename__ = "leave_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    # 会话删除后留言保留：联系人信息本身有价值，不该随对话一起消失
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )
    # user / guest：区分登录用户与匿名访客，方便坐席决定怎么回
    owner_type: Mapped[str] = mapped_column(String(10), nullable=False, default="guest")
    name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    contact: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=LEAVE_STATUS_NEW)
    handled_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # 坐席列表：按租户 + 状态 + 时间倒序
        Index("ix_leave_messages_tenant_id_status_created_at", "tenant_id", "status", "created_at"),
        Index("ix_leave_messages_tenant_id_conversation_id", "tenant_id", "conversation_id"),
    )
