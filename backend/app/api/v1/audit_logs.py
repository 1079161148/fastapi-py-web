"""审计日志路由（M8）。

只读：日志由各写操作接口在 service 层落库，这里不提供任何写入口
（能通过 API 改日志的审计没有意义）。
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import require_tenant_admin
from app.db.models.tenant import TenantMember
from app.db.session import get_session
from app.schemas.audit_log import AuditLogInfo
from app.services import audit as audit_service

router = APIRouter(prefix="/audit-logs", tags=["audit-logs"])


@router.get("", response_model=list[AuditLogInfo])
async def list_logs(
    action: str | None = Query(default=None, description="按 action 过滤，如 member.role"),
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    _admin: TenantMember = Depends(require_tenant_admin),
) -> list[AuditLogInfo]:
    logs = await audit_service.list_logs(session, action=action, limit=limit)
    return [
        AuditLogInfo(
            id=log.id,
            actor_id=log.actor_id,
            actor_name=log.actor_name,
            action=log.action,
            target_type=log.target_type,
            target_id=log.target_id,
            detail=log.detail,
            created_at=log.created_at.isoformat(),
        )
        for log in logs
    ]
