"""M3: knowledge_bases / documents / chunks（向量 + 关键词双索引）+ RLS

设计文档 6 / 9.4 / 10：

1. chunks 同时具备两路检索索引
   - embedding：vector(1536) + HNSW（vector_cosine_ops）
   - tsv：生成列由 search_text（应用层 jieba 分词）派生，GIN 索引
2. 三张表启用 FORCE RLS，策略与 0001/0002 一致
3. 索引以 tenant_id 打头，RLS 附加的租户条件才能走索引
4. 向量维度在此固定为 1536（与 AI_EMBEDDING_DIM 默认值一致）；
   换维度需要新迁移重建列与索引，不能只改环境变量

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "aics_app"
TENANT_TABLES = ("knowledge_bases", "documents", "chunks")
EMBEDDING_DIM = 1536


def upgrade() -> None:
    op.create_table(
        "knowledge_bases",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(500), nullable=False, server_default=""),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("chunk_strategy", sa.String(30), nullable=False, server_default="by_heading"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_knowledge_bases_tenant_id_created_at", "knowledge_bases", ["tenant_id", "created_at"]
    )

    op.create_table(
        "documents",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("kb_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("object_key", sa.String(500), nullable=False),
        sa.Column("content_type", sa.String(100), nullable=False, server_default=""),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="uploaded"),
        sa.Column("error_msg", sa.String(500), nullable=False, server_default=""),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["kb_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_documents_tenant_id_kb_id_created_at", "documents", ["tenant_id", "kb_id", "created_at"]
    )
    op.create_index("ix_documents_tenant_id_status", "documents", ["tenant_id", "status"])

    op.create_table(
        "chunks",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("kb_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
        sa.Column(
            "tsv",
            TSVECTOR,
            sa.Computed(
                "to_tsvector('simple'::regconfig, coalesce(search_text, ''))",
                persisted=True,
            ),
            nullable=True,
        ),
        sa.Column("chunk_strategy", sa.String(30), nullable=False, server_default=""),
        sa.Column("metadata", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["kb_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_chunks_tenant_id_kb_id", "chunks", ["tenant_id", "kb_id"])
    op.create_index("ix_chunks_tenant_id_document_id", "chunks", ["tenant_id", "document_id"])
    op.create_index("ix_chunks_tsv_gin", "chunks", ["tsv"], postgresql_using="gin")
    # HNSW：pgvector 的近似最近邻索引，余弦距离。
    # 不用 SQLAlchemy Index 表达（方言不支持 USING hnsw），直接写 DDL。
    op.execute(
        "CREATE INDEX ix_chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)"
    )

    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}")

    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS {table}_isolation ON {table}")
        op.execute(
            f"""
            CREATE POLICY {table}_isolation ON {table}
            USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)
            """
        )


def downgrade() -> None:
    for table in reversed(TENANT_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {table}_isolation ON {table}")
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
    for table in reversed(TENANT_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
