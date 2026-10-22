"""质检抽检与敏感词测试（真实数据库 + 假 LLM）。

覆盖：
- 敏感词 block：不落库、返回 SENSITIVE_BLOCKED、命中留痕（message_id 为空）
- 敏感词 warn：照常回答、只留痕
- 敏感词 handoff：建工单并留痕
- 质检：抽样 → 评分 → 概览口径（覆盖率、平均分、标签分布）
- 标签非法 422；非坐席不可访问
"""

import uuid

from chat_helpers import (
    PASSWORD,
    auth_headers,
    ensure_guest_session,
    parse_sse,
    register_user,
    tenant_headers,
)
from httpx import AsyncClient


async def _configure(client: AsyncClient, token: str, *, words: list[str], action: str) -> None:
    resp = await client.patch(
        "/api/v1/tenants/current",
        json={"settings": {"sensitive_words": words, "sensitive_action": action}},
        headers=auth_headers(token),
    )
    assert resp.status_code == 200, resp.text


async def _new_conversation(client: AsyncClient, headers: dict[str, str]) -> str:
    conversation = await client.post("/api/v1/conversations", json={}, headers=headers)
    assert conversation.status_code == 201, conversation.text
    return str(conversation.json()["id"])


async def _stream(
    client: AsyncClient, conversation_id: str, content: str, headers: dict[str, str]
) -> list[tuple[str, dict]]:
    resp = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages/stream",
        json={"content": content},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return parse_sse(resp.text)


async def test_sensitive_block_rejects_message(
    client: AsyncClient, unique_email: str, fake_llm
) -> None:
    owner = await register_user(client, unique_email)
    token = owner["tokens"]["access_token"]
    tenant_id = owner["user"]["tenant_id"]
    headers = {**auth_headers(token), **tenant_headers(tenant_id)}
    await ensure_guest_session(client, tenant_id)
    await _configure(client, token, words=["赌博"], action="block")

    conversation_id = await _new_conversation(client, headers)
    events = await _stream(client, conversation_id, "你们这里有赌博业务吗", headers)

    assert events[-1][0] == "error"
    assert events[-1][1]["code"] == "SENSITIVE_BLOCKED"

    # 被拦下的内容不进会话历史，否则"拦了等于没拦"
    messages = await client.get(
        f"/api/v1/conversations/{conversation_id}/messages", headers=headers
    )
    assert messages.json()["items"] == []

    hits = await client.get("/api/v1/quality/sensitive-hits", headers=auth_headers(token))
    assert hits.status_code == 200
    body = hits.json()
    assert len(body) == 1
    assert body[0]["words"] == ["赌博"]
    assert body[0]["action"] == "block"
    assert body[0]["message_id"] is None
    assert "赌博" in body[0]["snippet"]


async def test_sensitive_warn_keeps_conversation_flow(
    client: AsyncClient, unique_email: str, fake_llm
) -> None:
    owner = await register_user(client, unique_email)
    token = owner["tokens"]["access_token"]
    tenant_id = owner["user"]["tenant_id"]
    headers = {**auth_headers(token), **tenant_headers(tenant_id)}
    await ensure_guest_session(client, tenant_id)
    await _configure(client, token, words=["赌博"], action="warn")

    conversation_id = await _new_conversation(client, headers)
    events = await _stream(client, conversation_id, "你们这里有赌博业务吗", headers)

    assert events[-1][0] == "done"
    conversation = await client.get(f"/api/v1/conversations/{conversation_id}", headers=headers)
    # warn 只留痕，不改变会话状态
    assert conversation.json()["status"] == "ai"

    hits = await client.get("/api/v1/quality/sensitive-hits", headers=auth_headers(token))
    assert len(hits.json()) == 1
    assert hits.json()[0]["action"] == "warn"
    assert hits.json()[0]["message_id"] is not None


async def test_sensitive_handoff_creates_ticket(
    client: AsyncClient, unique_email: str, fake_llm
) -> None:
    owner = await register_user(client, unique_email)
    token = owner["tokens"]["access_token"]
    tenant_id = owner["user"]["tenant_id"]
    headers = {**auth_headers(token), **tenant_headers(tenant_id)}
    await ensure_guest_session(client, tenant_id)
    await _configure(client, token, words=["投诉"], action="handoff")

    conversation_id = await _new_conversation(client, headers)
    events = await _stream(client, conversation_id, "我要投诉你们的客服", headers)

    names = [name for name, _ in events]
    assert "handoff" in names

    tickets = await client.get(
        "/api/v1/agent/tickets", params={"status": "pending"}, headers=auth_headers(token)
    )
    assert tickets.status_code == 200
    assert len(tickets.json()["items"]) == 1
    assert tickets.json()["items"][0]["reason"] == "keyword"

    hits = await client.get("/api/v1/quality/sensitive-hits", headers=auth_headers(token))
    assert hits.json()[0]["action"] == "handoff"


