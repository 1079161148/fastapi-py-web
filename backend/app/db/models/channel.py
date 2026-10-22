"""渠道接入数据模型（设计文档 9.1 / 6）。

三张表的分工：

    channel_configs 1 ── N channel_messages
    channel_identities N ── 1 guest_sessions

- channel_configs：每租户每渠道的开关、外部账号标识、非敏感配置；
  唯一 (tenant_id, channel)，另有 (channel, external_account_id) 部分唯一索引
  供 webhook 反查租户（见迁移 0004 的 channel_tenant 函数）
- channel_messages：入/出站流水。(tenant_id, channel, external_msg_id) 唯一，
  平台重推回调时靠它去重——不去重必然重复回复
- channel_identities：外部用户（微信 openid / 钉钉 userid / 邮箱地址）到
  guest_sessions 的映射。渠道用户没有登录账号，复用 visitor 身份作为会话归属，
  从而不用给 conversations 增加第三种 owner（会破坏既有 owner 一致性约束）

凭证策略（设计文档 3.1 / 9.1）：AppSecret、邮箱密码等敏感凭证只走环境变量，
credentials 仅存非敏感项（AppID、邮箱地址、企业 ID）；加密入库留待 M8 加固。
"""

import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

# 渠道标识：与 conversations.channel / messages.source 取值一致
CHANNEL_WEB = "web"
CHANNEL_EMAIL = "email"
CHANNEL_WECHAT = "wechat"
CHANNEL_DINGTALK = "dingtalk"
ALL_CHANNELS = (CHANNEL_WEB, CHANNEL_EMAIL, CHANNEL_WECHAT, CHANNEL_DINGTALK)

# channel_messages.direction
DIRECTION_INBOUND = "inbound"
DIRECTION_OUTBOUND = "outbound"

# channel_messages.status
MSG_STATUS_RECEIVED = "received"
MSG_STATUS_PROCESSING = "processing"
MSG_STATUS_REPLIED = "replied"
MSG_STATUS_FAILED = "failed"
# 非文本消息（图片/语音/事件回调等）本期不处理，显式标记跳过而不是静默丢弃
MSG_STATUS_SKIPPED = "skipped"


class ChannelConfig(Base, TimestampMixin):
    __tablename__ = "channel_configs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    # 微信：公众号原始 ID（gh_xxx）；钉钉：robotCode 或 corpId；邮件：收件邮箱
    external_account_id: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    credentials: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    rate_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    # 邮件等"非平台验签"渠道的共享密钥，走请求头校验；平台渠道留空
    webhook_secret: Mapped[str] = mapped_column(String(200), nullable=False, default="")

    __table_args__ = (
        UniqueConstraint("tenant_id", "channel", name="uq_channel_configs_tenant_id_channel"),
    )


class ChannelMessage(Base, TimestampMixin):
    __tablename__ = "channel_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    external_msg_id: Mapped[str] = mapped_column(String(255), nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=MSG_STATUS_RECEIVED)
    external_user_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 出站所需渠道上下文（钉钉 sessionWebhook、邮件主题/Message-ID）。
    # 必须落库：入站处理后由另一个进程（worker）投递，内存传参丢不起
    reply_context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    error_msg: Mapped[str] = mapped_column(String(500), nullable=False, default="")

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "channel",
            "external_msg_id",
            name="uq_channel_messages_tenant_id_channel_external_msg_id",
        ),
        Index("ix_channel_messages_tenant_id_conversation_id", "tenant_id", "conversation_id"),
        Index("ix_channel_messages_tenant_id_status", "tenant_id", "status"),
    )


class ChannelIdentity(Base, TimestampMixin):
    __tablename__ = "channel_identities"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    external_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    guest_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("guest_sessions.guest_id", ondelete="SET NULL"),
        nullable=True,
    )
    display_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "channel",
            "external_user_id",
            name="uq_channel_identities_tenant_id_channel_external_user_id",
        ),
    )


__all__ = [
    "ALL_CHANNELS",
    "CHANNEL_DINGTALK",
    "CHANNEL_EMAIL",
    "CHANNEL_WEB",
    "CHANNEL_WECHAT",
    "DIRECTION_INBOUND",
    "DIRECTION_OUTBOUND",
    "MSG_STATUS_FAILED",
    "MSG_STATUS_PROCESSING",
    "MSG_STATUS_RECEIVED",
    "MSG_STATUS_REPLIED",
    "MSG_STATUS_SKIPPED",
    "ChannelConfig",
    "ChannelIdentity",
    "ChannelMessage",
]
