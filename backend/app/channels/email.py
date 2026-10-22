"""邮件渠道（设计文档 9.1 / 3.1）。

入站：IMAP 轮询（worker 定时任务）或共享密钥保护的 webhook（本地联调/演练）
出站：SMTP 发信

三个关键约束：

1. **零新增依赖**：`smtplib` / `imaplib` / `email` 都是标准库。设计文档 3.1 已明确
   "收发同步 IO 在线程池执行"，因此不需要 aiosmtplib——少一个依赖，行为与标准库一致。
2. **去重键是 Message-ID**：平台重发邮件、轮询窗口重叠都会产生重复入站；
   Message-ID 缺失时退化为正文 SHA256（宁可不合并，也不能重复回复）。
3. **会话关联**：回复主题带 `[#xxxxxxxx]`（会话 UUID 前 8 位），入站按它定位原会话；
   没有前缀时由服务层按发件人映射（同一个人默认沿用最近会话）。
"""

import asyncio
import contextlib
import hashlib
import hmac
import imaplib
import re
import smtplib
from collections.abc import Mapping
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.utils import formatdate, getaddresses, make_msgid, parseaddr
from html import unescape

from app.channels.base import (
    ChannelAck,
    ChannelPayloadError,
    ChannelSignatureError,
    InboundMessage,
    OutboundMessage,
)

CHANNEL = "email"

# 回复主题里的会话短号：[#3f9a1c2b]
CONVERSATION_REF_PATTERN = re.compile(r"\[#([0-9a-f]{8})\]", re.IGNORECASE)
SUBJECT_MAX_LENGTH = 200
BODY_MAX_LENGTH = 8000


def conversation_ref(subject: str) -> str | None:
    """从主题里取会话短号（供服务层定位会话）。"""
    match = CONVERSATION_REF_PATTERN.search(subject)
    return match.group(1).lower() if match else None


def _decode(raw: str | None) -> str:
    """解码 RFC 2047 编码的头部（中文主题/发件人名）。"""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw))).strip()
    except (UnicodeDecodeError, LookupError):
        return raw.strip()


def _find_part(message: Message, content_type: str) -> str | None:
    for part in message.walk():
        if part.get_content_type() != content_type:
            continue
        # 附件里的文本不算正文
        if part.get_content_disposition() == "attachment":
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            # 平台给的是无效 charset（如 x-unknown）：退回 utf-8，不让邮件处理失败
            return payload.decode("utf-8", errors="replace")
    return None


def _html_to_text(html: str) -> str:
    """极简 HTML 转文本：邮件正文只用于喂模型，不追求渲染还原。"""
    without_script = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", without_script)
    return unescape(re.sub(r"[ \t\r\f\v]+", " ", text))


def _recipient_address(message: Message) -> str:
    """取本封邮件的收件地址，用于反查租户。

    优先 Delivered-To（投递时写入，最接近真实收件箱），其次 To/Cc。
    """
    for header in ("Delivered-To", "To", "Cc"):
        raw = message.get(header)
        if not raw:
            continue
        for _, address in getaddresses([_decode(raw)]):
            if address:
                return address
    return ""


def extract_body(message: Message) -> str:
    """优先 text/plain；只有 HTML 时降级为去标签文本。"""
    if message.is_multipart():
        plain = _find_part(message, "text/plain")
        if plain and plain.strip():
            return plain
        html = _find_part(message, "text/html")
        return _html_to_text(html) if html else ""
    payload = message.get_payload(decode=True)
    if not isinstance(payload, bytes):
        return ""
    charset = message.get_content_charset() or "utf-8"
    try:
        raw = payload.decode(charset, errors="replace")
    except LookupError:
        raw = payload.decode("utf-8", errors="replace")
    return _html_to_text(raw) if message.get_content_type() == "text/html" else raw


