"""M7: users.username —— 邮箱与用户名都能登录

背景：原实现以邮箱为唯一登录标识（设计文档 6）。运营要求坐席可用短用户名登录，
且不能影响存量账号，因此：

1. **可空**：存量账号保持 NULL，行为完全不变（仍只能用邮箱登录）
2. **部分唯一索引**：只约束非 NULL 值。PG 的 UNIQUE 本就忽略 NULL，
   显式写 WHERE 是为了让"空值不占坑"这个意图可读
3. **小写归一 CHECK**：避免 `ZhangSan` 与 `zhangsan` 被当成两个账号。
   服务层负责 lower()，约束只做兜底——没有约束时一次漏改就会产生重复身份
4. **auth_lookup_user_by_name**：SECURITY DEFINER 绕过 users 的 RLS，
   与 0001 的 auth_lookup_user 同构。**不过滤 status**：注册时用它判断
   "名字是否被占用"，被禁用账号仍然占着这个名字；若这里过滤了，
   唯一索引会在 INSERT 时才报错，用户看到的是 500 而不是"用户名已被占用"

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "aics_app"


def upgrade() -> None:
    op.add_column("users", sa.Column("username", sa.String(32), nullable=True))

    # 存储一律小写：登录查询 lower(输入) 即可命中，无需额外索引函数
    op.create_check_constraint(
        "ck_users_username_lowercase",
        "users",
        "username IS NULL OR username = lower(username)",
    )
    op.create_index(
        "uq_users_username",
        "users",
        ["username"],
        unique=True,
        postgresql_where=sa.text("username IS NOT NULL"),
    )

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


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS auth_lookup_user_by_name(text)")
    op.drop_index("uq_users_username", table_name="users")
    op.drop_constraint("ck_users_username_lowercase", "users", type_="check")
    op.drop_column("users", "username")
