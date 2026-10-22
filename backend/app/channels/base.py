"""渠道适配层统一契约（设计文档 4.2 / 9.1）。

铁律：渠道差异只允许出现在 `app/channels/` 内。services 与对话编排
只见 `InboundMessage` / `OutboundMessage`，不 import 任何平台 SDK、不判断渠道名。

适配器生命周期：
    webhook 到达 → verify（验签）→ parse_inbound（报文 → 内部消息）
    → 对话链路 → send_outbound（内部消息 → 渠道协议）→ ack（平台要求的响应体）
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.exceptions import AppError


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """渠道原始报文归一化后的内部消息（与渠道无关）。"""

    channel: str
    # 平台消息 ID（微信 MsgId / 钉钉 msgId / 邮件 Message-ID）；幂等去重的键
    external_msg_id: str
    # 发送者在平台内的标识（openid / userid / 邮箱）
    external_user_id: str
    content: str
    message_type: str = "text"
    display_name: str = ""
    attachments: tuple[dict[str, Any], ...] = ()
    # 渠道自带的会话引用（邮件主题里的 [#xxxxxxxx]）。放在通用模型里，
    # 是为了让服务层定位会话时不必判断渠道名——渠道差异留在适配器内
    conversation_ref: str = ""
    # 报文指向的平台账号（微信 ToUserName / 钉钉 robotCode / 邮件收件地址）：
    # 多租户下用它把入站消息路由到正确租户
    account_id: str = ""
    # 回复所需的渠道上下文（邮件 Subject/Message-ID、微信 FromUserName 等）。
    # 不放进入库内容：它是协议细节，只在出站时用
    reply_context: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """要发往渠道的一条回复。"""

    channel: str
    conversation_id: uuid.UUID
    external_user_id: str
    content: str
    reply_context: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ChannelAck:
    """平台要求的同步响应体。

    微信要求 5s 内返回 "success"（否则重推三次），钉钉要求 JSON 且 errcode=0，
    邮件由我们自己的轮询触发，返回 JSON 便于联调。
    """

    body: str
    media_type: str = "application/json"


class ChannelError(AppError):
    """渠道相关错误的基类，便于上层统一按渠道语义处理。"""


class ChannelSignatureError(ChannelError):
    """验签失败：直接拒绝并记安全日志（设计文档 8）。"""

    def __init__(self, channel: str) -> None:
        super().__init__(
            code="CHANNEL_SIGNATURE_INVALID",
            message=f"{channel} 回调验签失败：请检查渠道凭证与验签参数是否与平台一致",
            status_code=403,
            expose=True,
        )


class ChannelNotConfiguredError(ChannelError):
    """该渠道未配置凭证：明确 503，不静默丢弃消息。"""

    def __init__(self, channel: str, missing: list[str]) -> None:
        super().__init__(
            code="CHANNEL_NOT_CONFIGURED",
            message=f"{channel} 渠道未配置：请设置 " + " / ".join(missing) + " 后重启服务",
            status_code=503,
            expose=True,
        )
        self.missing = missing


class ChannelPayloadError(ChannelError):
    """报文无法解析（平台改协议、手工构造的错误请求）。"""

    def __init__(self, channel: str, reason: str) -> None:
        super().__init__(
            code="CHANNEL_PAYLOAD_INVALID",
            message=f"{channel} 回调报文无法解析：{reason}",
            status_code=400,
            expose=True,
        )


class ChannelSendError(ChannelError):
    """出站投递失败（平台限频、窗口关闭、凭证失效等）。

    expose=True：这些原因对运维可操作（改配置/换渠道），
    被统一 5xx 兜底成"服务内部错误"反而无从排查。
    """

    def __init__(self, channel: str, reason: str) -> None:
        super().__init__(
            code="CHANNEL_SEND_FAILED",
            message=f"{channel} 渠道发送失败：{reason}",
            status_code=502,
            expose=True,
        )


class ChannelAdapter(Protocol):
    """渠道适配器。实现必须是无状态的：配置与凭证在构造时注入。"""

    channel: str

    def verify(self, *, query: Mapping[str, str], headers: Mapping[str, str], body: bytes) -> None:
        """验签：不通过抛 ChannelSignatureError，通过返回 None。"""
        ...

    def parse_inbound(self, body: bytes, *, headers: Mapping[str, str]) -> InboundMessage:
        """原始报文 → 内部消息。纯解析，不做 IO。"""
        ...

    async def send_outbound(self, message: OutboundMessage) -> str | None:
        """发送回复，返回平台消息 ID（拿不到返回 None）。"""
        ...

    def ack(self, inbound: InboundMessage | None = None) -> ChannelAck:
        """平台要求的同步响应体。"""
        ...


__all__ = [
    "ChannelAck",
    "ChannelAdapter",
    "ChannelError",
    "ChannelNotConfiguredError",
    "ChannelPayloadError",
    "ChannelSendError",
    "ChannelSignatureError",
    "InboundMessage",
    "OutboundMessage",
]
