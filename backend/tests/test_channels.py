"""多渠道接入集成测试（真实数据库 + 假 LLM / 假队列）。

覆盖设计文档 M4 验收点：
- 渠道配置 CRUD 与权限（坐席不能改渠道配置）
- 微信回调：验签、幂等去重（平台重推不重复回复）、未知账号 404
- 钉钉回调：HMAC 验签与时间窗校验
- 邮件：MIME 解析（中文主题/收件地址）、共享密钥验签、主题前缀关联会话
- worker 全链路：生成回复 → 投递 → 入/出站流水状态
- 渠道消息复用同一条对话链路（assistant.source = 渠道名）

平台账号标识在每个用例里随机生成：`(channel, external_account_id)` 全局唯一
（一个公众号/机器人只能绑定一个租户，否则入站无法路由），复用固定值会撞约束。
"""

import hashlib
import time
import uuid
from collections.abc import Mapping
from email.message import EmailMessage
from typing import Any

import pytest
from chat_helpers import PASSWORD, auth_headers, register_user
from httpx import AsyncClient
from knowledge_helpers import FakeEmbedder, FakeQueue
from sqlalchemy import select

from app.channels import ChannelRouter
from app.channels.base import ChannelSendError, OutboundMessage
from app.channels.dingtalk import sign as dingtalk_sign
from app.channels.email import EmailAdapter
from app.core.config import get_settings
from app.core.security import hash_password
from app.core.tenant import set_current_tenant
from app.db.models.channel import (
    CHANNEL_EMAIL,
    CHANNEL_WECHAT,
    MSG_STATUS_REPLIED,
    ChannelIdentity,
    ChannelMessage,
)
from app.db.models.conversation import Message
from app.db.models.tenant import TenantMember
from app.db.models.user import User
from app.db.session import async_session_factory
from app.services import channels as channel_service
from app.workers.channel_tasks import process_channel_inbound


@pytest.fixture
def wechat_account() -> str:
    return f"gh_{uuid.uuid4().hex[:10]}"


@pytest.fixture
def dingtalk_account() -> str:
    return f"robot-{uuid.uuid4().hex[:10]}"


@pytest.fixture
def email_account() -> str:
    return f"support-{uuid.uuid4().hex[:6]}@example.com"


def _wechat_xml(*, account: str, msg_id: str, content: str = "退款多久到账") -> bytes:
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{account}]]></ToUserName>"
        "<FromUserName><![CDATA[openid-user-1]]></FromUserName>"
        "<CreateTime>1700000000</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{content}]]></Content>"
        f"<MsgId>{msg_id}</MsgId>"
        "</xml>"
    ).encode()


def _wechat_query(*, token: str = "wechat-test-token") -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = "nonce-123"
    signature = hashlib.sha1("".join(sorted([token, timestamp, nonce])).encode("utf-8")).hexdigest()
    return {"signature": signature, "timestamp": timestamp, "nonce": nonce}


def _mime_email(*, subject: str, account: str, body: str = "退款多久到账？") -> bytes:
    message = EmailMessage()
    message["From"] = "张三 <user@example.com>"
    message["To"] = account
    message["Subject"] = subject
    message["Message-ID"] = f"<{uuid.uuid4().hex}@example.com>"
    message.set_content(body)
    return message.as_bytes()


def _email_adapter(account: str) -> EmailAdapter:
    return EmailAdapter(
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_username=account,
        smtp_password="secret",
        sender=account,
        imap_host="",
        imap_port=993,
        imap_username="",
        imap_password="",
    )


def _configure_wechat(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "wechat_app_id", "wx_m4_app")
    monkeypatch.setattr(settings, "wechat_app_secret", "wx_m4_secret")
    monkeypatch.setattr(settings, "wechat_token", "wechat-test-token")


def _configure_dingtalk(monkeypatch: pytest.MonkeyPatch, account: str) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "dingtalk_app_key", "ding_m4_key")
    monkeypatch.setattr(settings, "dingtalk_app_secret", "ding_m4_secret")
    monkeypatch.setattr(settings, "dingtalk_robot_code", account)


def _configure_email(monkeypatch: pytest.MonkeyPatch, account: str) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_username", account)
    monkeypatch.setattr(settings, "smtp_password", "smtp-secret")
    monkeypatch.setattr(settings, "smtp_from", account)


