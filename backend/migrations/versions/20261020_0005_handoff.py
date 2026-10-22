"""M6: handoff_tickets 转人工工单

设计文档 5.3 / 6：

一张工单记录"某段对话被转给真人"这件事：谁发起、为什么、谁认领、何时结束。
与 conversations.status 的分工：

- conversations.status 回答"现在谁在回"（ai / pending_handoff / human / closed）
- handoff_tickets 回答"这一单谁在处理"（agent_id / reason / claimed_at / resolved_at）

部分唯一索引 `uq_handoff_tickets_open_per_conversation` 保证同一会话只有一张
未结工单：用户连点两次"转人工"、渠道重复推送都能被数据库挡掉，幂等不靠应用层。

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "aics_app"
TABLE = "handoff_tickets"
# 与 0004 的写法一致：SET LOCAL 回滚后 current_setting 会返回空串，
# 必须 nullif 成 NULL，否则 ''::uuid 直接抛 invalid input syntax
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
        sa.Column("conversation_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("reason", sa.String(20), nullable=False, server_default="user_request"),
        sa.Column("requested_by", sa.String(20), nullable=False, server_default="user"),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("summary", sa.String(200), nullable=False, server_default=""),
        # 扩展位：触发关键词、AI 标记原文等。列名不用 metadata，避开 Base.metadata
        sa.Column("extra", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
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
        # 坐席离职删号时工单保留，agent_id 置空（审计需要"谁接过"）
        sa.ForeignKeyConstraint(["agent_id"], ["users.id"], ondelete="SET NULL"),
    )

    # 坐席工作台的主查询：按租户 + 状态 + 时间倒序
    op.create_index(
        "ix_handoff_tickets_tenant_id_status_created_at",
        TABLE,
        ["tenant_id", "status", "created_at"],
    )
    op.create_index(
        "ix_handoff_tickets_tenant_id_conversation_id", TABLE, ["tenant_id", "conversation_id"]
    )
    op.create_index("ix_handoff_tickets_tenant_id_agent_id", TABLE, ["tenant_id", "agent_id"])
    op.create_index(
        "uq_handoff_tickets_open_per_conversation",
        TABLE,
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'claimed')"),
    )

    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO {APP_ROLE}")

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
