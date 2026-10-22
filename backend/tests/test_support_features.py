"""M8 运营支撑功能测试（真实数据库 + 假 LLM）。

覆盖"菜单补齐"新增的后端能力：
- 快捷回复 CRUD，且写操作落审计日志
- 满意度评价：只能给 assistant 打分，分数进入报表口径
- 数据报表 / 客户管理：字段齐全、非坐席不可越权
- 审计日志只读且仅 owner/admin 可见
- 租户配置（欢迎语）：PATCH 落库后随访客会话下发给未登录用户
"""

from chat_helpers import (
    PASSWORD,
    auth_headers,
    ensure_guest_session,
    parse_sse,
    register_user,
    tenant_headers,
)
from httpx import AsyncClient

STATS_KEYS = {
    "total_conversations",
    "ai_resolved",
    "human_handled",
    "pending_handoff",
    "handoff_rate",
    "avg_first_reply_ms",
    "satisfaction_avg",
    "satisfaction_count",
    "new_leave_messages",
    "trend",
}


async def _create_agent(client: AsyncClient, *, token: str, email: str) -> str:
    """在租户内建一个坐席账号并返回它的令牌。"""
    created = await client.post(
        "/api/v1/tenants/current/members",
        json={"email": email, "password": PASSWORD, "display_name": "坐席", "role": "agent"},
        headers=auth_headers(token),
    )
    assert created.status_code == 201, created.text
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return str(login.json()["tokens"]["access_token"])


async def test_quick_reply_crud_is_audited(client: AsyncClient, unique_email: str) -> None:
    owner = await register_user(client, unique_email)
    token = owner["tokens"]["access_token"]

    created = await client.post(
        "/api/v1/quick-replies",
        json={"title": "退款政策", "content": "7 天无理由退货", "category": "faq"},
        headers=auth_headers(token),
    )
    assert created.status_code == 201, created.text
    reply_id = created.json()["id"]
    assert created.json()["category"] == "faq"

    listed = await client.get("/api/v1/quick-replies", headers=auth_headers(token))
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [reply_id]

    patched = await client.patch(
        f"/api/v1/quick-replies/{reply_id}",
        json={"content": "15 天无理由退货"},
        headers=auth_headers(token),
    )
    assert patched.status_code == 200, patched.text
    # 局部更新：未提交的字段保持原值
    assert patched.json()["content"] == "15 天无理由退货"
    assert patched.json()["title"] == "退款政策"

    deleted = await client.delete(f"/api/v1/quick-replies/{reply_id}", headers=auth_headers(token))
    assert deleted.status_code == 204, deleted.text
    assert (await client.get("/api/v1/quick-replies", headers=auth_headers(token))).json() == []

    logs = await client.get("/api/v1/audit-logs", headers=auth_headers(token))
    assert logs.status_code == 200
    actions = [item["action"] for item in logs.json()]
    assert actions.count("quick_reply.create") == 1
    assert actions.count("quick_reply.update") == 1
    assert actions.count("quick_reply.delete") == 1
    create_log = next(item for item in logs.json() if item["action"] == "quick_reply.create")
    assert create_log["target_id"] == reply_id


async def test_agent_cannot_read_audit_logs(client: AsyncClient, unique_email: str) -> None:
    owner = await register_user(client, unique_email)
    agent_token = await _create_agent(
        client, token=owner["tokens"]["access_token"], email=f"agent-{unique_email}"
    )

    resp = await client.get("/api/v1/audit-logs", headers=auth_headers(agent_token))
    assert resp.status_code == 403


async def test_stats_overview_shape(client: AsyncClient, unique_email: str) -> None:
    owner = await register_user(client, unique_email)
    token = owner["tokens"]["access_token"]

    resp = await client.get("/api/v1/stats/overview", headers=auth_headers(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) >= STATS_KEYS
    assert body["trend"] == [] or set(body["trend"][0]) >= {
        "date",
        "conversations",
        "messages",
        "ratings",
    }


async def test_rate_assistant_message_and_feeds_stats(
    client: AsyncClient, unique_email: str, fake_llm
) -> None:
    owner = await register_user(client, unique_email)
    token = owner["tokens"]["access_token"]
    tenant_id = owner["user"]["tenant_id"]
    headers = {**auth_headers(token), **tenant_headers(tenant_id)}
    await ensure_guest_session(client, tenant_id)

    conversation = await client.post("/api/v1/conversations", json={}, headers=headers)
    assert conversation.status_code == 201, conversation.text
    conversation_id = conversation.json()["id"]

    streamed = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages/stream",
        json={"content": "想咨询退款"},
        headers=headers,
    )
    assert streamed.status_code == 200, streamed.text
    events = parse_sse(streamed.text)
    assistant_id = events[-1][1]["id"]
    user_id = events[0][1]["id"]

    rated = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages/{assistant_id}/rating",
        json={"score": 5},
        headers=headers,
    )
    assert rated.status_code == 200, rated.text
    assert rated.json()["score"] == 5

    # 用户消息没有"满意度"这一说
    rejected = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages/{user_id}/rating",
        json={"score": 5},
        headers=headers,
    )
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "RATING_NOT_ALLOWED"

    # 评分与报表同源：打完分立刻能在报表里看到
    stats = await client.get("/api/v1/stats/overview", headers=auth_headers(token))
    assert stats.status_code == 200
    assert stats.json()["satisfaction_count"] >= 1
    assert stats.json()["satisfaction_avg"] == 5.0


async def test_customers_list_requires_agent(client: AsyncClient, unique_email: str) -> None:
    owner = await register_user(client, unique_email)
    token = owner["tokens"]["access_token"]

    resp = await client.get("/api/v1/customers", headers=auth_headers(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body["items"], list)
    assert body["total"] >= 0
    assert (await client.get("/api/v1/customers")).status_code == 401


async def test_welcome_message_reaches_guest(client: AsyncClient, unique_email: str) -> None:
    owner = await register_user(client, unique_email)
    token = owner["tokens"]["access_token"]
    tenant_id = owner["user"]["tenant_id"]

    patched = await client.patch(
        "/api/v1/tenants/current",
        json={"settings": {"welcome_message": "你好，这里是智能客服"}},
        headers=auth_headers(token),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["settings"]["welcome_message"] == "你好，这里是智能客服"

    # 访客没有令牌，读不到 /tenants/current：欢迎语只能随访客会话下发
    guest = await client.post("/api/v1/guest/sessions", headers=tenant_headers(tenant_id))
    assert guest.status_code == 200, guest.text
    assert guest.json()["welcome_message"] == "你好，这里是智能客服"
