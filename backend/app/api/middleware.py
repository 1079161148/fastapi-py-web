"""请求中间件：request_id 透传 + 租户上下文注入（设计文档 8/9.2）。

租户解析：X-Tenant-Id 请求头（本地/内部调用）→ 子域名（生产，预留）。
上下文在中间件里设置一次，service 层只读不写；
请求结束（含异常）必须清理，否则同一任务内的后续请求会读到残留租户。
"""

import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.tenant import parse_tenant_id, set_current_tenant

logger = structlog.get_logger("http")


def _reset_request_context() -> None:
    set_current_tenant(None)
    structlog.contextvars.clear_contextvars()


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
        structlog.contextvars.bind_contextvars(request_id=request_id)

        tenant_id = parse_tenant_id(request.headers.get("X-Tenant-Id"))
        set_current_tenant(tenant_id)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # 异常也记录并清理：500 的日志比成功的更有诊断价值
            logger.error(
                "http_request_failed",
                method=request.method,
                path=request.url.path,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
            _reset_request_context()
            raise

        logger.info(
            "http_request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            tenant_id=str(tenant_id) if tenant_id else None,
        )
        response.headers["X-Request-Id"] = request_id
        _reset_request_context()
        return response
