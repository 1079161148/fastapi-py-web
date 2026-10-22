"""chunks 检索仓库（设计文档 9.4：引擎可换，业务层只见接口）。

当前实现唯一用到的向量能力是 pgvector 的余弦距离与 tsvector 排序；
若未来 chunk 到千万级换 Qdrant/Milvus，只替换本文件的实现类。
"""

import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import ColumnElement, cast, func, literal, select
from sqlalchemy.dialects.postgresql import REGCONFIG
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.knowledge import Chunk, Document

# 关键词查询的词数上限：超长查询构造出的 tsquery 会拖慢规划与匹配
MAX_KEYWORD_TERMS = 32


@dataclass(frozen=True, slots=True)
class ChunkHit:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    kb_id: uuid.UUID
    document_name: str
    content: str
    metadata: dict[str, Any]
    score: float


class ChunkRepository(Protocol):
    async def vector_search(
        self,
        *,
        tenant_id: uuid.UUID,
        kb_ids: list[uuid.UUID],
        query_vector: list[float],
        limit: int,
    ) -> list[ChunkHit]: ...

    async def keyword_search(
        self,
        *,
        tenant_id: uuid.UUID,
        kb_ids: list[uuid.UUID],
        query_tokens: str,
        limit: int,
    ) -> list[ChunkHit]: ...


class PgVectorChunkRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def vector_search(
        self,
        *,
        tenant_id: uuid.UUID,
        kb_ids: list[uuid.UUID],
        query_vector: list[float],
        limit: int,
    ) -> list[ChunkHit]:
        if not kb_ids:
            return []
        distance = Chunk.embedding.cosine_distance(query_vector).label("distance")
        stmt = (
            select(Chunk, Document.filename, distance)
            .join(Document, Document.id == Chunk.document_id)
            .where(
                Chunk.tenant_id == tenant_id,
                Chunk.kb_id.in_(kb_ids),
                Chunk.embedding.is_not(None),
            )
            .order_by(distance)
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).all()
        return [_to_hit(chunk, filename, score=1.0 - float(dist)) for chunk, filename, dist in rows]

    async def keyword_search(
        self,
        *,
        tenant_id: uuid.UUID,
        kb_ids: list[uuid.UUID],
        query_tokens: str,
        limit: int,
    ) -> list[ChunkHit]:
        if not kb_ids or not query_tokens.strip():
            return []
        # 用 OR 而不是 plainto_tsquery 的 AND：
        # 中文问题常带口语修饰（"退款多久到账"），AND 会把只命中"退款"的段落全部丢掉，
        # 关键词通道直接失效。召回优先，精度交给 RRF 与 rerank。
        terms = query_tokens.split()[:MAX_KEYWORD_TERMS]
        if not terms:
            return []
        # 显式 cast 到 regconfig：to_tsquery 有 (text,text) 与 (regconfig,text) 两个重载
        tsquery: ColumnElement[Any] = func.to_tsquery(
            cast(literal("simple"), REGCONFIG), " | ".join(terms)
        )
        rank = func.ts_rank_cd(Chunk.tsv, tsquery).label("rank")
        stmt = (
            select(Chunk, Document.filename, rank)
            .join(Document, Document.id == Chunk.document_id)
            .where(
                Chunk.tenant_id == tenant_id,
                Chunk.kb_id.in_(kb_ids),
                Chunk.tsv.op("@@")(tsquery),
            )
            .order_by(rank.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).all()
        return [_to_hit(chunk, filename, score=float(score)) for chunk, filename, score in rows]


def _to_hit(chunk: Chunk, document_name: str, *, score: float) -> ChunkHit:
    return ChunkHit(
        chunk_id=chunk.id,
        document_id=chunk.document_id,
        kb_id=chunk.kb_id,
        document_name=document_name,
        content=chunk.content,
        metadata=chunk.chunk_metadata,
        score=score,
    )
