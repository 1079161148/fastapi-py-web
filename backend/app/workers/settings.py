"""arq worker 配置。
启动：`uv run arq app.workers.settings.WorkerSettings`
（或 backend 目录下 `.venv\\Scripts\\arq.exe app.workers.settings.WorkerSettings`）

worker 与 API 共用 DATABASE_URL（aics_app 角色，受 RLS 约束）：
任务里必须先 set_current_tenant 再访问业务表，否则 fail-closed 查不到任何行。

任务清单：
- process_document        知识库文档解析 / 分块 / 向量化（M3）
- process_channel_inbound 渠道入站生成回复并投递（M4）
- poll_email_channels     邮件 IMAP 轮询（M4，cron）

邮件轮询用 cron 而非队列链：它是周期性拉取，没有外部触发方。
间隔取 30s 或 60s（EMAIL_POLL_SECONDS），低于 30s 容易被邮件服务商判为异常。
"""

from typing import Any

from arq import cron
from arq.connections import RedisSettings

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import engine
from app.workers.channel_tasks import poll_email_channels, process_channel_inbound
from app.workers.cleanup_tasks import cleanup_guest_sessions
from app.workers.tasks import process_document

settings = get_settings()
logger = get_logger("worker")

# 30s 以下不提供：邮件服务商的异常流量判定阈值通常就在这个量级
_POLL_SECONDS = {0} if settings.email_poll_seconds >= 60 else {0, 30}


async def startup(ctx: dict[str, Any]) -> None:
    configure_logging(settings.log_level)
    logger.info(
        "worker_started",
        jobs=[
            "process_document",
            "process_channel_inbound",
            "poll_email_channels",
            "cleanup_guest_sessions",
        ],
    )


async def shutdown(ctx: dict[str, Any]) -> None:
    await engine.dispose()
    logger.info("worker_stopped")


class WorkerSettings:
    functions = [process_document, process_channel_inbound]
    cron_jobs = [
        cron(poll_email_channels, second=_POLL_SECONDS, unique=True),
        # 每日 03:00 清理过期访客会话（设计文档 5.5）：低频、无需实时性，
        # 挑业务低峰跑，避免与消息写入抢锁
        cron(cleanup_guest_sessions, hour=3, minute=0, unique=True),
    ]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    # 单进程并发 4：解析/向量化/对话都是 IO 密集，过高会同时压满外部模型配额
    max_jobs = 4
    # 单份大文档的分块+embedding 可能到分钟级；超时即失败并回写 error_msg
    job_timeout = 900
    keep_result = 3600
    on_startup = startup
    on_shutdown = shutdown
