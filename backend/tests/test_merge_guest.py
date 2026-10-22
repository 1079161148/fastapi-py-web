"""访客合并（双凭证）集成测试（设计文档 5.5 / 8 / 9.2）。

核心断言：
- 只带 token 或只带 guest cookie 都不能完成合并
- 合并后对话所有权转给用户，历史消息一条不少
- 重复提交幂等（already_merged），跨租户合并被 RLS 挡住
"""

import uuid

from chat_helpers import (
    PASSWORD,
    FakeLLM,
    auth_headers,
    ensure_guest_session,
    register_user,
    tenant_headers,
)
from httpx import AsyncClient


async def _guest_conversation_with_reply(
    client: AsyncClient, tenant_id: str, content: str = "帮我查下订单"
) -> tuple[str, str]:
    """造一段访客对话，返回 (guest_id, conversation_id)。"""
    guest_id = await ensure_guest_session(client, tenant_id)
    headers = tenant_headers(tenant_id)
    conversation_id = (
        await client.post("/api/v1/conversations", json={}, headers=headers)
    ).json()["id"]
    resp = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages/stream",
        json={"content": content},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return guest_id, conversation_id


async def test_merge_without_credentials_is_rejected_or_noop(
    client: AsyncClient, unique_email: str
) -> None:
    data = await register_user(client, unique_email)
    token = data["tokens"]["access_token"]
    tenant_id = data["user"]["tenant_id"]

    # 只有 token、没有访客 cookie：幂等成功但不合并任何东西
    no_cookie = await client.post("/api/v1/auth/merge-guest", headers=auth_headers(token))
    assert no_cookie.status_code == 200
    assert no_cookie.json() == {"merged_conversations": 0, "status": "no_session"}

    # 只有访客 cookie、没有 access token：必须 401（防止伪造 guest_id 劫持）
    await ensure_guest_session(client, tenant_id)
    no_token = await client.post("/api/v1/auth/merge-guest")
    assert no_token.status_code == 401
    assert no_token.json()["code"] == "UNAUTHENTICATED"


async def test_merge_transfers_ownership_and_is_idempotent(
    client: AsyncClient, unique_email: str, fake_llm: FakeLLM
) -> None:
    data = await register_user(client, unique_email)
    tenant_id = data["user"]["tenant_id"]
    guest_id, conversation_id = await _guest_conversation_with_reply(client, tenant_id)

    login = await client.post(
        "/api/v1/auth/login", json={"email": unique_email, "password": PASSWORD}
    )
    assert login.status_code == 200
    token = login.json()["tokens"]["access_token"]
    headers = auth_headers(token)

    # 合并前：访客对话不属于该用户
    before = await client.get("/api/v1/conversations", headers=headers)
    assert before.json()["items"] == []

    merged = await client.post("/api/v1/auth/merge-guest", headers=headers)
    assert merged.status_code == 200, merged.text
    assert merged.json() == {"merged_conversations": 1, "status": "merged"}

    after = await client.get("/api/v1/conversations", headers=headers)
    items = after.json()["items"]
    assert [item["id"] for item in items] == [conversation_id]
    assert items[0]["owner_type"] == "user"

    # 历史消息（含 AI 回答）完整保留
    messages = await client.get(
        f"/api/v1/conversations/{conversation_id}/messages", headers=headers
    )
    assert [item["role"] for item in messages.json()["items"]] == ["user", "assistant"]
    assert messages.json()["items"][1]["content"] == fake_llm.full_text

    # 幂等：带着已作废的 guest cookie 重复提交，返回成功而非报错
    replay = await client.post(
        "/api/v1/auth/merge-guest",
        headers={**headers, "Cookie": f"guest_id={guest_id}"},
    )
    assert replay.status_code == 200
    assert replay.json() == {"merged_conversations": 0, "status": "already_merged"}

    # 原 guest cookie 已清除，访客身份不再可用
    current = await client.get(
        "/api/v1/guest/sessions/current", headers=tenant_headers(tenant_id)
    )
    assert current.json()["guest_id"] is None


async def test_merge_does_not_cross_tenants(
    client: AsyncClient, unique_email: str, fake_llm: FakeLLM
) -> None:
    tenant_a = await register_user(client, unique_email, "租户 A")
    tenant_a_id = tenant_a["user"]["tenant_id"]

    _, conversation_id = await _guest_conversation_with_reply(client, tenant_a_id)

    # B 租户的账号尝试合并 A 租户的访客会话
    email_b = f"tenant-b-{uuid.uuid4().hex[:8]}@example.com"
    tenant_b = await register_user(client, email_b, "租户 B")
    token_b = tenant_b["tokens"]["access_token"]

    resp = await client.post("/api/v1/auth/merge-guest", headers=auth_headers(token_b))
    assert resp.status_code == 200
    assert resp.json()["status"] == "no_session"

    # B 租户看不到该会话
    b_list = await client.get("/api/v1/conversations", headers=auth_headers(token_b))
    assert b_list.json()["items"] == []

    # A 租户访客的会话仍然可用（没被误删 cookie，也没被转移）
    still = await client.get(
        f"/api/v1/conversations/{conversation_id}", headers=tenant_headers(tenant_a_id)
    )
    assert still.status_code == 200
    assert still.json()["owner_type"] == "guest"
