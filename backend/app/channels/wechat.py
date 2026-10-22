"""微信公众号渠道（设计文档 9.1 / 8）。

入站：公众平台服务器回调（明文模式）
出站：客服消息 API（`/cgi-bin/message/custom/send`）

协议要点与坑：

1. **验签**：`sha1(sorted([token, timestamp, nonce]) 拼接)`，token 是公众平台
   "服务器配置"里自己填的值。平台还会用 GET 发一次 echostr 做域名归属验证。
2. **5 秒限制**：平台要求 5s 内响应，否则重推三次。因此 webhook 只做验签 +
   落库 + 入队，AI 生成在 worker 里完成，同步响应固定为 "success"。
3. **账号标识**：报文里的 `ToUserName` 是公众号原始 ID（gh_xxx），
   用它反查租户配置（见 `account_id_from_body`）。
4. **不支持安全模式**：加密报文需要 encoding_aes_key 解密，本期未实现。
   遇到 `<Encrypt>` 报文显式报错而不是当作空消息——静默丢弃等于丢客户消息。
5. **48 小时窗口**：客服消息只能在用户最后一次交互后 48h 内主动发送，
   超窗平台返回 45015，这里转成可读错误，由调用方决定是否转邮件/短信。

access_token 是进程级缓存（2h 有效，提前 5 分钟刷新）：它是"应用凭证"，
不是请求状态，放模块级缓存不违反适配器的无状态约定。
"""

import asyncio
import hashlib
import hmac
import time
from collections.abc import Mapping
from typing import Any
from xml.etree import ElementTree

import httpx

from app.channels.base import (
    ChannelAck,
    ChannelPayloadError,
    ChannelSendError,
    ChannelSignatureError,
    InboundMessage,
    OutboundMessage,
)
from app.channels.http_json import request_json

CHANNEL = "wechat"
API_BASE = "https://api.weixin.qq.com"
# 平台单条文本消息上限约 2048 字节，超出会被截断，这里按字符保守裁剪
CONTENT_MAX_LENGTH = 600
BODY_MAX_BYTES = 1024 * 1024
TOKEN_EXPIRY_MARGIN_SECONDS = 300
# 超窗/无权限等不可重试的错误码，转成可读提示
_ERROR_HINTS = {
    45015: "用户已超过 48 小时未互动，客服消息窗口已关闭",
    40003: "openid 无效或不属于该公众号",
    48001: "该公众号未获得客服消息接口权限",
}

_token_cache: dict[str, tuple[str, float]] = {}
_token_lock = asyncio.Lock()


def account_id_from_body(body: bytes) -> str:
    """从报文里取公众号原始 ID（不验签，仅用于反查租户配置）。

    这个函数会在验签之前执行，因此只允许做纯字符串提取，
    绝不能据此产生任何副作用。
    """
    if len(body) > BODY_MAX_BYTES:
        raise ChannelPayloadError(CHANNEL, "报文超过 1MB")
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise ChannelPayloadError(CHANNEL, "XML 解析失败") from exc
    return (root.findtext("ToUserName") or "").strip()