async def test_quality_sample_review_and_overview(
    client: AsyncClient, unique_email: str, fake_llm
) -> None:
    owner = await register_user(client, unique_email)
    token = owner["tokens"]["access_token"]
    tenant_id = owner["user"]["tenant_id"]
    headers = {**auth_headers(token), **tenant_headers(tenant_id)}
    await ensure_guest_session(client, tenant_id)

    for _ in range(2):
        conversation_id = await _new_conversation(client, headers)
        await _stream(client, conversation_id, "你好", headers)

    sampled = await client.post(
        "/api/v1/quality/reviews/sample", params={"limit": 2}, headers=auth_headers(token)
    )
    assert sampled.status_code == 201, sampled.text
    assert sampled.json() == 2

    pending = await client.get(
        "/api/v1/quality/reviews", params={"status": "pending"}, headers=auth_headers(token)
    )
    assert len(pending.json()) == 2

    # 再抽一次：同一会话不重复进队列
    again = await client.post(
        "/api/v1/quality/reviews/sample", params={"limit": 5}, headers=auth_headers(token)
    )
    assert again.json() == 0

    review_id = pending.json()[0]["id"]
    submitted = await client.post(
        f"/api/v1/quality/reviews/{review_id}",
        json={"score": 4, "tags": ["unresolved"], "comment": "没答上来"},
        headers=auth_headers(token),
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "done"
    assert submitted.json()["score"] == 4

    done = await client.get(
        "/api/v1/quality/reviews", params={"status": "done"}, headers=auth_headers(token)
    )
    assert len(done.json()) == 1

    overview = await client.get("/api/v1/quality/overview", headers=auth_headers(token))
    assert overview.status_code == 200
    body = overview.json()
    assert body["reviewed_count"] == 1
    assert body["pending_count"] == 1
    assert body["avg_score"] == 4.0
    assert body["conversations_total"] == 2
    assert body["coverage"] == 0.5
    assert body["tag_counts"] == {"unresolved": 1}


async def test_quality_rejects_unknown_tag(
    client: AsyncClient, unique_email: str, fake_llm
) -> None:
    owner = await register_user(client, unique_email)
    token = owner["tokens"]["access_token"]
    tenant_id = owner["user"]["tenant_id"]
    headers = {**auth_headers(token), **tenant_headers(tenant_id)}
    await ensure_guest_session(client, tenant_id)

    conversation_id = await _new_conversation(client, headers)
    await _stream(client, conversation_id, "你好", headers)
    await client.post(
        "/api/v1/quality/reviews/sample", params={"limit": 1}, headers=auth_headers(token)
    )
    pending = await client.get("/api/v1/quality/reviews", headers=auth_headers(token))
    review_id = pending.json()[0]["id"]

    resp = await client.post(
        f"/api/v1/quality/reviews/{review_id}",
        json={"score": 3, "tags": ["瞎编的标签"], "comment": ""},
        headers=auth_headers(token),
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "QUALITY_TAG_INVALID"


async def test_auto_sample_by_ratio(client: AsyncClient, unique_email: str) -> None:
    owner = await register_user(client, unique_email)
    token = owner["tokens"]["access_token"]
    tenant_id = owner["user"]["tenant_id"]
    headers = {**auth_headers(token), **tenant_headers(tenant_id)}
    await ensure_guest_session(client, tenant_id)

    await client.patch(
        "/api/v1/tenants/current",
        json={"settings": {"quality_sample_ratio": 1}},
        headers=auth_headers(token),
    )

    await _new_conversation(client, headers)

    pending = await client.get(
        "/api/v1/quality/reviews", params={"status": "pending"}, headers=auth_headers(token)
    )
    assert len(pending.json()) == 1


async def test_customer_cannot_access_quality(client: AsyncClient, unique_email: str) -> None:
    owner = await register_user(client, unique_email)
    token = owner["tokens"]["access_token"]

    created = await client.post(
        "/api/v1/tenants/current/members",
        json={
            "email": f"customer-{uuid.uuid4().hex[:8]}@example.com",
            "password": PASSWORD,
            "role": "customer",
        },
        headers=auth_headers(token),
    )
    assert created.status_code == 201, created.text
    email = created.json()["email"]

    login = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    customer_token = login.json()["tokens"]["access_token"]

    resp = await client.get("/api/v1/quality/overview", headers=auth_headers(customer_token))
    assert resp.status_code == 403
