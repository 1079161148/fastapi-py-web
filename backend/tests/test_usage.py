"""M7 用量与成本测试（真实数据库 + 假 LLM）。

覆盖设计文档 10 / M7 的验收点：
- 一次对话落一条 usage 流水，token 与消息上的 tokens 一致
- 看板按模型/按天聚合，金额在价格未配置时为 0（不编造价目）
- 当日 token 配额耗尽时拒绝本轮请求，且不留下半截用户消息
- 非管理员（agent）访问成本看板 403
"""

from chat_helpers import (
    FakeLLM,
    auth_headers,
    ensure_guest_session,
    parse_sse,
    register_user,
    tenant_headers,
)
from httpx import AsyncClient


async def _stream_once(
    client: AsyncClient, *, access_token: str, tenant_id: str, content: str = "想咨询退款"
) -> list[tuple[str, dict]]:
    headers = {**auth_headers(access_token), **tenant_headers(tenant_id)}
    await ensure_guest_session(client, tenant_id)
    created = await client.post("/api/v1/conversations", json={}, headers=headers)
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]

    resp = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages/stream",
        json={"content": content},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return parse_sse(resp.text)


async def test_dialogue_usage_is_recorded(
    client: AsyncClient, unique_email: str, fake_llm: FakeLLM
) -> None:
    data = await register_user(client, unique_email)
    token = data["tokens"]["access_token"]
    tenant_id = data["user"]["tenant_id"]

    await _stream_once(client, access_token=token, tenant_id=tenant_id)

    overview = await client.get("/api/v1/usage/overview", headers=auth_headers(token))
    assert overview.status_code == 200, overview.text
    body = overview.json()
    # FakeLLM 的 usage 是 10 + 5
    assert body["today"]["total_tokens"] == 15
    assert body["today"]["prompt_tokens"] == 10
    assert body["today"]["completion_tokens"] == 5
    assert body["today"]["requests"] == 1
    # 价格默认 0：不内置假价目
    assert body["today"]["cost"] == 0.0
    # 注册时写入的默认配额
    assert body["quota"]["tokens_limit"] == 500_000
    assert body["quota"]["exceeded"] is False


async def test_usage_summary_groups_by_model(
    client: AsyncClient, unique_email: str, fake_llm: FakeLLM
) -> None:
    data = await register_user(client, unique_email)
    token = data["tokens"]["access_token"]
    tenant_id = data["user"]["tenant_id"]
    await _stream_once(client, access_token=token, tenant_id=tenant_id)

    summary = await client.get(
        "/api/v1/usage/summary",
        params={"group_by": "model"},
        headers=auth_headers(token),
    )
    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body["group_by"] == "model"
    assert len(body["buckets"]) == 1
    assert body["buckets"][0]["total_tokens"] == 15
    assert body["total"]["total_tokens"] == 15

    by_day = await client.get(
        "/api/v1/usage/summary", params={"group_by": "day"}, headers=auth_headers(token)
    )
    assert by_day.status_code == 200
    assert len(by_day.json()["buckets"]) == 1


async def test_usage_summary_rejects_bad_params(
    client: AsyncClient, unique_email: str
) -> None:
    data = await register_user(client, unique_email)
    token = data["tokens"]["access_token"]

    bad_group = await client.get(
        "/api/v1/usage/summary", params={"group_by": "week"}, headers=auth_headers(token)
    )
    assert bad_group.status_code == 400
    assert bad_group.json()["code"] == "INVALID_GROUP_BY"

    inverted = await client.get(
        "/api/v1/usage/summary",
        params={"start": "2026-10-20T00:00:00Z", "end": "2026-10-01T00:00:00Z"},
        headers=auth_headers(token),
    )
    assert inverted.status_code == 400
    assert inverted.json()["code"] == "INVALID_TIME_RANGE"


async def test_usage_overview_requires_tenant_admin(
    client: AsyncClient, unique_email: str
) -> None:
    owner = await register_user(client, unique_email)
    owner_token = owner["tokens"]["access_token"]
    tenant_id = owner["user"]["tenant_id"]

    created = await client.post(
        "/api/v1/tenants/current/members",
        json={
            "email": f"agent-{unique_email}",
            "password": "agent-password-123",
            "display_name": "坐席",
            "role": "agent",
        },
        headers=auth_headers(owner_token),
    )
    assert created.status_code == 201, created.text

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": f"agent-{unique_email}", "password": "agent-password-123"},
    )
    assert login.status_code == 200, login.text
    agent_token = login.json()["tokens"]["access_token"]

    denied = await client.get("/api/v1/usage/overview", headers=auth_headers(agent_token))
    assert denied.status_code == 403
    assert denied.json()["code"] == "FORBIDDEN"

    allowed = await client.get(
        "/api/v1/usage/overview",
        headers={**auth_headers(owner_token), **tenant_headers(tenant_id)},
    )
    assert allowed.status_code == 200


async def test_token_quota_blocks_stream_and_keeps_history_clean(
    client: AsyncClient, unique_email: str, fake_llm: FakeLLM
) -> None:
    data = await register_user(client, unique_email)
    token = data["tokens"]["access_token"]
    tenant_id = data["user"]["tenant_id"]

    # 先产生 15 tokens 用量，再把日预算压到 10：下一次请求应被拒
    await _stream_once(client, access_token=token, tenant_id=tenant_id)

    patched = await client.patch(
        "/api/v1/tenants/current/quota",
        json={"max_tokens_per_day": 10},
        headers=auth_headers(token),
    )
    assert patched.status_code == 200, patched.text

    headers = {**auth_headers(token), **tenant_headers(tenant_id)}
    await ensure_guest_session(client, tenant_id)
    conversation_id = (
        await client.post("/api/v1/conversations", json={}, headers=headers)
    ).json()["id"]

    resp = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages/stream",
        json={"content": "在吗"},
        headers=headers,
    )
    assert resp.status_code == 200
    events = parse_sse(resp.text)
    assert events[-1][0] == "error"
    assert events[-1][1]["code"] == "TOKEN_QUOTA_EXCEEDED"
    assert events[-1][1]["retryable"] is False

    # 配额拒绝必须发生在用户消息落库之前：历史保持干净
    messages = await client.get(
        f"/api/v1/conversations/{conversation_id}/messages", headers=headers
    )
    assert messages.json()["items"] == []
