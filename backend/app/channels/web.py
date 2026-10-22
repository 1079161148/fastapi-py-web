"""站内 Web 渠道（设计文档 9.1）。

Web 是首发渠道，入站走 `POST /conversations/{id}/messages/stream`（SSE），
并不经过 webhook；本适配器存在的原因有两个：

1. 让 `ChannelRouter` 出站时能按 `conversation.channel == "web"` 路由而不必特判；
2. `parse_inbound` 提供"统一内部消息"的参照实现，webhook 渠道的解析逻辑
   可以对照它做一致性测试。

出站是 no-op：站内回复由 SSE 实时推送，无需二次投递。
"""

import json
from collections.abc import Mapping

from app.channels.base import ChannelAck, ChannelPayloadError, InboundMessage, OutboundMessage
from app.db.models.channel import CHANNEL_WEB


class WebAdapter:
    channel = CHANNEL_WEB

    def verify(self, *, query: Mapping[str, str], headers: Mapping[str, str], body: bytes) -> None:
        # 站内渠道靠登录令牌 / 访客 cookie 鉴权，没有平台级签名可验
        return None

    def parse_inbound(self, body: bytes, *, headers: Mapping[str, str]) -> InboundMessage:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ChannelPayloadError(self.channel, "JSON 解析失败") from exc
        if not isinstance(payload, dict):
            raise ChannelPayloadError(self.channel, "请求体必须是 JSON 对象")

        content = str(payload.get("content") or "").strip()
        user_id = str(payload.get("user_id") or "").strip()
        if not content or not user_id:
            raise ChannelPayloadError(self.channel, "缺少 content 或 user_id")

        return InboundMessage(
            channel=self.channel,
            external_msg_id=str(payload.get("msg_id") or ""),
            external_user_id=user_id,
            content=content,
            display_name=str(payload.get("display_name") or ""),
            reply_context={},
        )

    async def send_outbound(self, message: OutboundMessage) -> str | None:
        # SSE 已经在生成过程中把内容推给前端，这里不再投递
        return None

    def ack(self, inbound: InboundMessage | None = None) -> ChannelAck:
        return ChannelAck(body=json.dumps({"status": "accepted"}))


__all__ = ["WebAdapter"]
