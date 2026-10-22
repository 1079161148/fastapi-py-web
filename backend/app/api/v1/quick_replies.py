"""快捷回复（坐席常用话术）路由。

读与用对所有坐席开放；增删改记审计日志（谁改了什么话术库要能追溯）。
"""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, require_agent
from app.db.models.tenant import TenantMember
from app.db.models.user import User
from app.db.session import get_session
from app.schemas.quick_reply import QuickReplyCreate, QuickReplyInfo, QuickReplyUpdate
from app.services import audit as audit_service
from app.services import quick_replies as quick_reply_service

router = APIRouter(prefix="/quick-replies", tags=["quick-replies"])


@router.get("", response_model=list[QuickReplyInfo])
async def list_replies(
    category: str | None = None,
    session: AsyncSession = Depends(get_session),
    _agent: TenantMember = Depends(require_agent),
) -> list[QuickReplyInfo]:
    replies = await quick_reply_service.list_replies(session, category=category)
    return [
        QuickReplyInfo(
            id=reply.id,
            title=reply.title,
            content=reply.content,
            category=reply.category,
            sort_order=reply.sort_order,
            created_at=reply.created_at.isoformat(),
        )
        for reply in replies
    ]


@router.post("", response_model=QuickReplyInfo, status_code=201)
async def create_reply(
    payload: QuickReplyCreate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    _agent: TenantMember = Depends(require_agent),
) -> QuickReplyInfo:
    reply = await quick_reply_service.create_reply(
        session,
        title=payload.title,
        content=payload.content,
        category=payload.category,
        sort_order=payload.sort_order,
        created_by=current_user.id,
    )
    await audit_service.record(
        session,
        action=audit_service.ACTION_QUICK_REPLY_CREATE,
        actor_id=current_user.id,
        actor_name=current_user.display_name,
        target_type="quick_reply",
        target_id=str(reply.id),
        detail={"title": reply.title, "category": reply.category},
    )
    await session.commit()
    return QuickReplyInfo(
        id=reply.id,
        title=reply.title,
        content=reply.content,
        category=reply.category,
        sort_order=reply.sort_order,
        created_at=reply.created_at.isoformat(),
    )


@router.patch("/{reply_id}", response_model=QuickReplyInfo)
async def update_reply(
    reply_id: uuid.UUID,
    payload: QuickReplyUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    _agent: TenantMember = Depends(require_agent),
) -> QuickReplyInfo:
    reply = await quick_reply_service.update_reply(
        session,
        reply_id=reply_id,
        title=payload.title,
        content=payload.content,
        category=payload.category,
        sort_order=payload.sort_order,
    )
    await audit_service.record(
        session,
        action=audit_service.ACTION_QUICK_REPLY_UPDATE,
        actor_id=current_user.id,
        actor_name=current_user.display_name,
        target_type="quick_reply",
        target_id=str(reply.id),
        detail={"title": reply.title},
    )
    await session.commit()
    return QuickReplyInfo(
        id=reply.id,
        title=reply.title,
        content=reply.content,
        category=reply.category,
        sort_order=reply.sort_order,
        created_at=reply.created_at.isoformat(),
    )


@router.delete("/{reply_id}", status_code=204)
async def delete_reply(
    reply_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    _agent: TenantMember = Depends(require_agent),
) -> None:
    reply = await quick_reply_service.delete_reply(session, reply_id=reply_id)
    await audit_service.record(
        session,
        action=audit_service.ACTION_QUICK_REPLY_DELETE,
        actor_id=current_user.id,
        actor_name=current_user.display_name,
        target_type="quick_reply",
        target_id=str(reply_id),
        detail={"title": reply.title},
    )
    await session.commit()
