"""租户上下文（设计文档 9.2）。

解析顺序：X-Tenant-Id 请求头 → 子域名（生产）。
上下文只从请求入口注入一次，service 层通过依赖读取，
禁止从业务参数取 tenant_id（见设计文档 4.2 分层规则）。
"""

import uuid
from contextvars import ContextVar

from app.core.exceptions import AppError

current_tenant_id: ContextVar[uuid.UUID | None] = ContextVar("current_tenant_id", default=None)


def set_current_tenant(tenant_id: uuid.UUID | None) -> None:
    current_tenant_id.set(tenant_id)


def get_current_tenant_id() -> uuid.UUID | None:
    return current_tenant_id.get()


def require_current_tenant_id() -> uuid.UUID:
    """取当前租户，缺失即失败。

    鉴权中间件之后所有业务接口都必须有租户上下文，
    缺失说明路由漏挂中间件或直接调用了 service——属于编程错误，快速失败。
    """
    tenant_id = current_tenant_id.get()
    if tenant_id is None:
        raise AppError(
            code="TENANT_CONTEXT_MISSING",
            message="缺少租户上下文：请通过 X-Tenant-Id 请求头指定租户",
            status_code=400,
        )
    return tenant_id


def parse_tenant_id(raw: str | None) -> uuid.UUID | None:
    """解析外部传入的租户标识；非法值直接拒绝，不静默忽略。"""
    if raw is None or raw == "":
        return None
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise AppError(
            code="INVALID_TENANT_ID",
            message="X-Tenant-Id 不是合法的 UUID",
            status_code=400,
        ) from exc
