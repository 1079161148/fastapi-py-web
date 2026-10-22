"""混合检索编排（设计文档 9.4）。

链路：查询 → 向量召回 top-50（HNSW）→ 关键词召回 top-50（tsvector + GIN）
      → RRF 融合去重 → 可选 rerank → top-k

RRF 用排名而非分数融合：向量余弦相似度与 ts_rank_cd 量纲完全不同，
直接加权相加需要逐数据集调参，排名融合没有这个负担（Cormack 等人的原始结论）。

降级策略（设计文档 10）：rerank 失败只记日志并按 RRF 顺序继续；
embedding 失败由调用方决定降级（聊天链路会退化为纯模型回答并显式标注）。
"""

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.db.models.knowledge import KnowledgeBase
from app.db.models.usage import KIND_EMBEDDING, KIND_RERANK
from app.llm.base import EmbeddingClient, RerankClient
from app.repositories.chunks import ChunkHit, PgVectorChunkRepository
from app.services import usage as usage_service
from app.services.ingestion import tokenize_for_search

logger = get_logger("retrieval")

# RRF 常数：60 是原论文经验值，排序靠前的名次差距被压平，避免单路支配结果
RRF_K = 60
# rerank 候选池倍数：只把前 N*top_k 条交给重排模型，控制调用成本与延迟
RERANK_MULTIPLIER = 4


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    kb_id: uuid.UUID
    document_name: str
    content: str
    metadata: dict[str, Any]
    # score：RRF 融合分；启用 rerank 时改为重排模型给出的相关性分
    score: float
    vector_score: float | None = None
    keyword_score: float | None = None
    reranked: bool = False


@dataclass(frozen=True, slots=True)
class RetrievalOutcome:
    """聊天链路使用的检索结果：degraded 表示"本该检索但没检索成"。"""

    items: list[RetrievedChunk] = field(default_factory=list)
    degraded: bool = False
    reason: str = ""


@dataclass(frozen=True, slots=True)
class SearchResult:
    """单次混合检索的结果：degraded 表示部分检索路不可用（如向量路失败）。"""

    items: list[RetrievedChunk] = field(default_factory=list)
    degraded: bool = False
    reason: str = ""


async def active_kb_ids(session: AsyncSession, tenant_id: uuid.UUID) -> list[uuid.UUID]:
    rows = await session.execute(
        select(KnowledgeBase.id).where(
            KnowledgeBase.tenant_id == tenant_id,
            KnowledgeBase.status == "active",
        )
    )
    return list(rows.scalars().all())


async def search(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kb_ids: list[uuid.UUID],
    query: str,
    embedder: EmbeddingClient,
    reranker: RerankClient | None = None,
    recall_k: int | None = None,
    top_k: int | None = None,
) -> SearchResult:
    """执行一次混合检索。

    嵌入失败不整体失败：退化为纯关键词检索并在结果里标注 degraded——
    免费嵌入网关偶发 504 时，关键词路仍能给出可用结果，比直接 502 有用。
    """
    settings = get_settings()
    recall = recall_k or settings.retrieval_recall_k
    limit = top_k or settings.retrieval_top_k
    if not kb_ids or not query.strip():
        return SearchResult()

    degraded = False
    reason = ""
    try:
        vectors = await embedder.embed([query])
    except AppError as exc:
        logger.warning(
            "embedding_failed_keyword_only", code=exc.code, tenant_id=str(tenant_id)
        )
        degraded = True
        reason = f"向量检索不可用（{exc.code}），本次结果仅来自关键词检索"
        # None = 调用失败（上方已记日志）；[] = 调用成功但返回空，单独记日志
        vectors = None
    if vectors == []:
        degraded = True
        reason = "嵌入服务未返回向量，本次结果仅来自关键词检索"
        logger.warning("embedding_empty_keyword_only", tenant_id=str(tenant_id))
    # 检索侧的嵌入按查询字符数计（每次聊天一次，量级小但必须可对账）
    await usage_service.record(
        tenant_id=tenant_id,
        kind=KIND_EMBEDDING,
        model=settings.ai_embedding_model,
        units=len(query),
    )

    repository = PgVectorChunkRepository(session)
    vector_hits: list[ChunkHit] = []
    if vectors:
        # 只有真的调用了嵌入才记账：降级为纯关键词时没产生 embedding 费用
        await usage_service.record(
            tenant_id=tenant_id,
            kind=KIND_EMBEDDING,
            model=settings.ai_embedding_model,
            units=len(query),
        )
        vector_hits = await repository.vector_search(
            tenant_id=tenant_id, kb_ids=kb_ids, query_vector=vectors[0], limit=recall
        )
    keyword_hits = await repository.keyword_search(
        tenant_id=tenant_id,
        kb_ids=kb_ids,
        query_tokens=tokenize_for_search(query),
        limit=recall,
    )

    fused = _rrf_fuse(vector_hits, keyword_hits)
    if not fused:
        return SearchResult(degraded=degraded, reason=reason)

    if reranker is None:
        return SearchResult(
            items=[_to_retrieved(item) for item in fused[:limit]],
            degraded=degraded,
            reason=reason,
        )

    pool_size = min(len(fused), max(limit * RERANK_MULTIPLIER, limit))
    pool = fused[:pool_size]
    documents = [item.hit.content for item in pool]
    try:
        rerank_hits = await reranker.rerank(
            query, documents, top_n=min(limit, pool_size)
        )
    except AppError as exc:
        # 可选能力失败不影响回答：按融合顺序返回，并记录以便观测
        logger.warning("rerank_failed_degrade", code=exc.code, pool=pool_size)
        return SearchResult(
            items=[_to_retrieved(item) for item in fused[:limit]],
            degraded=degraded,
            reason=reason,
        )

    await usage_service.record(
        tenant_id=tenant_id,
        kind=KIND_RERANK,
        model=settings.ai_rerank_model,
        units=len(query) + sum(len(document) for document in documents),
    )

    results: list[RetrievedChunk] = []
    for rerank_hit in rerank_hits:
        if 0 <= rerank_hit.index < len(pool):
            results.append(_to_retrieved(pool[rerank_hit.index], score=rerank_hit.score))
    if not results:  # 重排返回空（网关异常但状态 200）同样降级
        return SearchResult(
            items=[_to_retrieved(item) for item in fused[:limit]],
            degraded=degraded,
            reason=reason,
        )
    return SearchResult(items=results, degraded=degraded, reason=reason)


