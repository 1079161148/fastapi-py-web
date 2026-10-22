"""M2: guest_sessions / conversations / messages + RLS

设计文档 5.5 / 6 / 9.2：

1. 三张表均为租户级表，全部启用 FORCE ROW LEVEL SECURITY，
   策略与 0001 一致：tenant_id = current_setting('app.tenant_id', true)::uuid
   未设置上下文时返回空行（fail-closed）
2. 索引一律以 tenant_id 打头（RLS 会给每条查询附加租户条件）
3. 授予应用角色 aics_app 的 DML 权限，并为后续迁移新建的表设置默认权限
4. conversations 的 owner 互斥约束由数据库保证：
   user 与 guest 二选一，合并后 guest_id 必须置空

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "aics_app"
TENANT_TABLES = ("guest_sessions", "conversations", "messages")


def upgrade() -> None:
    op.create_table(
        "guest_sessions",
        sa.Column(
            "guest_id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("merged_user_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.ForeignKeyConstraint(["merged_user_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_guest_sessions_tenant_id_expires_at", "guest_sessions", ["tenant_id", "expires_at"]
    )

    op.create_table(
        "conversations",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False, server_default="web"),
        sa.Column("owner_type", sa.String(10), nullable=False),
        sa.Column("user_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("guest_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="ai"),
        sa.Column("title", sa.String(200), nullable=False, server_default=""),
        sa.Column(
            "voice_reply_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["guest_id"], ["guest_sessions.guest_id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "(owner_type = 'user' AND user_id IS NOT NULL AND guest_id IS NULL)"
            " OR (owner_type = 'guest' AND guest_id IS NOT NULL AND user_id IS NULL)",
            name="ck_conversations_owner_consistency",
        ),
    )
    op.create_index(
        "ix_conversations_tenant_id_updated_at", "conversations", ["tenant_id", "updated_at"]
    )
    op.create_index("ix_conversations_tenant_id_user_id", "conversations", ["tenant_id", "user_id"])
    op.create_index(
        "ix_conversations_tenant_id_guest_id", "conversations", ["tenant_id", "guest_id"]
    )

    op.create_table(
        "messages",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("message_type", sa.String(20), nullable=False, server_default="text"),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("source", sa.String(20), nullable=False, server_default="web"),
        sa.Column(
            "attachments",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "sources",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
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
    )
    op.create_index(
        "ix_messages_tenant_id_conversation_id_created_at",
        "messages",
        ["tenant_id", "conversation_id", "created_at"],
    )

    # 应用角色授权：0001 只对当时存在的表授权，这里必须补；同时设置默认权限，
    # 避免后续每加一张表就漏授权（漏了表现为运行时报 permission denied）
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
    )

    # RLS：FORCE 让策略对表属主同样生效
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
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {APP_ROLE}"
    )
