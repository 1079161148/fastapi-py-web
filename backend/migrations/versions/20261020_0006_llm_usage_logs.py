"""M7: llm_usage_logs 用量与成本流水

设计文档 6 / 10：
- 每次模型调用一行，按天/模型/类型聚合成看板
- 不含任何会话原文（设计文档 8：日志脱敏，用量表不存内容）

索引设计（对齐设计文档 10「多租户开销」：索引以 tenant_id 打头）：

- `(tenant_id, created_at)`：看板的时间范围扫描
- `(tenant_id, kind, model)`：按类型/模型聚合
- `(tenant_id, conversation_id)`：单会话成本追溯

conversation_id 外键用 SET NULL 而非 CASCADE：会话清理（含 guest TTL 任务）
不应该把历史成本一起抹掉，否则月度账单会随数据清理而缩水。

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "aics_app"
TABLE = "llm_usage_logs"
# 与 0005 一致：SET LOCAL 回滚后 current_setting 返回空串，必须 nullif 成 NULL
TENANT_EXPR = "nullif(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("model", sa.String(100), nullable=False, server_default=""),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "units", sa.Numeric(12, 3), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "cost", sa.Numeric(12, 6), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="SET NULL"
        ),
    )

    op.create_index(
        "ix_llm_usage_logs_tenant_id_created_at", TABLE, ["tenant_id", "created_at"]
    )
    op.create_index(
        "ix_llm_usage_logs_tenant_id_kind_model", TABLE, ["tenant_id", "kind", "model"]
    )
    op.create_index(
        "ix_llm_usage_logs_tenant_id_conversation_id",
        TABLE,
        ["tenant_id", "conversation_id"],
    )

    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO {APP_ROLE}")

    # 按租户隔离：与其余租户表一致，缺上下文即查不到任何行（fail-closed）
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {TABLE}_isolation ON {TABLE}
        USING (tenant_id = {TENANT_EXPR})
        WITH CHECK (tenant_id = {TENANT_EXPR})
        """
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS {TABLE}_isolation ON {TABLE}")
    op.drop_table(TABLE)
