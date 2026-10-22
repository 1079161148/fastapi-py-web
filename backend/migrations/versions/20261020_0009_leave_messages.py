"""M8: leave_messages 离线留言

设计文档 5.3 的兜底路径：坐席不在线 / 用户不想排队时，留下联系方式与问题。

与 handoff_tickets 的区别：
- handoff_tickets 是"现在就要人接"，有排队与认领语义
- leave_messages 是"稍后回你"，只需要联系人与待办状态

status: new（待处理）/ handled（已处理）。处理人置空而不是删行，
保留"谁在什么时候处理过"的记录。

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "aics_app"
TABLE = "leave_messages"
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
        sa.Column("owner_type", sa.String(10), nullable=False, server_default="guest"),
        sa.Column("name", sa.String(100), nullable=False, server_default=""),
        sa.Column("contact", sa.String(200), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="new"),
        sa.Column("handled_by", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True),
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
        # 会话删除后留言保留（联系人信息本身有价值）
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["handled_by"], ["users.id"], ondelete="SET NULL"),
    )

    op.create_index(
        "ix_leave_messages_tenant_id_status_created_at",
        TABLE,
        ["tenant_id", "status", "created_at"],
    )
    op.create_index(
        "ix_leave_messages_tenant_id_conversation_id", TABLE, ["tenant_id", "conversation_id"]
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
