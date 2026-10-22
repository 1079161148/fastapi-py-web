"""M3 测试件：假嵌入 / 假重排 / 假队列，以及知识库操作封装。

假嵌入的设计要点：把分词结果哈希到固定维度并归一化。
这样"共享词越多 → 余弦相似度越高"在数学上成立，
向量召回才能被断言，而不是只能验证"接口没报错"。
"""

import hashlib
import math
from collections.abc import Sequence

from httpx import AsyncClient

from app.core.config import get_settings
from app.llm.base import RerankHit
from app.services.ingestion import tokenize_for_search

# 跟随 AI_EMBEDDING_DIM：列维度在共享开发库里随配置走，测试假向量必须同维
EMBEDDING_DIM = get_settings().ai_embedding_dim

SAMPLE_MARKDOWN = """# 售后服务政策

## 退款流程

用户可在签收后 7 天内申请退款。审核通过后，退款将在 3 个工作日内原路退回。
超过 7 天的订单需要人工客服介入处理。

## 发票

电子发票会在订单完成后 24 小时内发送到下单邮箱。如需增值税专用发票，请联系客服。
""".encode()

SAMPLE_MARKDOWN_FILENAME = "policy.md"


class FakeEmbedder:
    def __init__(self) -> None:
        self.batches: list[int] = []

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.batches.append(len(texts))
        return [self._vector(text) for text in texts]

    @staticmethod
    def _vector(text: str) -> list[float]:
        vector = [0.0] * EMBEDDING_DIM
        for token in tokenize_for_search(text).split():
            index = int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16) % EMBEDDING_DIM
            vector[index] += 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            vector[0] = 1.0
            return vector
        return [value / norm for value in vector]


class FakeReranker:
    """故意反转候选顺序：用于验证 rerank 结果确实被采纳（而非原样返回）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def rerank(self, query: str, documents: Sequence[str], *, top_n: int) -> list[RerankHit]:
        self.calls += 1
        indexes = list(range(len(documents)))[::-1]
        return [
            RerankHit(index=index, score=float(len(indexes) - rank))
            for rank, index in enumerate(indexes)
        ][:top_n]


class FakeQueue:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    async def enqueue_job(self, function: str, *args: object, **kwargs: object) -> None:
        self.jobs.append((function, args, kwargs))


async def create_kb(client: AsyncClient, headers: dict[str, str], name: str = "客服知识库") -> dict:
    resp = await client.post(
        "/api/v1/knowledge/bases",
        json={"name": name, "description": "M3 测试库", "chunk_strategy": "by_heading"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def upload_markdown(
    client: AsyncClient,
    headers: dict[str, str],
    kb_id: str,
    *,
    filename: str = SAMPLE_MARKDOWN_FILENAME,
    content: bytes = SAMPLE_MARKDOWN,
) -> dict:
    resp = await client.post(
        "/api/v1/knowledge/documents",
        data={"kb_id": kb_id},
        files={"file": (filename, content, "text/markdown")},
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    return resp.json()
