"""guest 会话 TTL 清理（设计文档 5.5 / M6）。

两步，故意分开：

1. **标记过期**：超过 `expires_at` 的 active 访客会话 → expired。
   不删数据——坐席还能看到"这个访客后来登录了没有"，运营也能统计转化率。
2. **清除**：过期超过保留期（默认 90 天）的访客会话，连同消息一起删除，
   防止 conversations / messages 无限膨胀。

为什么用超管连接（MIGRATION_DATABASE_URL）：
跨租户扫描"所有租户的过期访客"在 RLS 下做不到——没有租户上下文时一行都查不到。
这是本任务**唯一**使用超管连接的地方，且只做 UPDATE/DELETE，不读取、不返回
任何业务内容；API 进程从不使用该连接（见 config 的注释与设计文档 9.2）。
"""

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("worker.cleanup")

# 过期访客会话的状态（与 conversations.guest_sessions.status 保持一致）
GUEST_STATUS_ACTIVE = "active"
GUEST_STATUS_EXPIRED = "expired"


async def cleanup_guest_sessions(ctx: dict[str, Any]) -> None:
    """每日清理：先过期，再按保留期物理删除。两步都在同一个连接上完成。"""
    settings = get_settings()
    if not settings.migration_database_url.strip():
        logger.warning("guest_cleanup_skipped", reason="MIGRATION_DATABASE_URL 未配置")
        return

    engine = create_async_engine(settings.migration_database_url, pool_pre_ping=True)
    try:
        async with engine.begin() as conn:
            expired = await conn.execute(
                text(
                    """
                    UPDATE guest_sessions
                       SET status = :expired, updated_at = now()
                     WHERE status = :active
                       AND expires_at <= now()
                    """
                ),
                {"expired": GUEST_STATUS_EXPIRED, "active": GUEST_STATUS_ACTIVE},
            )
            expired_count = expired.rowcount or 0

            # 会话 + 消息：messages 通过外键 CASCADE 跟着删，这里只需删会话行
            purged = await conn.execute(
                text(
                    """
                    DELETE FROM conversations
                     WHERE owner_type = 'guest'
                       AND guest_id IN (
                           SELECT guest_id
                             FROM guest_sessions
                            WHERE status = :expired
                              AND expires_at <= now() - (:days || ' days')::interval
                       )
                    """
                ),
                {
                    "expired": GUEST_STATUS_EXPIRED,
                    "days": str(settings.guest_cleanup_retention_days),
                },
            )
            purged_conversations = purged.rowcount or 0

            removed = await conn.execute(
                text(
                    """
                    DELETE FROM guest_sessions
                     WHERE status = :expired
                       AND expires_at <= now() - (:days || ' days')::interval
                    """
                ),
                {
                    "expired": GUEST_STATUS_EXPIRED,
                    "days": str(settings.guest_cleanup_retention_days),
                },
            )
            removed_sessions = removed.rowcount or 0
    except Exception:  # noqa: BLE001 - 定时任务失败必须留痕，arq 会记录并重试
        logger.exception("guest_cleanup_failed")
        raise
    finally:
        await engine.dispose()

    logger.info(
        "guest_cleanup_done",
        expired=expired_count,
        purged_conversations=purged_conversations,
        removed_sessions=removed_sessions,
        retention_days=settings.guest_cleanup_retention_days,
    )


__all__ = ["cleanup_guest_sessions"]
