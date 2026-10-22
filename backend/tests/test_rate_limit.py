"""M8 限流测试（真实 Redis）。

覆盖：
- 对话限流按"用户"计数，超限返回 429 与 retry_after（不是静默降速）
- 认证接口限流按 IP 计数，超限响应带 Retry-After 头
- 限流开关关闭时完全不计数（测试环境默认关闭，这里验证开关本身有效）
- Redis 不可用时 fail-open（限流故障不能变成全站 503）
"""

from chat_helpers import (
    PASSWORD,
    FakeLLM,
    auth_headers,
    ensure_guest_session,
    register_user,
    tenant_headers,
)
from httpx import AsyncClient

from app.core import rate_limit
from app.core.config import get_settings


async def _create_conversation(
    client: AsyncClient, *, headers: dict[str, str], tenant_id: str
) -> str:
    await ensure_guest_session(client, tenant_id)
    created = await client.post("/api/v1/conversations", json={}, headers=headers)
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def test_chat_rate_limited_per_user(
    client: AsyncClient, unique_email: str, fake_llm: FakeLLM, monkeypatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_chat", 1)

    data = await register_user(client, unique_email)
    token = data["tokens"]["access_token"]
    tenant_id = data["user"]["tenant_id"]
    headers = {**auth_headers(token), **tenant_headers(tenant_id)}

    conversation_id = await _create_conversation(
        client, headers=headers, tenant_id=tenant_id
    )
    first = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages/stream",
        json={"content": "第一问"},
        headers=headers,
    )
    assert first.status_code == 200

    second = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages/stream",
        json={"content": "第二问"},
        headers=headers,
    )
    assert second.status_code == 429, second.text
    body = second.json()
    assert body["code"] == "RATE_LIMITED"
    assert body["detail"]["retry_after"] >= 1


async def test_auth_rate_limited_per_ip(
    client: AsyncClient, unique_email: str, monkeypatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_auth", 1)
    # 固定一个本用例专属的 IP，避免与其他用例的计数互相干扰
    fake_ip = f"ip-{unique_email}"
    monkeypatch.setattr(rate_limit, "client_ip", lambda request: fake_ip)

    first = await client.post(
        "/api/v1/auth/login", json={"email": unique_email, "password": PASSWORD}
    )
    # 账号不存在也是 401，但计数已经发生
    assert first.status_code == 401

    second = await client.post(
        "/api/v1/auth/login", json={"email": unique_email, "password": PASSWORD}
    )
    assert second.status_code == 429
    assert "Retry-After" in second.headers
    assert second.json()["code"] == "RATE_LIMITED"


async def test_rate_limit_disabled_counts_nothing(
    client: AsyncClient, unique_email: str, fake_llm: FakeLLM
) -> None:
    """测试环境默认关闭限流：连续两轮对话都应成功。"""
    data = await register_user(client, unique_email)
    token = data["tokens"]["access_token"]
    tenant_id = data["user"]["tenant_id"]
    headers = {**auth_headers(token), **tenant_headers(tenant_id)}

    conversation_id = await _create_conversation(
        client, headers=headers, tenant_id=tenant_id
    )
    for content in ("第一问", "第二问"):
        resp = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages/stream",
            json={"content": content},
            headers=headers,
        )
        assert resp.status_code == 200


async def test_rate_limit_fails_open_when_redis_unavailable(
    client: AsyncClient, unique_email: str, monkeypatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_auth", 1)

    async def _broken_redis():
        raise ConnectionError("redis down")

    monkeypatch.setattr(rate_limit, "get_redis", _broken_redis)

    for _ in range(2):
        resp = await client.post(
            "/api/v1/auth/login", json={"email": unique_email, "password": PASSWORD}
        )
        # fail-open：Redis 不可用时不应拒绝请求，业务错误码保持原样
        assert resp.status_code == 401
