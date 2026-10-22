"""M4: channel_configs / channel_messages / channel_identities + 入站租户反查函数

设计文档 9.1 / 6：

1. channel_configs：每租户每渠道的开关与外部账号标识（唯一 (tenant_id, channel)）
2. channel_messages：入/出站流水，唯一键 (tenant_id, channel, external_msg_id)
   做幂等去重——各平台都会重推回调，不去重必然重复回复
3. channel_identities：外部用户（微信 openid / 钉钉 userid / 邮箱）到内部
   guest_sessions 的映射，使渠道会话复用既有的 owner 模型
4. 三张表启用 FORCE RLS，策略与 0002/0003 一致

两个 SECURITY DEFINER 函数是**唯一受控越权点**：
- channel_tenant：webhook 到达时还没有租户上下文（RLS 下查不到任何行），
  必须先按 (channel, external_account_id) 反查租户才能 SET LOCAL。只返回 tenant_id。
- channel_accounts：邮件轮询任务需要列出所有启用 email 渠道的租户，同样无上下文。
  只返回 (tenant_id, external_account_id)，不暴露凭证。
两者都锁 search_path，且只授予 aics_app EXECUTE。

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "aics_app"
TENANT_TABLES = ("channel_configs", "channel_messages", "channel_identities")

# 全部租户表及其隔离列。本迁移统一重建策略，同时修掉一个真实缺陷：
# `current_setting('app.tenant_id', true)` 在 SET LOCAL 回滚后返回**空字符串**
# 而不是 NULL，此时 `''::uuid` 抛 invalid input syntax，表现为"偶发 500"。
# nullif 把空串归零为 NULL，策略退化为"全部不可见"（fail-closed）。
RLS_COLUMNS = {
    "tenants": "id",
    "tenant_members": "tenant_id",
    "users": "tenant_id",
    "refresh_tokens": "tenant_id",
    "guest_sessions": "tenant_id",
    "conversations": "tenant_id",
    "messages": "tenant_id",
    "knowledge_bases": "tenant_id",
    "documents": "tenant_id",
    "chunks": "tenant_id",
    "channel_configs": "tenant_id",
    "channel_messages": "tenant_id",
    "channel_identities": "tenant_id",
}

TENANT_EXPR = "nullif(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "channel_configs",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        # 平台侧账号标识：微信填公众号原始 ID（gh_xxx）、钉钉填 robotCode/corpId、
        # 邮件填收件邮箱。入站靠它反查租户，因此同一渠道内必须唯一
        sa.Column(
            "external_account_id", sa.String(200), nullable=False, server_default=""
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        # 只放非敏感配置（AppID、邮箱地址等）；AppSecret / 密码等仍走环境变量
        sa.Column("credentials", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        # 出站每分钟上限（钉钉等平台有主动消息频次限制）
        sa.Column("rate_limit", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("webhook_secret", sa.String(200), nullable=False, server_default=""),
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
        sa.UniqueConstraint("tenant_id", "channel", name="uq_channel_configs_tenant_id_channel"),
    )
    # 反查索引：webhook 无租户上下文，靠 (channel, external_account_id) 找租户。
    # 用部分唯一索引跳过空值：未配置外部账号的渠道行（如 web）不应互相冲突
    op.create_index(
        "uq_channel_configs_channel_external_account_id",
        "channel_configs",
        ["channel", "external_account_id"],
        unique=True,
        postgresql_where=sa.text("external_account_id <> ''"),
    )

    op.create_table(
        "channel_messages",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("external_msg_id", sa.String(255), nullable=False),
        sa.Column("conversation_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("direction", sa.String(10), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="received"),
        sa.Column("external_user_id", sa.String(255), nullable=False, server_default=""),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        # 出站上下文（钉钉 sessionWebhook、邮件主题）：跨进程投递必须持久化
        sa.Column(
            "reply_context", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("error_msg", sa.String(500), nullable=False, server_default=""),
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
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        # 幂等键：平台重推同一条消息时撞唯一键 → 直接跳过，不重复生成回复
        sa.UniqueConstraint(
            "tenant_id",
            "channel",
            "external_msg_id",
            name="uq_channel_messages_tenant_id_channel_external_msg_id",
        ),
    )
    op.create_index(
        "ix_channel_messages_tenant_id_conversation_id",
        "channel_messages",
        ["tenant_id", "conversation_id"],
    )
    op.create_index(
        "ix_channel_messages_tenant_id_status", "channel_messages", ["tenant_id", "status"]
    )

    op.create_table(
        "channel_identities",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("external_user_id", sa.String(255), nullable=False),
        sa.Column("guest_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("display_name", sa.String(200), nullable=False, server_default=""),
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
        sa.ForeignKeyConstraint(
            ["guest_id"], ["guest_sessions.guest_id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "channel",
            "external_user_id",
            name="uq_channel_identities_tenant_id_channel_external_user_id",
        ),
    )

    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}")

    for table, column in RLS_COLUMNS.items():
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS {table}_isolation ON {table}")
        # tenants 的注册流程在无租户上下文的事务里创建首个租户，必须放开 INSERT
        # 检查（与 0001 保持一致）；其余表读写都要求租户上下文匹配
        with_check = "true" if table == "tenants" else f"{column} = {TENANT_EXPR}"
        op.execute(
            f"""
            CREATE POLICY {table}_isolation ON {table}
            USING ({column} = {TENANT_EXPR})
            WITH CHECK ({with_check})
            """
        )

    # 入站反查：只返回 tenant_id，不泄露配置与凭证
    op.execute(
        """
        CREATE OR REPLACE FUNCTION channel_tenant(
            p_channel text, p_external_account_id text
        ) RETURNS uuid
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT tenant_id
            FROM channel_configs
            WHERE channel = p_channel
              AND external_account_id = p_external_account_id
              AND external_account_id <> ''
              AND enabled
            LIMIT 1
        $$
        """
    )
    # 邮件轮询：列出某渠道所有启用的 (租户, 外部账号)
    op.execute(
        """
        CREATE OR REPLACE FUNCTION channel_accounts(p_channel text)
        RETURNS TABLE (tenant_id uuid, external_account_id text)
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT c.tenant_id, c.external_account_id
            FROM channel_configs c
            WHERE c.channel = p_channel
              AND c.external_account_id <> ''
              AND c.enabled
            ORDER BY c.created_at
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION channel_tenant(text, text) FROM PUBLIC"
    )
    op.execute("REVOKE ALL ON FUNCTION channel_accounts(text) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION channel_tenant(text, text) TO {APP_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION channel_accounts(text) TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS channel_accounts(text)")
    op.execute("DROP FUNCTION IF EXISTS channel_tenant(text, text)")
    for table in reversed(TENANT_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {table}_isolation ON {table}")
    op.execute("DROP INDEX IF EXISTS uq_channel_configs_channel_external_account_id")
    for table in reversed(TENANT_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
