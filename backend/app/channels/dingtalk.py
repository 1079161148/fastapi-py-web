"""钉钉渠道（设计文档 9.1 / 8）。

入站：机器人回调（HTTP POST + header 签名）
出站：优先 sessionWebhook（入站报文自带，1.5 小时内有效）；
     失效后回退到 `robot/oToMessages/batchSend`（需要 access_token）

协议要点与坑：

1. **签名**：`base64(hmac_sha256(key=app_secret, data=f"{timestamp}\\n{app_secret}"))`，
   放在 header `sign` / `timestamp`。同时校验时间戳偏差（默认 1 小时），防重放。
2. **5 秒限制**：与微信一致，webhook 只落库 + 入队，同步返回 `{"errcode":0}`。
3. **账号标识**：机器人回调报文含 `robotCode`，用它反查租户配置。
4. **两条出站路径**：sessionWebhook 无需 token、可回复群聊；但它 1.5 小时过期，
   坐席隔天回复必须走 batchSend。两条都实现，避免"只能秒回"的假可用。
5. 主动推送受企业后台权限与频次限制，失败原因原样抛给调用方，不静默降级。

access_token 与微信一样是进程级缓存（7200s，提前 5 分钟刷新）。
"""

import asyncio
import base64
import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from typing import Any

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

CHANNEL = "dingtalk"
API_BASE = "https://api.dingtalk.com"
CONTENT_MAX_LENGTH = 1800
BODY_MAX_BYTES = 1024 * 1024
# 时间戳允许偏差：钉钉回调没有 nonce，靠时间窗防重放
TIMESTAMP_TOLERANCE_SECONDS = 3600
TOKEN_EXPIRY_MARGIN_SECONDS = 300

_token_cache: dict[str, tuple[str, float]] = {}
_token_lock = asyncio.Lock()


