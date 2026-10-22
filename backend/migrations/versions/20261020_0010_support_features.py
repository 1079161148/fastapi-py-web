"""M8 运营支撑：满意度评分、快捷回复、审计日志

- messages.rating：1-5 满意度，未评价为 NULL
- quick_replies：坐席常用话术
- audit_logs：租户内敏感操作留痕

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "aics_app"
TENANT_EXPR = "nullif(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    # ---- 满意度评分 ----
    op.add_column("messages", sa.Column("rating", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_messages_rating_range", "messages", "rating IS NULL OR rating BETWEEN 1 AND 5"
    )

    # ---- 快捷回复 ----
    op.create_table(
        "quick_replies",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(100), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("category", sa.String(20), nullable=False, server_default="other"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.UUID(as_uuid=True), nullable=True),
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
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_quick_replies_tenant_id_category_sort", "quick_replies", ["tenant_id", "category", "sort_order"]
    )

    # ---- 审计日志 ----
    op.create_table(
        "audit_logs",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_name", sa.String(100), nullable=False, server_default=""),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("target_type", sa.String(50), nullable=False, server_default=""),
        sa.Column("target_id", sa.String(100), nullable=False, server_default=""),
        sa.Column("detail", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
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
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_audit_logs_tenant_id_created_at", "audit_logs", ["tenant_id", "created_at"]
    )
    op.create_index("ix_audit_logs_tenant_id_action", "audit_logs", ["tenant_id", "action"])

    # ---- 新表权限与 RLS（与 0002/0009 一致）----
    for table in ("quick_replies", "audit_logs"):
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
    for table in ("quick_replies", "audit_logs"):
        op.execute(f"DROP POLICY IF EXISTS {table}_isolation ON {table}")
        op.drop_table(table)
    op.drop_constraint("ck_messages_rating_range", "messages", type_="check")
    op.drop_column("messages", "rating")
