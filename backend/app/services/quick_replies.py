"""快捷回复（坐席常用话术）CRUD。"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.core.tenant import require_current_tenant_id
from app.db.models.support import QuickReply


async def list_replies(
    session: AsyncSession,
    *,
    category: str | None = None,
) -> list[QuickReply]:
    tenant_id = require_current_tenant_id()
    stmt = select(QuickReply).where(QuickReply.tenant_id == tenant_id)
    if category:
        stmt = stmt.where(QuickReply.category == category)
    # 同分组内按 sort_order，再按创建时间，保证顺序稳定
    stmt = stmt.order_by(QuickReply.sort_order, QuickReply.created_at)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def create_reply(
    session: AsyncSession,
    *,
    title: str,
    content: str,
    category: str,
    sort_order: int,
    created_by: uuid.UUID | None = None,
) -> QuickReply:
    tenant_id = require_current_tenant_id()
    reply = QuickReply(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        title=title,
        content=content,
        category=category,
        sort_order=sort_order,
        created_by=created_by,
    )
    session.add(reply)
    await session.commit()
    await session.refresh(reply)
    return reply


async def update_reply(
    session: AsyncSession,
    *,
    reply_id: uuid.UUID,
    title: str | None,
    content: str | None,
    category: str | None,
    sort_order: int | None,
) -> QuickReply:
    reply = await _get(session, reply_id=reply_id)
    if title is not None:
        reply.title = title
    if content is not None:
        reply.content = content
    if category is not None:
        reply.category = category
    if sort_order is not None:
        reply.sort_order = sort_order
    await session.commit()
    await session.refresh(reply)
    return reply


async def delete_reply(session: AsyncSession, *, reply_id: uuid.UUID) -> QuickReply:
    """删除并返回被删对象（路由层要拿 title 写审计）。"""
    reply = await _get(session, reply_id=reply_id)
    await session.delete(reply)
    await session.commit()
    return reply


async def _get(session: AsyncSession, *, reply_id: uuid.UUID) -> QuickReply:
    tenant_id = require_current_tenant_id()
    reply = await session.get(QuickReply, reply_id)
    if reply is None or reply.tenant_id != tenant_id:
        raise NotFoundError("快捷回复")
    return reply
