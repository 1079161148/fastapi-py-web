"""arq 连接池（应用侧入队）。

懒创建 + 单例：Redis 未启动不应该拖垮 API 启动（健康检查仍需可用），
只有真正要入队时才报 QUEUE_UNAVAILABLE，并给出可操作提示。
"""

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.core.config import get_settings
from app.core.exceptions import AppError

_pool: ArqRedis | None = None


async def get_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        settings = get_settings()
        try:
            _pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        except Exception as exc:  # redis 连接失败抛多种底层异常
            raise AppError(
                code="QUEUE_UNAVAILABLE",
                message=(
                    "任务队列不可用：请确认 Redis 已启动（docker compose up -d redis），"
                    "并已启动 worker（arq app.workers.settings.WorkerSettings）"
                ),
                status_code=503,
                expose=True,
            ) from exc
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None
