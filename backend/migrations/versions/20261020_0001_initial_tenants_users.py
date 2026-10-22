"""initial: tenants / tenant_members / users / refresh_tokens + RLS

建立租户隔离的数据库地基（设计文档 M1 / 9.2）：

1. pgvector 扩展（M3 知识库检索依赖）
2. 应用角色 aics_app：非超管、无 BYPASSRLS——超管连接才会绕过 RLS，
   所以应用运行时必须用这个角色连库
3. 四张表 + 索引
4. RLS：所有表 FORCE ROW LEVEL SECURITY，未设置 app.tenant_id 时
   查询返回空行（fail-closed），从数据库层兜底租户隔离
5. auth_lookup_user：SECURITY DEFINER 函数，供登录引导查询使用
   （否则"未设租户上下文就查不到用户"与"放开 users 策略"二选一，都会破坏隔离）

Revision ID: 0001
Revises:
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 应用角色名与口令：口令经 APP_DB_PASSWORD 注入，缺省值仅供本地开发
APP_ROLE = "aics_app"
TENANT_TABLES = ("tenants", "tenant_members", "users", "refresh_tokens")


def upgrade() -> None:
    # 1. pgvector：M3 检索依赖，提前安装避免后续迁移改动线上扩展
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # 2. 应用角色：登录 + 有限权限；不存在则创建（幂等）
    import os

    role_password = os.environ.get("APP_DB_PASSWORD", "aics_app")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{role_password}' NOBYPASSRLS;
            ELSE
                ALTER ROLE {APP_ROLE} NOBYPASSRLS;
            END IF;
        END
        $$;
        """
    )

    # 3. 表结构
    op.create_table(
        "tenants",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("slug", sa.String(63), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("quota", sa.dialects.postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("settings", sa.dialects.postgresql.JSONB(), nullable=False, server_default="{}"),
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
    )
    op.create_index("uq_tenants_slug", "tenants", ["slug"], unique=True)

    op.create_table(
        "users",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False, server_default=""),
        sa.Column("role", sa.String(20), nullable=False, server_default="customer"),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="RESTRICT"),
    )
    op.create_index("uq_users_email", "users", ["email"], unique=True)

    op.create_table(
        "tenant_members",
        sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default="customer"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("tenant_id", "user_id"),
    )

    op.create_table(
        "refresh_tokens",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("jti", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )
    op.create_index("uq_refresh_tokens_jti", "refresh_tokens", ["jti"], unique=True)

    # 4. 应用角色授权（仅 DML，不授予 DDL）
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}")

    # 5. RLS：FORCE 让策略对表属主同样生效；未设上下文时按 NULL 比较，行全部不可见
    rls_statements = {
        "tenants": "id",
        "tenant_members": "tenant_id",
        "users": "tenant_id",
        "refresh_tokens": "tenant_id",
    }
    for table, column in rls_statements.items():
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS {table}_isolation ON {table}")
        # tenants 放开 INSERT 检查：注册流程在无上下文事务里创建首个租户
        if table == "tenants":
            op.execute(
                f"""
                CREATE POLICY {table}_isolation ON {table}
                USING ({column} = current_setting('app.tenant_id', true)::uuid)
                WITH CHECK (true)
                """
            )
        else:
            op.execute(
                f"""
                CREATE POLICY {table}_isolation ON {table}
                USING ({column} = current_setting('app.tenant_id', true)::uuid)
                WITH CHECK ({column} = current_setting('app.tenant_id', true)::uuid)
                """
            )

    # 6. 登录引导查询：SECURITY DEFINER 以属主身份执行，绕过 RLS；
    #    只暴露按邮箱查用户这一种能力，不放开整表
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auth_lookup_user(p_email text)
        RETURNS SETOF users
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT * FROM users WHERE email = p_email AND status = 'active' LIMIT 1
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION auth_lookup_user(text) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION auth_lookup_user(text) TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS auth_lookup_user(text)")
    for table in TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_isolation ON {table}")
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("DROP EXTENSION IF EXISTS vector")
