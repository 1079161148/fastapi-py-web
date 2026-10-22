"""重排客户端（设计文档 9.4：rerank 可选，未配置则跳过）。

协议选择：Cohere / Jina / 多数 OpenAI 兼容网关都提供 `POST {base}/rerank`，
请求体为 `{model, query, documents, top_n}`，响应 `{results: [{index, relevance_score}]}`。
这里按该通用形态实现，字段做兼容读取（relevance_score / score）。
"""

from collections.abc import Sequence

import httpx

from app.core.logging import get_logger
from app.llm.base import LLMUpstreamError, RerankHit

logger = get_logger("llm.rerank")


class OpenAIRerankClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds

    async def rerank(
        self, query: str, documents: Sequence[str], *, top_n: int
    ) -> list[RerankHit]:
        if not documents:
            return []

        payload = {
            "model": self._model,
            "query": query,
            "documents": list(documents),
            "top_n": min(top_n, len(documents)),
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(self._timeout, connect=10.0)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{self._base_url}/rerank", json=payload, headers=headers
                )
        except httpx.TimeoutException as exc:
            raise LLMUpstreamError("重排服务响应超时") from exc
        except httpx.HTTPError as exc:
            logger.warning("rerank_transport_error", error=str(exc))
            raise LLMUpstreamError("无法连接重排服务") from exc

        if response.status_code >= 400:
            logger.warning(
                "rerank_rejected", status=response.status_code, body=response.text[:300]
            )
            raise LLMUpstreamError(f"重排服务返回错误（HTTP {response.status_code}）")

        return _parse_rerank(response.json(), top_n=top_n)


def _parse_rerank(payload: object, *, top_n: int) -> list[RerankHit]:
    if not isinstance(payload, dict):
        raise LLMUpstreamError("重排服务返回了非 JSON 对象")
    raw_results = payload.get("results") or payload.get("data")
    if not isinstance(raw_results, list):
        raise LLMUpstreamError("重排服务响应缺少 results 字段")

    hits: list[RerankHit] = []
    for entry in raw_results:
        if not isinstance(entry, dict):
            continue
        index = entry.get("index")
        if not isinstance(index, int):
            continue
        raw_score = entry.get("relevance_score", entry.get("score", 0.0))
        score = float(raw_score) if isinstance(raw_score, int | float) else 0.0
        hits.append(RerankHit(index=index, score=score))

    hits.sort(key=lambda hit: hit.score, reverse=True)
    return hits[:top_n]
