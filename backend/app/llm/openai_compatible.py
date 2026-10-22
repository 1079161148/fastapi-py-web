"""OpenAI 兼容协议的流式客户端（设计文档 3.1：一套代码切供应商）。

只用 httpx 直连 /chat/completions，不引入各家 SDK：
- 供应商覆盖：OpenAI / DeepSeek / 通义 / 智谱 / 本地 vLLM 均提供该端点
- 失败要快且明确：配置缺失在工厂层抛 LLMNotConfiguredError（503），
  上游异常抛 LLMUpstreamError（502），绝不静默返回空串

AI_BASE_URL 约定：填写包含版本段的基地址（如 https://api.deepseek.com/v1），
这里只做 `rstrip('/') + '/chat/completions'`，不猜路径、不自动补 /v1。
"""

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from app.core.logging import get_logger
from app.llm.base import (
    ChatMessage,
    LLMUpstreamError,
    StreamChunk,
    TokenUsage,
)

logger = get_logger("llm")

# 上游错误体截断长度：只用于日志与 detail，避免把整页 HTML 塞进响应
ERROR_BODY_LIMIT = 500
# 单次 embeddings 请求的文本条数：过大易触发网关体积限制，过小则往返开销高
EMBED_BATCH_SIZE = 32


class OpenAICompatibleClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float = 120.0,
        expected_embedding_dim: int | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds
        self._expected_embedding_dim = expected_embedding_dim

    @property
    def model(self) -> str:
        return self._model

    def chat_stream(
        self, messages: Sequence[ChatMessage], *, temperature: float = 0.7
    ) -> AsyncIterator[StreamChunk]:
        return self._chat_stream(messages, temperature=temperature)

    async def _chat_stream(
        self, messages: Sequence[ChatMessage], *, temperature: float
    ) -> AsyncIterator[StreamChunk]:
        payload = {
            "model": self._model,
            "messages": [_to_wire_message(m) for m in messages],
            "temperature": temperature,
            "stream": True,
            # 末帧附 usage，供成本核算；不支持的网关会忽略多余字段
            "stream_options": {"include_usage": True},
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        timeout = httpx.Timeout(self._timeout, connect=10.0)

        url = f"{self._base_url}/chat/completions"
        try:
            async with (
                httpx.AsyncClient(timeout=timeout) as client,
                client.stream("POST", url, json=payload, headers=headers) as response,
            ):
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    # 不记录请求头：其中含 Authorization
                    logger.warning(
                        "llm_upstream_rejected",
                        status=response.status_code,
                        body=body[:ERROR_BODY_LIMIT],
                    )
                    raise LLMUpstreamError(
                        f"AI 服务返回错误（HTTP {response.status_code}），请稍后重试或联系管理员"
                    )

                async for line in response.aiter_lines():
                    chunk = _parse_sse_line(line)
                    if chunk is not None:
                        yield chunk
        except httpx.TimeoutException as exc:
            raise LLMUpstreamError("AI 服务响应超时，请稍后重试") from exc
        except httpx.HTTPError as exc:
            # 连接失败 / 传输中断：对外统一为可重试的上游错误
            logger.warning("llm_transport_error", error=str(exc))
            raise LLMUpstreamError("无法连接 AI 服务，请稍后重试") from exc

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """批量向量化（OpenAI 兼容 /embeddings）。顺序与输入严格一致。"""
        if not texts:
            return []

        url = f"{self._base_url}/embeddings"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(self._timeout, connect=10.0)
        vectors: list[list[float]] = []

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                for start in range(0, len(texts), EMBED_BATCH_SIZE):
                    batch = list(texts[start : start + EMBED_BATCH_SIZE])
                    response = await client.post(
                        url, json={"model": self._model, "input": batch}, headers=headers
                    )
                    if response.status_code >= 400:
                        body = response.text
                        logger.warning(
                            "llm_embedding_rejected",
                            status=response.status_code,
                            body=body[:ERROR_BODY_LIMIT],
                        )
                        raise LLMUpstreamError(
                            f"嵌入服务返回错误（HTTP {response.status_code}），"
                            "请检查 AI_EMBEDDING_* 配置"
                        )
                    vectors.extend(_parse_embeddings(response.json()))
        except httpx.TimeoutException as exc:
            raise LLMUpstreamError("嵌入服务响应超时，请稍后重试") from exc
        except httpx.HTTPError as exc:
            logger.warning("llm_embedding_transport_error", error=str(exc))
            raise LLMUpstreamError("无法连接嵌入服务，请稍后重试") from exc

        if len(vectors) != len(texts):
            raise LLMUpstreamError(
                f"嵌入返回数量不匹配：请求 {len(texts)} 条，返回 {len(vectors)} 条"
            )
        if self._expected_embedding_dim is not None:
            for vector in vectors:
                if len(vector) != self._expected_embedding_dim:
                    raise LLMUpstreamError(
                        f"嵌入维度不一致：模型返回 {len(vector)} 维，"
                        f"AI_EMBEDDING_DIM 配置为 {self._expected_embedding_dim}。"
                        "换模型需同时重建 chunks.embedding 列"
                    )
        return vectors


def _to_wire_message(message: ChatMessage) -> dict[str, Any]:
    """ChatMessage → OpenAI 兼容的 message 结构。

    纯文本消息保持字符串 content（老网关对数组形式兼容性不一），
    只有带图时才转成 content 数组。
    """
    if not message.images:
        return {"role": message.role, "content": message.content}

    parts: list[dict[str, Any]] = []
    if message.content:
        parts.append({"type": "text", "text": message.content})
    for image in message.images:
        parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{image.mime_type};base64,{image.data_b64}"},
            }
        )
    return {"role": message.role, "content": parts}


