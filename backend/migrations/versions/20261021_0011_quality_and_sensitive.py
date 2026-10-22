"""M9 质检抽检与敏感词

- quality_reviews：抽检队列与评分结果（一条会话一条，抽样时建 pending）
- sensitive_hits：敏感词命中留痕（不放 messages 里：命中是运营事件，
  与消息本身的正交，且命中后消息可能根本没落库）

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "aics_app"
TENANT_EXPR = "nullif(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "quality_reviews",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", sa.UUID(as_uuid=True), nullable=False),
        # 抽样人（自动抽样时为空）；质检评分人单独记，便于"谁评的"
        sa.Column("sampled_by", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("reviewer_id", sa.UUID(as_uuid=True), nullable=True),
        # pending / done
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("score", sa.Integer(), nullable=True),
        # 问题标签：未解答 / 答非所问 / 话术违规 …
        sa.Column("tags", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("comment", sa.Text(), nullable=False, server_default=""),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sampled_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reviewer_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("score IS NULL OR score BETWEEN 1 AND 5", name="ck_quality_score"),
    )
    op.create_index(
        "ix_quality_reviews_tenant_id_status_created_at",
        "quality_reviews",
        ["tenant_id", "status", "created_at"],
    )
    # 同一会话只抽检一次（抽样去重靠它，不是靠应用层先查后插）
    op.create_index(
        "uq_quality_reviews_conversation",
        "quality_reviews",
        ["conversation_id"],
        unique=True,
    )

    op.create_table(
        "sensitive_hits",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", sa.UUID(as_uuid=True), nullable=False),
        # blocked 时不落库消息，message_id 为空
        sa.Column("message_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("words", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        # block / handoff / warn
        sa.Column("action", sa.String(20), nullable=False, server_default="warn"),
        sa.Column("snippet", sa.String(500), nullable=False, server_default=""),
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
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_sensitive_hits_tenant_id_created_at", "sensitive_hits", ["tenant_id", "created_at"]
    )
    op.create_index("ix_sensitive_hits_tenant_id_action", "sensitive_hits", ["tenant_id", "action"])

    for table in ("quality_reviews", "sensitive_hits"):
        op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_isolation ON {table}
            USING (tenant_id = {TENANT_EXPR})
            WITH CHECK (tenant_id = {TENANT_EXPR})
            """
        )


def downgrade() -> None:
    for table in ("quality_reviews", "sensitive_hits"):
        op.execute(f"DROP POLICY IF EXISTS {table}_isolation ON {table}")
        op.drop_table(table)
