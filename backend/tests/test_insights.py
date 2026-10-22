"""AI 效果洞察测试（真实数据库 + 假 LLM / 假嵌入）。

覆盖未命中问题看板的关键口径：
- 空租户：全 0，命中率为 None（不是 0%，避免误读）
- 无知识库时所有 AI 回答都未命中，同文提问按次数聚合
- 转人工固定文案回复不算 AI 回答，也不把前面的提问误报成知识缺口
- 有知识库且命中时，引用数与命中率正确
"""

import pytest
from chat_helpers import (
    auth_headers,
    ensure_guest_session,
    register_user,
    tenant_headers,
)
from httpx import AsyncClient
from knowledge_helpers import (
    FakeEmbedder,
    FakeQueue,
    create_kb,
    upload_markdown,
)


async def _ask(
    client: AsyncClient, conversation_id: str, content: str, headers: dict[str, str]
) -> None:
    resp = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages/stream",
        json={"content": content},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text


async def test_insights_empty_tenant(client: AsyncClient, unique_email: str) -> None:
    owner = await register_user(client, unique_email)
    token = owner["tokens"]["access_token"]

    resp = await client.get("/api/v1/insights/overview", headers=auth_headers(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_answers"] == 0
    assert body["cited_answers"] == 0
    assert body["hit_rate"] is None
    assert body["unanswered_total"] == 0
    assert body["unanswered"] == []
    assert body["knowledge_documents"] == 0


async def test_unanswered_questions_are_aggregated(
    client: AsyncClient, unique_email: str, fake_llm
) -> None:
    owner = await register_user(client, unique_email)
    token = owner["tokens"]["access_token"]
    tenant_id = owner["user"]["tenant_id"]
    headers = {**auth_headers(token), **tenant_headers(tenant_id)}
    await ensure_guest_session(client, tenant_id)

    # 两个会话问同一个问题：AI 无知识库可引用 → 全部未命中
    for _ in range(2):
        conversation = await client.post("/api/v1/conversations", json={}, headers=headers)
        conversation_id = conversation.json()["id"]
        await _ask(client, conversation_id, "怎么注销账号", headers)

    overview = await client.get("/api/v1/insights/overview", headers=auth_headers(token))
    body = overview.json()
    assert body["total_answers"] == 2
    assert body["cited_answers"] == 0
    assert body["hit_rate"] == 0.0
    assert body["unanswered_total"] == 2
    assert len(body["unanswered"]) == 1
    assert body["unanswered"][0]["question"] == "怎么注销账号"
    assert body["unanswered"][0]["ask_count"] == 2


async def test_locked_reply_is_not_counted(
    client: AsyncClient, unique_email: str, fake_llm
) -> None:
    owner = await register_user(client, unique_email)
    token = owner["tokens"]["access_token"]
    tenant_id = owner["user"]["tenant_id"]
    headers = {**auth_headers(token), **tenant_headers(tenant_id)}
    await ensure_guest_session(client, tenant_id)

    conversation = await client.post("/api/v1/conversations", json={}, headers=headers)
    conversation_id = conversation.json()["id"]

    # 坐席接管后 AI 闭嘴：回复是固定文案，不代表 AI 能力
    patched = await client.patch(
        f"/api/v1/conversations/{conversation_id}",
        json={"status": "human"},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    await _ask(client, conversation_id, "怎么注销账号", headers)

    body = (await client.get("/api/v1/insights/overview", headers=auth_headers(token))).json()
    assert body["total_answers"] == 0
    assert body["unanswered_total"] == 0
    assert body["unanswered"] == []


async def test_cited_answer_counts_as_hit(
    client: AsyncClient,
    unique_email: str,
    fake_llm,
    fake_queue: FakeQueue,
    fake_embedder: FakeEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = await register_user(client, unique_email)
    token = owner["tokens"]["access_token"]
    tenant_id = owner["user"]["tenant_id"]
    headers = {**auth_headers(token), **tenant_headers(tenant_id)}
    await ensure_guest_session(client, tenant_id)

    kb = await create_kb(client, auth_headers(token))
    document = await upload_markdown(client, auth_headers(token), kb["id"])
    monkeypatch.setattr("app.workers.tasks.get_embedding_client", lambda: fake_embedder)
    from app.workers.tasks import process_document

    await process_document({}, document["id"], tenant_id)

    conversation = await client.post("/api/v1/conversations", json={}, headers=headers)
    conversation_id = conversation.json()["id"]
    await _ask(client, conversation_id, "退款多久到账", headers)

    body = (await client.get("/api/v1/insights/overview", headers=auth_headers(token))).json()
    assert body["knowledge_documents"] == 1
    assert body["total_answers"] == 1
    assert body["cited_answers"] == 1
    assert body["hit_rate"] == 1.0
    assert body["unanswered_total"] == 0
