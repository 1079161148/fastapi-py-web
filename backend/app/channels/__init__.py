"""渠道适配层（设计文档 4.2 / 9.1）。

对外只暴露统一模型与工厂：`InboundMessage` / `OutboundMessage` / `build_adapter`
/ `ChannelRouter`。业务层不得直接 import 本目录下的具体适配器模块——
否则新增渠道就要改业务代码，"可插拔"名存实亡。
"""

from app.channels.base import (
    ChannelAck,
    ChannelAdapter,
    ChannelError,
    ChannelNotConfiguredError,
    ChannelPayloadError,
    ChannelSendError,
    ChannelSignatureError,
    InboundMessage,
    OutboundMessage,
)
from app.channels.factory import build_adapter, credentials_hint, extract_account_id
from app.channels.router import ChannelRouter

__all__ = [
    "ChannelAck",
    "ChannelAdapter",
    "ChannelError",
    "ChannelNotConfiguredError",
    "ChannelPayloadError",
    "ChannelRouter",
    "ChannelSendError",
    "ChannelSignatureError",
    "InboundMessage",
    "OutboundMessage",
    "build_adapter",
    "credentials_hint",
    "extract_account_id",
]