async def retrieve_for_chat(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    query: str,
    embedder: EmbeddingClient | None,
    reranker: RerankClient | None,
) -> RetrievalOutcome:
    """聊天场景的检索入口：任何失败都转为"无依据回答"，绝不中断对话。"""
    settings = get_settings()
    if not settings.rag_enabled:
        return RetrievalOutcome(reason="知识库检索已关闭（RAG_ENABLED=false）")

    kb_ids = await active_kb_ids(session, tenant_id)
    if not kb_ids:
        return RetrievalOutcome(reason="租户还没有可用的知识库")

    if embedder is None:
        return RetrievalOutcome(degraded=True, reason="嵌入模型未配置，本次回答未使用知识库")

    try:
        result = await search(
            session,
            tenant_id=tenant_id,
            kb_ids=kb_ids,
            query=query,
            embedder=embedder,
            reranker=reranker,
        )
    except AppError as exc:
        logger.warning("retrieval_failed_degrade", code=exc.code, tenant_id=str(tenant_id))
        return RetrievalOutcome(
            degraded=True, reason=f"知识库检索失败（{exc.code}），本次回答未引用"
        )

    # 向量路降级但关键词路有结果：照样引用，同时把降级原因带给前端气泡
    if not result.items:
        if result.degraded:
            return RetrievalOutcome(degraded=True, reason=result.reason)
        return RetrievalOutcome(reason="未检索到相关资料")
    return RetrievalOutcome(
        items=result.items, degraded=result.degraded, reason=result.reason
    )


@dataclass(frozen=True, slots=True)
class _FusedHit:
    hit: ChunkHit
    score: float
    vector_score: float | None
    keyword_score: float | None


def _rrf_fuse(vector_hits: list[ChunkHit], keyword_hits: list[ChunkHit]) -> list[_FusedHit]:
    scores: dict[uuid.UUID, float] = {}
    vector_scores: dict[uuid.UUID, float] = {}
    keyword_scores: dict[uuid.UUID, float] = {}
    hits: dict[uuid.UUID, ChunkHit] = {}

    for rank, hit in enumerate(vector_hits, start=1):
        hits.setdefault(hit.chunk_id, hit)
        vector_scores[hit.chunk_id] = hit.score
        scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (RRF_K + rank)
    for rank, hit in enumerate(keyword_hits, start=1):
        hits.setdefault(hit.chunk_id, hit)
        keyword_scores.setdefault(hit.chunk_id, hit.score)
        scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (RRF_K + rank)

    ordered = sorted(hits.values(), key=lambda item: scores[item.chunk_id], reverse=True)
    return [
        _FusedHit(
            hit=hit,
            score=scores[hit.chunk_id],
            vector_score=vector_scores.get(hit.chunk_id),
            keyword_score=keyword_scores.get(hit.chunk_id),
        )
        for hit in ordered
    ]


def _to_retrieved(item: _FusedHit, *, score: float | None = None) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=item.hit.chunk_id,
        document_id=item.hit.document_id,
        kb_id=item.hit.kb_id,
        document_name=item.hit.document_name,
        content=item.hit.content,
        metadata=item.hit.metadata,
        score=item.score if score is None else score,
        vector_score=item.vector_score,
        keyword_score=item.keyword_score,
        reranked=score is not None,
    )