def sign(timestamp: str, app_secret: str) -> str:
    """钉钉回调签名：stringToSign = timestamp + "\\n" + appSecret。"""
    string_to_sign = f"{timestamp}\n{app_secret}".encode()
    digest = hmac.new(app_secret.encode(), string_to_sign, digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def account_id_from_body(body: bytes) -> str:
    """取 robotCode（不验签，仅用于反查租户配置）。"""
    if len(body) > BODY_MAX_BYTES:
        raise ChannelPayloadError(CHANNEL, "报文超过 1MB")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ChannelPayloadError(CHANNEL, "JSON 解析失败") from exc
    if not isinstance(payload, dict):
        raise ChannelPayloadError(CHANNEL, "请求体必须是 JSON 对象")
    return str(payload.get("robotCode") or payload.get("chatbotCorpId") or "").strip()


class DingTalkAdapter:
    channel = CHANNEL

    def __init__(
        self,
        *,
        app_key: str,
        app_secret: str,
        robot_code: str,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._app_key = app_key
        self._app_secret = app_secret
        self._robot_code = robot_code or app_key
        self._client = http_client
        self._timeout = timeout_seconds

    # ---------- 入站 ----------

    def verify(self, *, query: Mapping[str, str], headers: Mapping[str, str], body: bytes) -> None:
        timestamp = headers.get("timestamp", "")
        provided = headers.get("sign", "")
        if not timestamp or not provided:
            raise ChannelSignatureError(self.channel)
        try:
            drift = abs(time.time() - int(timestamp) / 1000)
        except ValueError as exc:
            raise ChannelSignatureError(self.channel) from exc
        if drift > TIMESTAMP_TOLERANCE_SECONDS:
            # 时间戳超出窗口：既可能是重放，也可能是服务器时钟漂移
            raise ChannelSignatureError(self.channel)
        if not hmac.compare_digest(sign(timestamp, self._app_secret), provided):
            raise ChannelSignatureError(self.channel)

    def parse_inbound(self, body: bytes, *, headers: Mapping[str, str]) -> InboundMessage:
        if len(body) > BODY_MAX_BYTES:
            raise ChannelPayloadError(self.channel, "报文超过 1MB")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ChannelPayloadError(self.channel, "JSON 解析失败") from exc
        if not isinstance(payload, dict):
            raise ChannelPayloadError(self.channel, "请求体必须是 JSON 对象")

        msg_type = str(payload.get("msgtype") or "unknown")
        msg_id = str(payload.get("msgId") or "").strip()
        # 企业内部员工用 staffId，外部联系人用 senderId，两者取到哪个用哪个
        user_id = str(payload.get("senderStaffId") or payload.get("senderId") or "").strip()
        if not user_id:
            raise ChannelPayloadError(self.channel, "缺少 senderStaffId / senderId")
        if not msg_id:
            # 没有 msgId 时用会话 + 时间戳兜底，保证幂等键稳定可复现
            msg_id = f"dingtalk-{payload.get('conversationId', '')}-{payload.get('createAt', '')}"

        text = payload.get("text") if isinstance(payload.get("text"), dict) else {}
        content = str((text or {}).get("content") or "").strip() if msg_type == "text" else ""

        return InboundMessage(
            channel=self.channel,
            external_msg_id=msg_id,
            external_user_id=user_id,
            content=content,
            message_type=msg_type,
            display_name=str(payload.get("senderNick") or ""),
            account_id=str(payload.get("robotCode") or payload.get("chatbotCorpId") or "").strip(),
            reply_context={
                "session_webhook": str(payload.get("sessionWebhook") or ""),
                "session_webhook_expires_at": str(payload.get("sessionWebhookExpiredTime") or ""),
                "conversation_id": str(payload.get("conversationId") or ""),
            },
        )

    # ---------- 出站 ----------

    async def send_outbound(self, message: OutboundMessage) -> str | None:
        session_webhook = message.reply_context.get("session_webhook", "")
        if session_webhook and not _session_webhook_expired(message.reply_context):
            await self._send_via_session_webhook(session_webhook, message)
            return None
        return await self._send_via_robot(message)

    async def _send_via_session_webhook(
        self, session_webhook: str, message: OutboundMessage
    ) -> None:
        data = await self._request(
            "POST",
            session_webhook,
            json={"msgtype": "text", "text": {"content": message.content[:CONTENT_MAX_LENGTH]}},
        )
        errcode = int(data.get("errcode", 0) or 0)
        if errcode != 0:
            raise ChannelSendError(
                self.channel, f"sessionWebhook 发送失败（{errcode}）：{data.get('errmsg')}"
            )

    async def _send_via_robot(self, message: OutboundMessage) -> str | None:
        token = await self._access_token()
        # msgParam 必须是 JSON 字符串（钉钉 API 的既定协议，不是笔误）
        data = await self._request(
            "POST",
            f"{API_BASE}/v1.0/robot/oToMessages/batchSend",
            headers={"x-acs-dingtalk-access-token": token},
            json={
                "robotCode": self._robot_code,
                "userIds": [message.external_user_id],
                "msgKey": "sampleText",
                "msgParam": json.dumps(
                    {"content": message.content[:CONTENT_MAX_LENGTH]}, ensure_ascii=False
                ),
            },
        )
        process_query_key = data.get("processQueryKey")
        return str(process_query_key) if process_query_key else None

    async def _access_token(self) -> str:
        cached = _token_cache.get(self._app_key)
        if cached and cached[1] > time.monotonic():
            return cached[0]

        async with _token_lock:
            cached = _token_cache.get(self._app_key)
            if cached and cached[1] > time.monotonic():
                return cached[0]
            data = await self._request(
                "POST",
                f"{API_BASE}/v1.0/oauth2/accessToken",
                json={"appKey": self._app_key, "appSecret": self._app_secret},
            )
            token = str(data.get("accessToken") or "")
            if not token:
                raise ChannelSendError(
                    self.channel, f"获取 access_token 失败：{data.get('message') or data}"
                )
            expire_in = int(data.get("expireIn", 7200) or 7200)
            _token_cache[self._app_key] = (
                token,
                time.monotonic() + max(expire_in - TOKEN_EXPIRY_MARGIN_SECONDS, 60),
            )
            return token

    async def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        # sessionWebhook 是完整 URL（自带签名串），因此这里传绝对地址
        return await request_json(
            client=self._client,
            channel=self.channel,
            method=method,
            url=url,
            timeout_seconds=self._timeout,
            **kwargs,
        )

    def ack(self, inbound: InboundMessage | None = None) -> ChannelAck:
        return ChannelAck(body='{"errcode":0,"errmsg":"ok"}')


def _session_webhook_expired(reply_context: Mapping[str, str]) -> bool:
    raw = reply_context.get("session_webhook_expires_at", "")
    if not raw.isdigit():
        return False
    return int(raw) / 1000 <= time.time()


__all__ = ["CHANNEL", "DingTalkAdapter", "account_id_from_body", "sign"]
