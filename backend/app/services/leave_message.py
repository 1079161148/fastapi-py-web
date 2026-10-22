"""离线留言服务（M8）。

坐席不在线、用户不想排队时的兜底：留下联系方式与问题，稍后人工回。
与转人工工单的区别：留言没有排队与认领，只有"待处理/已处理"。
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.db.models.conversation import Conversation
from app.db.models.leave_message import LEAVE_STATUS_HANDLED, LEAVE_STATUS_NEW, LeaveMessage
from app.services.pagination import decode_cursor, encode_cursor

logger = get_logger("leave_message")

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


async def create(
    session: AsyncSession,
    *,
    conversation: Conversation | None,
    tenant_id: uuid.UUID,
    owner_type: str,
    name: str,
    contact: str,
    content: str,
) -> LeaveMessage:
    """落一条留言。同一会话反复提交不做合并：用户补充了新信息就该多一条。"""
    message = LeaveMessage(
        tenant_id=tenant_id,
        conversation_id=conversation.id if conversation is not None else None,
        owner_type=owner_type,
        name=name,
        contact=contact,
        content=content,
        status=LEAVE_STATUS_NEW,
    )
    session.add(message)
    await session.commit()
    await session.refresh(message)
    logger.info(
        "leave_message_created",
        leave_message_id=str(message.id),
        conversation_id=str(message.conversation_id) if message.conversation_id else None,
    )
    return message


async def list_messages(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    status: str | None,
    limit: int,
    cursor: str | None,
) -> tuple[list[LeaveMessage], str | None]:
    """坐席列表：新的在前（留言是"待回"，不是"排队"，先看最近进来的）。"""
    stmt = select(LeaveMessage).where(LeaveMessage.tenant_id == tenant_id)
    if status:
        stmt = stmt.where(LeaveMessage.status == status)
    stmt = stmt.order_by(LeaveMessage.created_at.desc(), LeaveMessage.id.desc()).limit(limit + 1)
    if cursor is not None:
        sort_time, row_id = decode_cursor(cursor)
        stmt = stmt.where(LeaveMessage.created_at < sort_time)

    rows = list((await session.execute(stmt)).scalars().all())
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = encode_cursor(items[-1].created_at, items[-1].id) if has_more and items else None
    return items, next_cursor


async def mark_handled(
    session: AsyncSession, *, tenant_id: uuid.UUID, leave_id: uuid.UUID, agent_id: uuid.UUID
) -> LeaveMessage:
    """标记已处理。幂等：重复标记保留第一次的处理人与时间。"""
    message = await session.get(LeaveMessage, leave_id)
    if message is None or message.tenant_id != tenant_id:
        raise NotFoundError("留言")
    if message.status != LEAVE_STATUS_HANDLED:
        message.status = LEAVE_STATUS_HANDLED
        message.handled_by = agent_id
        message.handled_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(message)
        logger.info("leave_message_handled", leave_message_id=str(leave_id))
    return message


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "create",
    "list_messages",
    "mark_handled",
]
