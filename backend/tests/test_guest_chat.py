"""访客会话与流式对话集成测试（真实数据库 + 假 LLM）。

覆盖设计文档 M2 验收点：
- 访客 cookie 签发（HttpOnly / SameSite=Lax），重复请求复用同一会话
- SSE 流式对话：message → delta* → done，assistant 落库带 tokens/耗时
- 越权隔离：另一个访客读不到他人会话（404）
- 无凭证 401；模型未配置时 503 且不落库
"""

from chat_helpers import (
    FakeLLM,
    ensure_guest_session,
    parse_sse,
    register_user,
    tenant_headers,
)
from httpx import AsyncClient

from app.core.config import get_settings


async def test_guest_session_sets_httponly_cookie(
    client: AsyncClient, unique_email: str
) -> None:
    data = await register_user(client, unique_email)
    headers = tenant_headers(data["user"]["tenant_id"])

    first = await client.post("/api/v1/guest/sessions", headers=headers)
    assert first.status_code == 200, first.text
    payload = first.json()
    assert payload["status"] == "active"

    cookie = first.headers["set-cookie"].lower()
    assert "guest_id=" in cookie
    # 设计文档 5.5：HttpOnly + SameSite=Lax，不允许 JS 读取
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/" in cookie

    second = await client.post("/api/v1/guest/sessions", headers=headers)
    assert second.json()["guest_id"] == payload["guest_id"], "刷新页面应复用同一访客会话"


async def test_guest_chat_stream_persists_messages(
    client: AsyncClient, unique_email: str, fake_llm: FakeLLM
) -> None:
    data = await register_user(client, unique_email)
    tenant_id = data["user"]["tenant_id"]
    headers = tenant_headers(tenant_id)
    await ensure_guest_session(client, tenant_id)

    created = await client.post("/api/v1/conversations", json={}, headers=headers)
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]
    assert created.json()["owner_type"] == "guest"

    resp = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages/stream",
        json={"content": "你们几点上班？"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")

    events = parse_sse(resp.text)
    names = [name for name, _ in events]
    assert names[0] == "message", "首帧回传用户消息（前端据此刻画自己的气泡）"
    assert names.count("delta") == 3
    assert names[-1] == "done"

    done = events[-1][1]
    assert done["content"] == fake_llm.full_text
    assert done["tokens"] == 15
    assert done["latency_ms"] is not None

    # prompt 组装：system 打头，最后一条是用户刚发的内容
    assert fake_llm.last_prompt[0].role == "system"
    assert fake_llm.last_prompt[-1].content == "你们几点上班？"

    messages = await client.get(
        f"/api/v1/conversations/{conversation_id}/messages", headers=headers
    )
    items = messages.json()["items"]
    assert [item["role"] for item in items] == ["user", "assistant"]
    assert items[1]["content"] == fake_llm.full_text
    assert items[1]["tokens"] == 15

    # 会话标题由首条用户消息生成
    detail = await client.get(f"/api/v1/conversations/{conversation_id}", headers=headers)
    assert detail.json()["title"] == "你们几点上班？"


async def test_messages_cursor_pagination(
    client: AsyncClient, unique_email: str, fake_llm: FakeLLM
) -> None:
    data = await register_user(client, unique_email)
    tenant_id = data["user"]["tenant_id"]
    headers = tenant_headers(tenant_id)
    await ensure_guest_session(client, tenant_id)

    conversation_id = (
        await client.post("/api/v1/conversations", json={}, headers=headers)
    ).json()["id"]
    for content in ("第一问", "第二问"):
        resp = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages/stream",
            json={"content": content},
            headers=headers,
        )
        assert resp.status_code == 200

    first_page = await client.get(
        f"/api/v1/conversations/{conversation_id}/messages",
        params={"limit": 2},
        headers=headers,
    )
    body = first_page.json()
    assert [item["role"] for item in body["items"]] == ["user", "assistant"]
    assert body["items"][0]["content"] == "第二问"
    assert body["next_cursor"] is not None

    second_page = await client.get(
        f"/api/v1/conversations/{conversation_id}/messages",
        params={"limit": 2, "cursor": body["next_cursor"]},
        headers=headers,
    )
    older = second_page.json()
    assert [item["role"] for item in older["items"]] == ["user", "assistant"]
    assert older["items"][0]["content"] == "第一问"
    assert older["next_cursor"] is None


async def test_other_guest_cannot_access_conversation(
    client: AsyncClient, unique_email: str, fake_llm: FakeLLM
) -> None:
    data = await register_user(client, unique_email)
    tenant_id = data["user"]["tenant_id"]
    headers = tenant_headers(tenant_id)
    await ensure_guest_session(client, tenant_id)
    conversation_id = (
        await client.post("/api/v1/conversations", json={}, headers=headers)
    ).json()["id"]

    # 换一个访客：清掉 cookie 后重新签发
    client.cookies.clear()
    other_guest = await ensure_guest_session(client, tenant_id)

    read = await client.get(f"/api/v1/conversations/{conversation_id}", headers=headers)
    assert read.status_code == 404

    stream = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages/stream",
        json={"content": "偷看"},
        headers=headers,
    )
    assert stream.status_code == 404

    messages = await client.get(
        f"/api/v1/conversations/{conversation_id}/messages", headers=headers
    )
    assert messages.status_code == 404

    # 自己的新会话只有自己的数据（确认不是 RLS 把所有人都挡住）
    current = await client.get("/api/v1/guest/sessions/current", headers=headers)
    assert current.json()["guest_id"] == other_guest
    assert current.json()["conversation_count"] == 0


async def test_conversations_require_identity(client: AsyncClient, unique_email: str) -> None:
    data = await register_user(client, unique_email)
    headers = tenant_headers(data["user"]["tenant_id"])
    client.cookies.clear()

    resp = await client.get("/api/v1/conversations", headers=headers)
    assert resp.status_code == 401
    assert resp.json()["code"] == "GUEST_SESSION_INVALID"


async def test_stream_without_llm_config_returns_503(
    client: AsyncClient, unique_email: str, monkeypatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "ai_api_key", "")
    monkeypatch.setattr(settings, "ai_base_url", "")
    monkeypatch.setattr(settings, "ai_model", "")

    data = await register_user(client, unique_email)
    tenant_id = data["user"]["tenant_id"]
    headers = tenant_headers(tenant_id)
    await ensure_guest_session(client, tenant_id)
    conversation_id = (
        await client.post("/api/v1/conversations", json={}, headers=headers)
    ).json()["id"]

    resp = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages/stream",
        json={"content": "在吗"},
        headers=headers,
    )
    assert resp.status_code == 503
    body = resp.json()
    assert body["code"] == "LLM_NOT_CONFIGURED"
    # 错误信息必须可操作：明确指出要设置哪些变量
    assert "AI_API_KEY" in body["message"]
    assert "AI_MODEL" in body["message"]

    # 配置错误不该污染会话历史
    messages = await client.get(
        f"/api/v1/conversations/{conversation_id}/messages", headers=headers
    )
    assert messages.json()["items"] == []