class RecordingRouter(ChannelRouter):
    """记录出站投递的假路由：worker 测试里避免真实调用平台 API。"""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[OutboundMessage] = []

    async def deliver(
        self,
        *,
        channel: str,
        conversation_id: uuid.UUID,
        external_user_id: str,
        content: str,
        reply_context: Mapping[str, str] | None = None,
    ) -> str | None:
        self.sent.append(
            OutboundMessage(
                channel=channel,
                conversation_id=conversation_id,
                external_user_id=external_user_id,
                content=content,
                reply_context=dict(reply_context or {}),
            )
        )
        return "platform-msg-1"


async def _setup_tenant(client: AsyncClient, email: str) -> tuple[dict[str, str], str]:
    data = await register_user(client, email)
    return auth_headers(data["tokens"]["access_token"]), data["user"]["tenant_id"]


async def _put_config(
    client: AsyncClient, headers: dict[str, str], channel: str, **payload: Any
) -> dict[str, Any]:
    resp = await client.put(f"/api/v1/channels/{channel}", json=payload, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _load_message(
    tenant_id: str, channel: str, external_msg_id: str
) -> ChannelMessage | None:
    set_current_tenant(uuid.UUID(tenant_id))
    try:
        async with async_session_factory() as session:
            return await channel_service.load_message(
                session,
                tenant_id=uuid.UUID(tenant_id),
                channel=channel,
                external_msg_id=external_msg_id,
            )
    finally:
        set_current_tenant(None)


async def test_channel_config_management_and_permissions(
    client: AsyncClient, unique_email: str, wechat_account: str
) -> None:
    headers, tenant_id = await _setup_tenant(client, unique_email)

    listed = await client.get("/api/v1/channels", headers=headers)
    assert listed.status_code == 200, listed.text
    channels = {item["channel"]: item for item in listed.json()}
    assert set(channels) == {"web", "email", "wechat", "dingtalk"}
    # 未落库的渠道也要返回占位行，前端才能一次看到全部渠道状态
    assert channels["wechat"]["enabled"] is False
    assert channels["wechat"]["webhook_path"] == "/api/v1/channels/wechat/webhook"

    saved = await _put_config(
        client,
        headers,
        "wechat",
        enabled=True,
        external_account_id=wechat_account,
        rate_limit=30,
    )
    assert saved["external_account_id"] == wechat_account
    assert saved["enabled"] is True
    assert saved["rate_limit"] == 30
    # 环境变量缺凭证时明确标注不可用（配了也不生效）
    assert saved["configured"] is False
    assert "WECHAT_APP_ID" in saved["missing"]

    # 坐席角色不可改渠道配置（涉及外部账号与凭证）
    agent_email = f"agent-{uuid.uuid4().hex[:8]}@example.com"
    async with async_session_factory() as session:
        set_current_tenant(uuid.UUID(tenant_id))
        agent = User(
            id=uuid.uuid4(),
            email=agent_email,
            password_hash=hash_password(PASSWORD),
            display_name="坐席",
            role="customer",
            tenant_id=uuid.UUID(tenant_id),
        )
        session.add(agent)
        await session.flush()
        session.add(TenantMember(tenant_id=uuid.UUID(tenant_id), user_id=agent.id, role="agent"))
        await session.commit()
    set_current_tenant(None)

    login = await client.post(
        "/api/v1/auth/login", json={"email": agent_email, "password": PASSWORD}
    )
    agent_headers = auth_headers(login.json()["tokens"]["access_token"])
    forbidden = await client.put(
        "/api/v1/channels/wechat", json={"enabled": False}, headers=agent_headers
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "FORBIDDEN"


async def test_wechat_webhook_is_idempotent(
    client: AsyncClient,
    unique_email: str,
    wechat_account: str,
    fake_queue: FakeQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """平台重推同一条消息只能产生一次回复（设计文档 9.1 的核心约束）。"""
    _configure_wechat(monkeypatch)
    headers, tenant_id = await _setup_tenant(client, unique_email)
    await _put_config(client, headers, "wechat", enabled=True, external_account_id=wechat_account)

    body = _wechat_xml(account=wechat_account, msg_id="10001")
    query = _wechat_query()
    first = await client.post("/api/v1/channels/wechat/webhook", params=query, content=body)
    assert first.status_code == 200, first.text
    assert first.text == "success", "微信只认 success，返回 JSON 会被当作被动回复"
    assert len(fake_queue.jobs) == 1
    assert fake_queue.jobs[0][0] == "process_channel_inbound"

    second = await client.post("/api/v1/channels/wechat/webhook", params=query, content=body)
    assert second.status_code == 200
    assert len(fake_queue.jobs) == 1, "重推不能再次入队"

    set_current_tenant(uuid.UUID(tenant_id))
    try:
        async with async_session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ChannelMessage).where(
                            ChannelMessage.tenant_id == uuid.UUID(tenant_id),
                            ChannelMessage.channel == CHANNEL_WECHAT,
                        )
                    )
                )
                .scalars()
                .all()
            )
    finally:
        set_current_tenant(None)

    assert len(rows) == 1
    assert rows[0].direction == "inbound"
    assert rows[0].conversation_id is not None