def _parse_sse_line(line: str) -> StreamChunk | None:
    """解析一行 SSE；非数据行、[DONE]、心跳注释一律返回 None。

    网关偶发非 JSON 心跳（如 `: ping`），跳过而不是抛错——
    一条坏行不应中断整轮回答。
    """
    if not line or line.startswith(":"):
        return None
    if not line.startswith("data:"):
        return None

    data = line[len("data:") :].strip()
    if not data or data == "[DONE]":
        return None

    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        logger.debug("llm_sse_non_json", line=data[:120])
        return None
    if not isinstance(payload, dict):
        return None

    usage_payload = payload.get("usage")
    usage = _parse_usage(usage_payload) if isinstance(usage_payload, dict) else None

    choices = payload.get("choices")
    delta_text = ""
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            delta = first.get("delta")
            if isinstance(delta, dict):
                content = delta.get("content")
                if isinstance(content, str):
                    delta_text = content

    if not delta_text and usage is None:
        return None
    return StreamChunk(delta=delta_text, usage=usage)


def _parse_usage(raw: dict[str, object]) -> TokenUsage:
    def _as_int(key: str) -> int:
        value = raw.get(key)
        return value if isinstance(value, int) else 0

    return TokenUsage(
        prompt_tokens=_as_int("prompt_tokens"),
        completion_tokens=_as_int("completion_tokens"),
        total_tokens=_as_int("total_tokens"),
    )


def _parse_embeddings(payload: Any) -> list[list[float]]:
    """解析 /embeddings 响应，并按 index 归位（部分网关返回顺序不保证）。"""
    if not isinstance(payload, dict):
        raise LLMUpstreamError("嵌入服务返回了非 JSON 对象，请检查 AI_BASE_URL 是否正确")
    data = payload.get("data")
    if not isinstance(data, list):
        raise LLMUpstreamError("嵌入服务响应缺少 data 字段，请检查模型是否为嵌入模型")

    indexed: list[tuple[int, list[float]]] = []
    for position, entry in enumerate(data):
        if not isinstance(entry, dict):
            continue
        vector = entry.get("embedding")
        if not isinstance(vector, list) or not vector:
            continue
        try:
            values = [float(value) for value in vector]
        except (TypeError, ValueError) as exc:
            raise LLMUpstreamError("嵌入向量包含非数值元素") from exc
        raw_index = entry.get("index")
        index = raw_index if isinstance(raw_index, int) else position
        indexed.append((index, values))

    indexed.sort(key=lambda item: item[0])
    return [vector for _, vector in indexed]