class WechatAdapter:
    channel = CHANNEL

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        token: str,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._token = token
        self._client = http_client
        self._timeout = timeout_seconds

    # ---------- 入站 ----------

    def verify(self, *, query: Mapping[str, str], headers: Mapping[str, str], body: bytes) -> None:
        signature = query.get("signature", "")
        timestamp = query.get("timestamp", "")
        nonce = query.get("nonce", "")
        if not (signature and timestamp and nonce):
            raise ChannelSignatureError(self.channel)
        expected = hashlib.sha1(
            "".join(sorted([self._token, timestamp, nonce])).encode("utf-8")
        ).hexdigest()
        # 摘要比较用 compare_digest，避免时序侧信道
        if not hmac.compare_digest(expected, signature):
            raise ChannelSignatureError(self.channel)

    def echo(self, *, query: Mapping[str, str]) -> str:
        """GET 校验：平台用 echostr 验证回调地址归属，原样回显即可。"""
        return query.get("echostr", "")

    def parse_inbound(self, body: bytes, *, headers: Mapping[str, str]) -> InboundMessage:
        if len(body) > BODY_MAX_BYTES:
            raise ChannelPayloadError(self.channel, "报文超过 1MB")
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError as exc:
            raise ChannelPayloadError(self.channel, "XML 解析失败") from exc

        if root.find("Encrypt") is not None:
            raise ChannelPayloadError(self.channel, "当前仅支持明文模式，请在公众平台关闭消息加密")

        msg_type = (root.findtext("MsgType") or "").strip()
        from_user = (root.findtext("FromUserName") or "").strip()
        if not from_user:
            raise ChannelPayloadError(self.channel, "缺少 FromUserName")

        # 事件推送（关注/取关/菜单点击）本期不进对话链路，交给上层标记 skipped
        content = (root.findtext("Content") or "").strip() if msg_type == "text" else ""
        message_id = (root.findtext("MsgId") or "").strip()
        if not message_id:
            # 事件没有 MsgId；用 FromUserName + CreateTime 组合保证幂等键稳定
            created = (root.findtext("CreateTime") or "").strip()
            message_id = f"wechat-{from_user}-{created}-{msg_type}"

        return InboundMessage(
            channel=self.channel,
            external_msg_id=message_id,
            external_user_id=from_user,
            content=content,
            message_type=msg_type or "unknown",
            account_id=(root.findtext("ToUserName") or "").strip(),
            reply_context={"to_user": (root.findtext("ToUserName") or "").strip()},
        )

    # ---------- 出站 ----------

    async def send_outbound(self, message: OutboundMessage) -> str | None:
        token = await self._access_token()
        payload = {
            "touser": message.external_user_id,
            "msgtype": "text",
            "text": {"content": message.content[:CONTENT_MAX_LENGTH]},
        }
        data = await self._request(
            "POST",
            f"{API_BASE}/cgi-bin/message/custom/send",
            params={"access_token": token},
            json=payload,
        )
        errcode = int(data.get("errcode", 0) or 0)
        if errcode != 0:
            hint = _ERROR_HINTS.get(errcode, str(data.get("errmsg") or "未知错误"))
            raise ChannelSendError(self.channel, f"发送失败（{errcode}）：{hint}")
        msgid = data.get("msgid")
        return str(msgid) if msgid is not None else None

    async def _access_token(self) -> str:
        cached = _token_cache.get(self._app_id)
        if cached and cached[1] > time.monotonic():
            return cached[0]

        async with _token_lock:
            cached = _token_cache.get(self._app_id)
            if cached and cached[1] > time.monotonic():
                return cached[0]
            data = await self._request(
                "GET",
                f"{API_BASE}/cgi-bin/token",
                params={
                    "grant_type": "client_credential",
                    "appid": self._app_id,
                    "secret": self._app_secret,
                },
            )
            token = str(data.get("access_token") or "")
            if not token:
                raise ChannelSendError(
                    self.channel,
                    f"获取 access_token 失败（{data.get('errcode')}）：{data.get('errmsg')}",
                )
            expires_in = int(data.get("expires_in", 7200) or 7200)
            _token_cache[self._app_id] = (
                token,
                time.monotonic() + max(expires_in - TOKEN_EXPIRY_MARGIN_SECONDS, 60),
            )
            return token

    async def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        return await request_json(
            client=self._client,
            channel=self.channel,
            method=method,
            url=url,
            timeout_seconds=self._timeout,
            **kwargs,
        )

    def ack(self, inbound: InboundMessage | None = None) -> ChannelAck:
        # 平台只认 "success"（或不含 XML 的任意短文本），返回 JSON 会被当作被动回复
        return ChannelAck(body="success", media_type="text/plain")


__all__ = ["CHANNEL", "WechatAdapter", "account_id_from_body"]