async def test_wechat_webhook_rejects_bad_signature(
    client: AsyncClient,
    unique_email: str,
    wechat_account: str,
    fake_queue: FakeQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_wechat(monkeypatch)
    headers, _ = await _setup_tenant(client, unique_email)
    await _put_config(client, headers, "wechat", enabled=True, external_account_id=wechat_account)

    query = _wechat_query()
    query["signature"] = "0" * 40
    resp = await client.post(
        "/api/v1/channels/wechat/webhook",
        params=query,
        content=_wechat_xml(account=wechat_account, msg_id="10002"),
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "CHANNEL_SIGNATURE_INVALID"
    assert not fake_queue.jobs, "验签失败绝不能入队"


async def test_webhook_unknown_account_is_404(
    client: AsyncClient,
    unique_email: str,
    wechat_account: str,
    fake_queue: FakeQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_wechat(monkeypatch)
    headers, _ = await _setup_tenant(client, unique_email)
    await _put_config(client, headers, "wechat", enabled=True, external_account_id=wechat_account)

    resp = await client.post(
        "/api/v1/channels/wechat/webhook",
        params=_wechat_query(),
        content=_wechat_xml(account=f"gh_unknown_{uuid.uuid4().hex[:6]}", msg_id="10003"),
    )
    assert resp.status_code == 404
    assert not fake_queue.jobs


async def test_wechat_get_verification_echoes_challenge(
    client: AsyncClient,
    unique_email: str,
    wechat_account: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """公众平台配置回调地址时用 GET + echostr 验证归属。"""
    _configure_wechat(monkeypatch)
    headers, _ = await _setup_tenant(client, unique_email)
    await _put_config(client, headers, "wechat", enabled=True, external_account_id=wechat_account)

    query = _wechat_query()
    query.update({"account": wechat_account, "echostr": "echo-ok"})
    resp = await client.get("/api/v1/channels/wechat/webhook", params=query)
    assert resp.status_code == 200, resp.text
    assert resp.text == "echo-ok"


async def test_dingtalk_webhook_verifies_signature(
    client: AsyncClient,
    unique_email: str,
    dingtalk_account: str,
    fake_queue: FakeQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_dingtalk(monkeypatch, dingtalk_account)
    headers, _ = await _setup_tenant(client, unique_email)
    await _put_config(
        client, headers, "dingtalk", enabled=True, external_account_id=dingtalk_account
    )

    timestamp = str(int(time.time() * 1000))
    payload = {
        "msgtype": "text",
        "text": {"content": "发票怎么开"},
        "msgId": "ding-1001",
        "senderStaffId": "staff-1",
        "senderNick": "李四",
        "robotCode": dingtalk_account,
        "sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession?session=abc",
    }
    good = await client.post(
        "/api/v1/channels/dingtalk/webhook",
        json=payload,
        headers={"timestamp": timestamp, "sign": dingtalk_sign(timestamp, "ding_m4_secret")},
    )
    assert good.status_code == 200, good.text
    assert good.json() == {"errcode": 0, "errmsg": "ok"}
    assert len(fake_queue.jobs) == 1

    bad = await client.post(
        "/api/v1/channels/dingtalk/webhook",
        json={**payload, "msgId": "ding-1002"},
        headers={"timestamp": timestamp, "sign": "invalid"},
    )
    assert bad.status_code == 403

    # 时间戳超出窗口 → 拒绝（钉钉回调没有 nonce，只能靠时间窗防重放）
    stale = str(int(time.time() * 1000) - 7200_000)
    expired = await client.post(
        "/api/v1/channels/dingtalk/webhook",
        json={**payload, "msgId": "ding-1003"},
        headers={"timestamp": stale, "sign": dingtalk_sign(stale, "ding_m4_secret")},
    )
    assert expired.status_code == 403


async def test_worker_generates_reply_and_delivers(
    client: AsyncClient,
    unique_email: str,
    wechat_account: str,
    fake_queue: FakeQueue,
    fake_llm,
    fake_embedder: FakeEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """worker 全链路：生成（复用对话编排）→ 投递 → 入/出站流水都置 replied。"""
    _configure_wechat(monkeypatch)
    headers, tenant_id = await _setup_tenant(client, unique_email)
    await _put_config(client, headers, "wechat", enabled=True, external_account_id=wechat_account)
    assert (
        await client.post(
            "/api/v1/channels/wechat/webhook",
            params=_wechat_query(),
            content=_wechat_xml(account=wechat_account, msg_id="20001"),
        )
    ).status_code == 200

    router = RecordingRouter()
    monkeypatch.setattr("app.workers.channel_tasks.ChannelRouter", lambda: router)
    monkeypatch.setattr("app.workers.channel_tasks.get_llm_client", lambda: fake_llm)
    monkeypatch.setattr("app.workers.channel_tasks.get_embedding_client", lambda: fake_embedder)
    monkeypatch.setattr("app.workers.channel_tasks.get_rerank_client", lambda: None)

    await process_channel_inbound({}, tenant_id, CHANNEL_WECHAT, "20001")

    assert len(router.sent) == 1
    assert router.sent[0].content == fake_llm.full_text
    assert router.sent[0].external_user_id == "openid-user-1"

    record = await _load_message(tenant_id, CHANNEL_WECHAT, "20001")
    assert record is not None
    assert record.status == MSG_STATUS_REPLIED
    assert record.conversation_id is not None

    set_current_tenant(uuid.UUID(tenant_id))
    try:
        async with async_session_factory() as session:
            messages = (
                (
                    await session.execute(
                        select(Message).where(Message.conversation_id == record.conversation_id)
                    )
                )
                .scalars()
                .all()
            )
            outbound = (
                await session.execute(
                    select(ChannelMessage).where(
                        ChannelMessage.external_msg_id == f"out-{record.id}"
                    )
                )
            ).scalar_one()
    finally:
        set_current_tenant(None)

    assert [item.role for item in messages] == ["user", "assistant"]
    assert all(item.source == CHANNEL_WECHAT for item in messages)
    assert outbound.status == MSG_STATUS_REPLIED
    assert outbound.content == fake_llm.full_text

    # 重复执行同一任务：已完成，不重复生成也不重复发送
    await process_channel_inbound({}, tenant_id, CHANNEL_WECHAT, "20001")
    assert len(router.sent) == 1


async def test_worker_retry_only_resends_after_generation_succeeded(
    client: AsyncClient,
    unique_email: str,
    wechat_account: str,
    fake_queue: FakeQueue,
    fake_llm,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """投递失败后重试：只重投已生成的回复，不重新调用模型（省额度）。"""
    _configure_wechat(monkeypatch)
    headers, tenant_id = await _setup_tenant(client, unique_email)
    await _put_config(client, headers, "wechat", enabled=True, external_account_id=wechat_account)
    await client.post(
        "/api/v1/channels/wechat/webhook",
        params=_wechat_query(),
        content=_wechat_xml(account=wechat_account, msg_id="30001"),
    )

    router = RecordingRouter()
    sent: list[OutboundMessage] = router.sent

    async def fail_deliver(
        *,
        channel: str,
        conversation_id: uuid.UUID,
        external_user_id: str,
        content: str,
        reply_context: Mapping[str, str] | None = None,
    ) -> str | None:
        sent.append(
            OutboundMessage(
                channel=channel,
                conversation_id=conversation_id,
                external_user_id=external_user_id,
                content=content,
            )
        )
        raise ChannelSendError(CHANNEL_WECHAT, "服务端 45015 窗口关闭")

    router.deliver = fail_deliver  # type: ignore[method-assign]
    monkeypatch.setattr("app.workers.channel_tasks.ChannelRouter", lambda: router)
    monkeypatch.setattr("app.workers.channel_tasks.get_llm_client", lambda: fake_llm)

    await process_channel_inbound({}, tenant_id, CHANNEL_WECHAT, "30001")
    record = await _load_message(tenant_id, CHANNEL_WECHAT, "30001")
    assert record is not None
    assert record.status == "failed"
    assert "45015" in record.error_msg

    # 第二次：投递成功，模型只被调用过一次（回复内容已缓存到出站流水）
    async def succeed(
        *,
        channel: str,
        conversation_id: uuid.UUID,
        external_user_id: str,
        content: str,
        reply_context: Mapping[str, str] | None = None,
    ) -> str | None:
        sent.append(
            OutboundMessage(
                channel=channel,
                conversation_id=conversation_id,
                external_user_id=external_user_id,
                content=content,
            )
        )
        return "platform-msg-2"

    router.deliver = succeed  # type: ignore[method-assign]
    await process_channel_inbound({}, tenant_id, CHANNEL_WECHAT, "30001")
    record = await _load_message(tenant_id, CHANNEL_WECHAT, "30001")
    assert record is not None and record.status == MSG_STATUS_REPLIED
    assert fake_llm.call_count == 1
    assert len(sent) == 2


def test_email_parse_extracts_sender_ref_and_account() -> None:
    adapter = _email_adapter("support@example.com")

    inbound = adapter.parse_inbound(
        _mime_email(subject="Re: [#3f9a1c2b] 退款咨询", account="support@example.com"),
        headers={},
    )
    assert inbound.conversation_ref == "3f9a1c2b"
    assert inbound.external_user_id == "user@example.com"
    assert inbound.display_name == "张三"
    assert inbound.account_id == "support@example.com"
    assert "退款" in inbound.content
    assert inbound.external_msg_id.startswith("<")
    assert inbound.reply_context["subject"].endswith("退款咨询")

    plain = adapter.parse_inbound(
        _mime_email(subject="发票问题", account="support@example.com"), headers={}
    )
    assert plain.conversation_ref == ""


async def test_email_webhook_requires_shared_secret(
    client: AsyncClient,
    unique_email: str,
    email_account: str,
    fake_queue: FakeQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_email(monkeypatch, email_account)
    headers, tenant_id = await _setup_tenant(client, unique_email)
    await _put_config(
        client,
        headers,
        "email",
        enabled=True,
        external_account_id=email_account,
        webhook_secret="mail-secret",
    )

    raw = _mime_email(subject="退款多久到账", account=email_account)
    no_secret = await client.post(
        f"/api/v1/channels/email/webhook?account={email_account}", content=raw
    )
    assert no_secret.status_code == 403

    accepted = await client.post(
        f"/api/v1/channels/email/webhook?account={email_account}",
        content=raw,
        headers={"X-Channel-Secret": "mail-secret"},
    )
    assert accepted.status_code == 200, accepted.text
    assert len(fake_queue.jobs) == 1

    message_id = _email_adapter(email_account).parse_inbound(raw, headers={}).external_msg_id
    record = await _load_message(tenant_id, CHANNEL_EMAIL, message_id)
    assert record is not None and record.conversation_id is not None

    set_current_tenant(uuid.UUID(tenant_id))
    try:
        async with async_session_factory() as session:
            identities = (
                (
                    await session.execute(
                        select(ChannelIdentity).where(
                            ChannelIdentity.tenant_id == uuid.UUID(tenant_id),
                            ChannelIdentity.channel == CHANNEL_EMAIL,
                        )
                    )
                )
                .scalars()
                .all()
            )
    finally:
        set_current_tenant(None)

    assert len(identities) == 1
    assert identities[0].guest_id is not None
    assert identities[0].external_user_id == "user@example.com"


async def test_channel_account_is_bound_to_single_tenant(
    client: AsyncClient, unique_email: str, wechat_account: str
) -> None:
    """同一公众号只能绑定一个租户：否则入站无法唯一确定租户。"""
    headers_a, _ = await _setup_tenant(client, unique_email)
    await _put_config(client, headers_a, "wechat", enabled=True, external_account_id=wechat_account)

    other = await register_user(client, f"ch-b-{uuid.uuid4().hex[:8]}@example.com", "M4 渠道租户 B")
    resp = await client.put(
        "/api/v1/channels/wechat",
        json={"enabled": True, "external_account_id": wechat_account},
        headers=auth_headers(other["tokens"]["access_token"]),
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "CONFLICT"