class EmailAdapter:
    channel = CHANNEL

    def __init__(
        self,
        *,
        smtp_host: str,
        smtp_port: int,
        smtp_username: str,
        smtp_password: str,
        sender: str,
        imap_host: str,
        imap_port: int,
        imap_username: str,
        imap_password: str,
        webhook_secret: str = "",
        timeout_seconds: float = 20.0,
    ) -> None:
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._smtp_username = smtp_username
        self._smtp_password = smtp_password
        self._sender = sender or smtp_username
        self._imap_host = imap_host
        self._imap_port = imap_port
        self._imap_username = imap_username
        self._imap_password = imap_password
        self._webhook_secret = webhook_secret
        self._timeout = timeout_seconds

    # ---------- 入站 ----------

    def verify(self, *, query: Mapping[str, str], headers: Mapping[str, str], body: bytes) -> None:
        """邮件没有平台签名，用共享密钥头代替。

        未配置密钥时一律拒绝（fail-closed）：否则任何人都能伪造邮件入站，
        直接消耗模型额度。
        """
        if not self._webhook_secret:
            raise ChannelSignatureError(self.channel)
        provided = headers.get("x-channel-secret", "")
        if not _constant_time_equals(provided, self._webhook_secret):
            raise ChannelSignatureError(self.channel)

    def parse_inbound(self, body: bytes, *, headers: Mapping[str, str]) -> InboundMessage:
        message = message_from_bytes(body)
        sender_name, sender_email = parseaddr(_decode(message.get("From")))
        if not sender_email:
            raise ChannelPayloadError(self.channel, "缺少 From 发件人地址")

        subject = _decode(message.get("Subject"))
        content = extract_body(message).strip()
        if not content:
            raise ChannelPayloadError(self.channel, "邮件正文为空（仅支持纯文本或 HTML）")

        message_id = _decode(message.get("Message-ID")) or (
            f"email-{hashlib.sha256(body).hexdigest()[:32]}"
        )
        return InboundMessage(
            channel=self.channel,
            external_msg_id=message_id,
            external_user_id=sender_email.lower(),
            content=content[:BODY_MAX_LENGTH],
            display_name=sender_name,
            conversation_ref=conversation_ref(subject) or "",
            # 收件地址决定入站归属哪个租户（多租户共用一个 IMAP 信箱时尤其重要）
            account_id=_recipient_address(message).lower(),
            reply_context={
                "subject": subject[:SUBJECT_MAX_LENGTH],
                "message_id": message_id,
                "references": _decode(message.get("References")),
            },
        )

    async def fetch_unseen(self, limit: int) -> list[bytes]:
        """拉取未读邮件原始报文（不置已读，成功处理后再 mark_seen）。"""
        if not self._imap_host or not self._imap_username:
            return []
        return await asyncio.to_thread(self._fetch_unseen_sync, limit)

    async def mark_seen(self, raw_messages: list[bytes]) -> None:
        """按 Message-ID 重新定位并置已读。

        IMAP 的 UID 在连接间可能变化，用 Message-ID 搜索最稳；找不到就跳过——
        重复处理由幂等键兜底，不会重复回复。
        """
        if not raw_messages or not self._imap_host:
            return
        message_ids = [_decode(message_from_bytes(raw).get("Message-ID")) for raw in raw_messages]
        await asyncio.to_thread(self._mark_seen_sync, [mid for mid in message_ids if mid])

    def _fetch_unseen_sync(self, limit: int) -> list[bytes]:
        client = imaplib.IMAP4_SSL(self._imap_host, self._imap_port, timeout=self._timeout)
        try:
            client.login(self._imap_username, self._imap_password)
            client.select("INBOX")
            status, data = client.search(None, "UNSEEN")
            if status != "OK" or not data or not data[0]:
                return []
            uids = data[0].split()[-limit:]
            results: list[bytes] = []
            for uid in uids:
                # PEEK：拉取正文但不改已读状态
                status, payload = client.fetch(uid, "(BODY.PEEK[])")
                if status != "OK" or not payload:
                    continue
                for item in payload:
                    if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                        results.append(item[1])
            return results
        finally:
            # 连接可能已被服务端断开；退出失败不影响已拉取的报文
            with contextlib.suppress(imaplib.IMAP4.error, OSError):
                client.logout()

    def _mark_seen_sync(self, message_ids: list[str]) -> None:
        client = imaplib.IMAP4_SSL(self._imap_host, self._imap_port, timeout=self._timeout)
        try:
            client.login(self._imap_username, self._imap_password)
            client.select("INBOX")
            for message_id in message_ids:
                status, data = client.search(None, "HEADER", "Message-ID", message_id)
                if status != "OK" or not data or not data[0]:
                    continue
                for uid in data[0].split():
                    client.store(uid, "+FLAGS", "(\\Seen)")
        finally:
            with contextlib.suppress(imaplib.IMAP4.error, OSError):
                client.logout()

    # ---------- 出站 ----------

    async def send_outbound(self, message: OutboundMessage) -> str | None:
        email = self._build_reply(message)
        await asyncio.to_thread(self._send_sync, email)
        return str(email.get("Message-ID") or "") or None

    def _build_reply(self, message: OutboundMessage) -> EmailMessage:
        email = EmailMessage()
        email["From"] = self._sender
        email["To"] = message.external_user_id
        email["Subject"] = self._reply_subject(message)
        email["Date"] = formatdate(localtime=True)
        email["Message-ID"] = make_msgid(domain=_domain_of(self._sender))
        in_reply_to = message.reply_context.get("message_id", "")
        if in_reply_to:
            email["In-Reply-To"] = in_reply_to
            references = message.reply_context.get("references", "")
            email["References"] = f"{references} {in_reply_to}".strip()
        email.set_content(message.content)
        return email

    def _reply_subject(self, message: OutboundMessage) -> str:
        original = message.reply_context.get("subject", "")
        # 去掉旧的 Re: 前缀，避免 Re: Re: Re:
        base = re.sub(r"^(?:\s*re\s*:\s*)+", "", original, flags=re.IGNORECASE).strip()
        tag = f"[#{message.conversation_id.hex[:8]}]"
        subject = f"Re: {tag} {base}".strip()
        return subject[:SUBJECT_MAX_LENGTH]

    def _send_sync(self, email: EmailMessage) -> None:
        if self._smtp_port == 465:
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                self._smtp_host, self._smtp_port, timeout=self._timeout
            )
        else:
            client = smtplib.SMTP(self._smtp_host, self._smtp_port, timeout=self._timeout)
        try:
            if self._smtp_port != 465:
                client.starttls()
            if self._smtp_username:
                client.login(self._smtp_username, self._smtp_password)
            client.send_message(email)
        finally:
            try:
                client.quit()
            except (smtplib.SMTPException, OSError):
                # 已发送成功但服务端拒绝 quit 时不应把成功判为失败
                client.close()

    def ack(self, inbound: InboundMessage | None = None) -> ChannelAck:
        return ChannelAck(body='{"status":"accepted"}')


def _domain_of(address: str) -> str | None:
    _, email_address = parseaddr(address)
    return email_address.split("@")[-1] if "@" in email_address else None


def _constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


__all__ = ["CHANNEL", "EmailAdapter", "conversation_ref", "extract_body"]
