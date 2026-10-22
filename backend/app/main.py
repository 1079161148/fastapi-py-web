"""FastAPI 应用装配入口。

分层规则见设计文档 4.2：api 只做校验与编排，业务逻辑在 services。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.middleware import RequestContextMiddleware
from app.api.v1 import (
    agent,
    audit_logs,
    auth,
    channels,
    conversations,
    customers,
    guest,
    health,
    insights,
    knowledge,
    messages,
    quality,
    quick_replies,
    stats,
    tenants,
    usage,
    voice,
)
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.rate_limit import RateLimitMiddleware
from app.workers.pool import close_pool

settings = get_settings()
logger = get_logger("app")

# Vite 开发服务器默认源；生产由 Nginx 同源代理，不需要 CORS
DEV_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    configure_logging(settings.log_level)
    logger.info("app_started", env=settings.app_env, app=settings.app_name)
    yield
    # 队列连接是懒创建的；没用到时 close_pool 直接返回
    await close_pool()
    logger.info("app_stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if not settings.is_production else None,
    )
    # 中间件顺序：后添加的先执行。RequestContext 先于 CORS 执行，
    # 保证 request_id 与租户上下文覆盖到 CORS 预检之外的完整链路。
    # 限流放在 CORS 内侧：429 也要带 CORS 头，否则浏览器只报网络错误，
    # 前端无法把"请求过于频繁"提示给用户。
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=DEV_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    api_v1 = APIRouter(prefix="/api/v1")
    api_v1.include_router(health.router)
    api_v1.include_router(auth.router)
    api_v1.include_router(tenants.router)
    api_v1.include_router(usage.router)
    api_v1.include_router(guest.router)
    api_v1.include_router(conversations.router)
    api_v1.include_router(knowledge.router)
    api_v1.include_router(channels.router)
    api_v1.include_router(messages.router)
    api_v1.include_router(voice.router)
    api_v1.include_router(agent.router)
    api_v1.include_router(quick_replies.router)
    api_v1.include_router(stats.router)
    api_v1.include_router(customers.router)
    api_v1.include_router(audit_logs.router)
    api_v1.include_router(insights.router)
    api_v1.include_router(quality.router)
    app.include_router(api_v1)
    return app


app = create_app()
