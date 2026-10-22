"""M2 测试公共件：假 LLM、SSE 解析、注册/访客会话构造。

假 LLM 的作用：让流式链路（编排 → SSE 转发 → 落库）在无 API Key 的
环境里也能完整验证；真实模型调用只在人工端到端验证时使用。
"""

import json
from collections.abc import AsyncIterator, Sequence

from httpx import AsyncClient

from app.llm.base import ChatMessage, StreamChunk, TokenUsage

PASSWORD = "test-password-123"


class FakeLLM:
    """按预置分片产出，末帧带 usage；记录 prompt 供断言。"""

    def __init__(self, chunks: Sequence[str] = ("你好", "，我是", "客服助手")) -> None:
        self._chunks = list(chunks)
        self.last_prompt: list[ChatMessage] = []
        self.call_count = 0

    def chat_stream(
        self, messages: Sequence[ChatMessage], *, temperature: float = 0.7
    ) -> AsyncIterator[StreamChunk]:
        self.last_prompt = list(messages)
        self.call_count += 1
        return self._stream_words()

    async def _stream_words(self) -> AsyncIterator[StreamChunk]:
        for word in self._chunks:
            yield StreamChunk(delta=word)
        yield StreamChunk(usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15))

    @property
    def full_text(self) -> str:
        return "".join(self._chunks)


async def register_user(
    client: AsyncClient, email: str, tenant_name: str = "M2 测试租户"
) -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "tenant_name": tenant_name},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def tenant_headers(tenant_id: str) -> dict[str, str]:
    """访客请求靠租户头确定落库租户（生产由子域名解析）。"""
    return {"X-Tenant-Id": tenant_id}


def auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


async def ensure_guest_session(client: AsyncClient, tenant_id: str) -> str:
    resp = await client.post("/api/v1/guest/sessions", headers=tenant_headers(tenant_id))
    assert resp.status_code == 200, resp.text
    return str(resp.json()["guest_id"])


def parse_sse(raw: str) -> list[tuple[str, dict]]:
    """解析 SSE 文本为 [(event, data), ...]；只依赖 event/data 两行。"""
    events: list[tuple[str, dict]] = []
    for block in raw.strip().split("\n\n"):
        if not block.strip():
            continue
        name = "message"
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
        if data_lines:
            events.append((name, json.loads("\n".join(data_lines))))
    return events
