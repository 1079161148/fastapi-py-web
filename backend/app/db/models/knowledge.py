"""知识库数据模型（设计文档 6 / 9.4）。

    knowledge_bases 1 ── N documents 1 ── N chunks

chunks 同时承载两路检索所需的索引：
- embedding：pgvector，HNSW + 余弦距离；维度由 AI_EMBEDDING_DIM 固定
- tsv：**生成列**，由 search_text（应用层 jieba 分词后的空格串）自动派生。
  用生成列而不是应用写 tsv，是为了让一次 INSERT 同时完成文本与索引写入，
  避免"先插入再逐行 UPDATE tsv"的 N 次往返；to_tsvector 在 regconfig
  为常量时是 IMMUTABLE，满足生成列要求。

中文为什么不用 PG 默认分词：simple/english 配置把整句当一个 token，
中文关键词召回会完全失效。分词在应用层做（jieba），PG 只负责索引与排序。
"""

import uuid
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import Computed, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import get_settings
from app.db.base import Base, TimestampMixin

settings = get_settings()

# documents.status：uploaded → processing → done / failed
DOC_STATUS_UPLOADED = "uploaded"
DOC_STATUS_PROCESSING = "processing"
DOC_STATUS_DONE = "done"
DOC_STATUS_FAILED = "failed"

# 分块策略（设计文档 9.4：策略可配置，来源信息进 chunk 元数据）
CHUNK_STRATEGY_BY_HEADING = "by_heading"
CHUNK_STRATEGY_FIXED_WINDOW = "fixed_window"


class KnowledgeBase(Base, TimestampMixin):
    __tablename__ = "knowledge_bases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    # 该库默认分块策略，上传文档时固化到 chunk.chunk_strategy
    chunk_strategy: Mapped[str] = mapped_column(
        String(30), nullable=False, default=CHUNK_STRATEGY_BY_HEADING
    )

    __table_args__ = (Index("ix_knowledge_bases_tenant_id_created_at", "tenant_id", "created_at"),)


class Document(Base, TimestampMixin):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    kb_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    # 对象存储 key（local 后端下是相对路径），原件保留以便重跑解析
    object_key: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=DOC_STATUS_UPLOADED)
    error_msg: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("ix_documents_tenant_id_kb_id_created_at", "tenant_id", "kb_id", "created_at"),
        Index("ix_documents_tenant_id_status", "tenant_id", "status"),
    )


class Chunk(Base, TimestampMixin):
    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    kb_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # jieba 分词结果（空格分隔），tsv 生成列的输入
    search_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(settings.ai_embedding_dim), nullable=True
    )
    tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('simple'::regconfig, coalesce(search_text, ''))",
            persisted=True,
        ),
        nullable=True,
    )
    chunk_strategy: Mapped[str] = mapped_column(String(30), nullable=False, default="")
    # 溯源信息：标题路径、页码、字符区间等（引用展示用）
    chunk_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )

    __table_args__ = (
        Index("ix_chunks_tenant_id_kb_id", "tenant_id", "kb_id"),
        Index("ix_chunks_tenant_id_document_id", "tenant_id", "document_id"),
        Index("ix_chunks_tsv_gin", "tsv", postgresql_using="gin"),
    )
