"""Alembic 环境（异步）。

连接 URL 用 MIGRATION_DATABASE_URL（超管，用于执行 DDL 与 RLS 策略），
应用运行时用 DATABASE_URL（aics_app 角色，受 RLS 约束）——两者分离是
设计文档第 8 章"超管操作走独立数据库角色"的落地方式。
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

# 导入所有模型，确保 autogenerate 能感知全部表
from app.db import models as _models  # noqa: F401
from app.db.base import Base

config = context.config
settings = get_settings()

# 用环境变量覆盖 ini 中的占位 URL
config.set_main_option("sqlalchemy.url", settings.migration_database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.migration_database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
