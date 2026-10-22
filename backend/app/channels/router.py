"""出站路由（设计文档 9.1）：按 `conversation.channel` 把回复送回原渠道。

为什么单独一层：
- 对话编排只产出文本，不该知道"发去哪、用什么协议"
- 入站自动回复、坐席手动回复、失败重试共用同一出口，
  渠道限频与错误处理只实现一次

适配器不缓存实例：构造成本可忽略，而缓存会把凭证焊死在进程里，
运维改了环境变量必须重启才能生效——这在渠道排障时很致命。
"""

import uuid
from collections.abc import Mapping

import httpx

from app.channels.base import ChannelAdapter, OutboundMessage
from app.channels.factory import build_adapter


class ChannelRouter:
    """出站投递入口。http_client 仅测试注入，生产为 None（临时建连）。"""

    def __init__(self, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._http_client = http_client

    def adapter_for(self, channel: str, *, webhook_secret: str = "") -> ChannelAdapter:
        return build_adapter(
            channel, webhook_secret=webhook_secret, http_client=self._http_client
        )

    async def deliver(
        self,
        *,
        channel: str,
        conversation_id: uuid.UUID,
        external_user_id: str,
        content: str,
        reply_context: Mapping[str, str] | None = None,
    ) -> str | None:
        adapter = self.adapter_for(channel)
        return await adapter.send_outbound(
            OutboundMessage(
                channel=channel,
                conversation_id=conversation_id,
                external_user_id=external_user_id,
                content=content,
                reply_context=dict(reply_context or {}),
            )
        )


__all__ = ["ChannelRouter"]
