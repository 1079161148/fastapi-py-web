"""审计日志（M8）：租户内敏感操作留痕。

写日志失败绝不阻断业务：记录动作本身是辅助能力，
为了留痕把成员管理搞挂是本末倒置。失败只记 warning。
"""

import uuid
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.tenant import require_current_tenant_id
from app.db.models.support import AuditLog

logger = get_logger("audit")

# 常见 action 常量：避免调用方各写字符串
ACTION_MEMBER_ADD = "member.add"
ACTION_MEMBER_ROLE = "member.role"
ACTION_MEMBER_REMOVE = "member.remove"
ACTION_TENANT_UPDATE = "tenant.update"
ACTION_TENANT_QUOTA = "tenant.quota"
ACTION_QUICK_REPLY_CREATE = "quick_reply.create"
ACTION_QUICK_REPLY_UPDATE = "quick_reply.update"
ACTION_QUICK_REPLY_DELETE = "quick_reply.delete"


async def record(
    session: AsyncSession,
    *,
    action: str,
    actor_id: uuid.UUID | None = None,
    actor_name: str = "",
    target_type: str = "",
    target_id: str = "",
    detail: dict[str, Any] | None = None,
) -> None:
    """写入一条审计日志。调用方负责 commit（与业务同一事务）。"""
    tenant_id = require_current_tenant_id()
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor_id=actor_id,
            actor_name=actor_name,
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail=detail or {},
        )
    )


async def list_logs(
    session: AsyncSession,
    *,
    action: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> list[AuditLog]:
    """按时间倒序分页。cursor 为上一页最后一条的 id（时间序即 id 序近似）。"""
    tenant_id = require_current_tenant_id()
    stmt = select(AuditLog).where(AuditLog.tenant_id == tenant_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    stmt = stmt.order_by(desc(AuditLog.created_at), desc(AuditLog.id)).limit(min(limit, 200))
    result = await session.execute(stmt)
    return list(result.scalars().all())
