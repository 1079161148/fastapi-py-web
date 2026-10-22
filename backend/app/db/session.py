"""数据库引擎与会话。

M1 起在此基础上追加租户上下文（SET LOCAL app.tenant_id）与 RLS 策略，
此处只保留最小可用骨架。
"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings
from app.db.rls import register_rls_hooks

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,  # 取出前探测连接，规避 Docker/网络导致的 stale connection
    pool_size=10,
    max_overflow=20,
)

# RLS：事务开启时注入 SET LOCAL app.tenant_id（设计文档 9.2）
register_rls_hooks(engine)

async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：请求级会话，结束时自动关闭。"""
    async with async_session_factory() as session:
        yield session
