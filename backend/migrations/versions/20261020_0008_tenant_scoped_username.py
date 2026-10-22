"""M7: 用户名改为租户内唯一

0007 把 username 做成了全局唯一。业务要求是**租户内唯一**——不同租户可以有
同名的"张三"，这是多租户系统里更合理的边界（全局唯一等于让 A 公司抢注了
B 公司员工想用的名字）。

由此产生一个必须一起解决的推论：
**登录时只有"用户名 + 密码"是定位不到人的**，同名用户可能分布在多个租户。
因此登录入口改为：

- 邮箱（全局唯一）→ 无需租户标识，仍然一条查询定位
- 用户名 → 必须同时提供租户 slug，走 (slug, username) 定位

实现上：
1. 唯一约束从 `username` 改为 `(tenant_id, username)`
2. `auth_lookup_user_by_name(p_tenant_slug, p_username)` 改为两参数，
   返回该租户下的用户。租户不存在与用户不存在都返回空行，调用方统一报
   401，顺带避免"租户是否存在"的枚举面

**升级注意**：0007 期间若已产生跨租户同名数据，建新唯一索引会失败。
本迁移先不做数据修复——当前只有单租户演示数据，出现失败说明环境已被
写入多租户同名数据，需要人工确认保留哪一条。

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "aics_app"


def upgrade() -> None:
    op.drop_index("uq_users_username", table_name="users")
    op.create_index(
        "uq_users_tenant_username",
        "users",
        ["tenant_id", "username"],
        unique=True,
        postgresql_where=sa.text("username IS NOT NULL"),
    )

    # 函数签名变了（多一个参数），CREATE OR REPLACE 不能改参数列表，必须先删
    op.execute("DROP FUNCTION IF EXISTS auth_lookup_user_by_name(text)")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auth_lookup_user_by_name(
            p_tenant_slug text, p_username text
        )
        RETURNS SETOF users
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT u.*
              FROM users u
              JOIN tenants t ON t.id = u.tenant_id
             WHERE t.slug = lower(p_tenant_slug)
               AND u.username = lower(p_username)
             LIMIT 1
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION auth_lookup_user_by_name(text, text) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION auth_lookup_user_by_name(text, text) TO {APP_ROLE}")


def downgrade() -> None:
    """回退到全局唯一。

    若已存在跨租户同名用户名，这一步会失败——这是数据层面的真实冲突，
    必须人工决定改哪个名字，不做静默改名。
    """
    op.execute("DROP FUNCTION IF EXISTS auth_lookup_user_by_name(text, text)")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auth_lookup_user_by_name(p_username text)
        RETURNS SETOF users
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT * FROM users WHERE username = lower(p_username) LIMIT 1
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION auth_lookup_user_by_name(text) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION auth_lookup_user_by_name(text) TO {APP_ROLE}")

    op.drop_index("uq_users_tenant_username", table_name="users")
    op.create_index(
        "uq_users_username",
        "users",
        ["username"],
        unique=True,
        postgresql_where=sa.text("username IS NOT NULL"),
    )
