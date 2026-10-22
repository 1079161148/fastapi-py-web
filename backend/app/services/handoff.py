"""人工接管服务（设计文档 5.3）。

状态联动（两条线各自负责一件事，互不覆盖）：

    工单：pending --认领--> claimed --结束--> resolved
    会话：ai --建单--> pending_handoff --认领--> human --结束--> ai

只有 `human` 会锁住 AI（见 chat.py）：pending_handoff 只是"已进入队列"，
坐席还没来，此时禁止 AI 回答等于把用户晾死。这是与文档图示的差异，
理由记在这里而不是藏在代码里。

幂等与并发：
- 建单：同会话已有未结工单直接复用，部分唯一索引兜底并发
- 认领：`UPDATE ... WHERE status='pending'` 的 rowcount 决定成败，
  两个坐席同时点只会有一个成功，另一个拿到 409 而不是"悄悄抢走"
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.pubsub import (
    EVENT_CONVERSATION_UPDATED,
    EVENT_TICKET_CANCELLED,
    EVENT_TICKET_CLAIMED,
    EVENT_TICKET_CREATED,
    EVENT_TICKET_RESOLVED,
    RealtimeEvent,
    publish,
)
from app.db.models.conversation import (
    CONVERSATION_STATUS_AI,
    CONVERSATION_STATUS_HUMAN,
    CONVERSATION_STATUS_PENDING_HANDOFF,
    Conversation,
    Message,
)
from app.db.models.handoff import (
    OPEN_TICKET_STATUSES,
    REASON_USER_REQUEST,
    REQUESTED_BY_USER,
    TICKET_STATUS_CANCELLED,
    TICKET_STATUS_CLAIMED,
    TICKET_STATUS_PENDING,
    TICKET_STATUS_RESOLVED,
    HandoffTicket,
)
from app.llm.base import ROLE_USER
from app.services.pagination import decode_cursor, encode_cursor

logger = get_logger("handoff")

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
SUMMARY_MAX_CHARS = 200
# 关键词触发时最多看最近这么多条用户消息：更早的话已经过时了
SUMMARY_LOOKBACK = 1


@dataclass(frozen=True, slots=True)
class HandoffResult:
    ticket: HandoffTicket
    created: bool


def _open_status_condition() -> ColumnElement[bool]:
    return HandoffTicket.status.in_(OPEN_TICKET_STATUSES)


async def get_open_ticket(
    session: AsyncSession, *, conversation_id: uuid.UUID
) -> HandoffTicket | None:
    result = await session.execute(
        select(HandoffTicket)
        .where(HandoffTicket.conversation_id == conversation_id, _open_status_condition())
        .order_by(HandoffTicket.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _last_user_message(session: AsyncSession, conversation_id: uuid.UUID) -> str:
    result = await session.execute(
        select(Message.content)
        .where(Message.conversation_id == conversation_id, Message.role == ROLE_USER)
        .order_by(Message.created_at.desc())
        .limit(SUMMARY_LOOKBACK)
    )
    content = result.scalar_one_or_none() or ""
    return content.strip()[:SUMMARY_MAX_CHARS]


async def request_handoff(
    session: AsyncSession,
    *,
    conversation: Conversation,
    reason: str = REASON_USER_REQUEST,
    requested_by: str = REQUESTED_BY_USER,
    note: str = "",
    extra: dict[str, Any] | None = None,
) -> HandoffResult:
    """建单（幂等）：同会话已有未结工单则直接返回，不重复入队。

    调用方必须已完成会话归属/租户校验。返回 created 标记，便于接口区分
    "新建"与"已在队列中"，也给前端不同提示。
    """
    existing = await get_open_ticket(session, conversation_id=conversation.id)
    if existing is not None:
        return HandoffResult(ticket=existing, created=False)

    now = datetime.now(UTC)
    ticket = HandoffTicket(
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        status=TICKET_STATUS_PENDING,
        reason=reason,
        requested_by=requested_by,
        note=note,
        summary=await _last_user_message(session, conversation.id),
        extra=extra or {},
        created_at=now,
        updated_at=now,
    )
    session.add(ticket)
    # pending_handoff 只是"排队中"：AI 继续回答，避免用户没人管
    if conversation.status == CONVERSATION_STATUS_AI:
        conversation.status = CONVERSATION_STATUS_PENDING_HANDOFF
        conversation.updated_at = now

    try:
        await session.commit()
    except IntegrityError:
        # 并发重复提交：唯一索引挡下后回读已存在的那张，语义仍是"排在队列里了"
        await session.rollback()
        existing = await get_open_ticket(session, conversation_id=conversation.id)
        if existing is None:
            raise
        return HandoffResult(ticket=existing, created=False)

    await session.refresh(ticket)
    await session.refresh(conversation)
    await publish([_event(EVENT_TICKET_CREATED, ticket, conversation)])
    logger.info(
        "handoff_requested",
        ticket_id=str(ticket.id),
        conversation_id=str(conversation.id),
        reason=reason,
        requested_by=requested_by,
    )
    return HandoffResult(ticket=ticket, created=True)


async def get_ticket(
    session: AsyncSession, *, tenant_id: uuid.UUID, ticket_id: uuid.UUID
) -> HandoffTicket:
    ticket = await session.get(HandoffTicket, ticket_id)
    if ticket is None or ticket.tenant_id != tenant_id:
        raise NotFoundError("工单")
    return ticket


async def claim_ticket(
    session: AsyncSession, *, tenant_id: uuid.UUID, ticket_id: uuid.UUID, agent_id: uuid.UUID
) -> HandoffTicket:
    """坐席认领：条件更新 + returning，抢不到就是抢不到（409）。"""
    now = datetime.now(UTC)
    result = await session.execute(
        update(HandoffTicket)
        .where(
            HandoffTicket.id == ticket_id,
            HandoffTicket.tenant_id == tenant_id,
            HandoffTicket.status == TICKET_STATUS_PENDING,
        )
        .values(
            status=TICKET_STATUS_CLAIMED,
            agent_id=agent_id,
            claimed_at=now,
            updated_at=now,
        )
        .returning(HandoffTicket)
        .execution_options(synchronize_session=False)
    )
    ticket = result.scalar_one_or_none()
    if ticket is None:
        existing = await session.get(HandoffTicket, ticket_id)
        if existing is None or existing.tenant_id != tenant_id:
            raise NotFoundError("工单")
        if existing.status == TICKET_STATUS_CLAIMED:
            raise ConflictError("该工单已被其他坐席认领，请刷新队列")
        raise ConflictError(f"该工单当前状态为 {existing.status}，无法认领")

    conversation = await _set_conversation_status(
        session, conversation_id=ticket.conversation_id, status=CONVERSATION_STATUS_HUMAN, now=now
    )
    await session.commit()
    await session.refresh(ticket)
    await publish(
        [
            _event(EVENT_TICKET_CLAIMED, ticket, conversation),
            _event(
                EVENT_CONVERSATION_UPDATED,
                ticket,
                conversation,
                data_extra={"status": conversation.status},
            ),
        ]
    )
    logger.info("handoff_claimed", ticket_id=str(ticket.id), agent_id=str(agent_id))
    return ticket


async def resolve_ticket(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    ticket_id: uuid.UUID,
    agent_id: uuid.UUID,
    force: bool = False,
) -> HandoffTicket:
    """结束工单并把会话交还 AI（`force` 供 owner/admin 强制结束别人的单）。"""
    now = datetime.now(UTC)
    conditions = [
        HandoffTicket.id == ticket_id,
        HandoffTicket.tenant_id == tenant_id,
        HandoffTicket.status.in_((TICKET_STATUS_PENDING, TICKET_STATUS_CLAIMED)),
    ]
    if not force:
        conditions.append(HandoffTicket.agent_id == agent_id)

    result = await session.execute(
        update(HandoffTicket)
        .where(*conditions)
        .values(status=TICKET_STATUS_RESOLVED, resolved_at=now, updated_at=now)
        .returning(HandoffTicket)
        .execution_options(synchronize_session=False)
    )
    ticket = result.scalar_one_or_none()
    if ticket is None:
        existing = await session.get(HandoffTicket, ticket_id)
        if existing is None or existing.tenant_id != tenant_id:
            raise NotFoundError("工单")
        raise ConflictError("只能结束自己认领的工单；如需强制结束请让管理员操作")

    conversation = await _set_conversation_status(
        session, conversation_id=ticket.conversation_id, status=CONVERSATION_STATUS_AI, now=now
    )
    await session.commit()
    await session.refresh(ticket)
    await publish(
        [
            _event(EVENT_TICKET_RESOLVED, ticket, conversation),
            _event(
                EVENT_CONVERSATION_UPDATED,
                ticket,
                conversation,
                data_extra={"status": conversation.status},
            ),
        ]
    )
    logger.info("handoff_resolved", ticket_id=str(ticket.id), agent_id=str(agent_id))
    return ticket


async def cancel_ticket(
    session: AsyncSession, *, tenant_id: uuid.UUID, conversation_id: uuid.UUID
) -> HandoffTicket | None:
    """用户主动取消排队：pending --取消--> cancelled，会话回到 ai。

    只允许取消仍在排队（pending）的单。已认领时坐席已经在打字，单子突然消失
    比"再等一会儿"更让人困惑，所以拒绝并让用户直接跟坐席说。

    没有未结工单时返回 None：接口按幂等处理（重复点取消不该报错）。
    调用方（conversations 路由）负责校验会话归属，这里只做状态判定。
    """
    now = datetime.now(UTC)
    open_ticket = await get_open_ticket(session, conversation_id=conversation_id)
    if open_ticket is None or open_ticket.tenant_id != tenant_id:
        return None

    result = await session.execute(
        update(HandoffTicket)
        .where(
            HandoffTicket.id == open_ticket.id,
            HandoffTicket.tenant_id == tenant_id,
            HandoffTicket.status == TICKET_STATUS_PENDING,
        )
        .values(status=TICKET_STATUS_CANCELLED, resolved_at=now, updated_at=now)
        .returning(HandoffTicket)
        .execution_options(synchronize_session=False)
    )
    ticket = result.scalar_one_or_none()
    if ticket is None:
        # 认领与取消同时发生：认领赢了，取消方必须知道
        raise ConflictError("坐席已接入，无法取消排队；请直接把问题告诉坐席")

    conversation = await _set_conversation_status(
        session, conversation_id=conversation_id, status=CONVERSATION_STATUS_AI, now=now
    )
    await session.commit()
    await session.refresh(ticket)
    await publish(
        [
            _event(EVENT_TICKET_CANCELLED, ticket, conversation),
            _event(
                EVENT_CONVERSATION_UPDATED,
                ticket,
                conversation,
                data_extra={"status": conversation.status},
            ),
        ]
    )
    logger.info("handoff_cancelled", ticket_id=str(ticket.id), conversation_id=str(conversation_id))
    return ticket


async def list_tickets(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    statuses: list[str] | None,
    agent_id: uuid.UUID | None,
    limit: int,
    cursor: str | None,
) -> tuple[list[HandoffTicket], str | None]:
    """坐席队列：默认"未结优先、旧的先处理"。

    排队场景下按 created_at **升序**：最早进来的应该最先被接，
    否则"刚转进来"的单会被反复刷屏、老单永远排在后面。
    """
    stmt = select(HandoffTicket).where(HandoffTicket.tenant_id == tenant_id)
    if statuses:
        stmt = stmt.where(HandoffTicket.status.in_(statuses))
    if agent_id is not None:
        stmt = stmt.where(HandoffTicket.agent_id == agent_id)
    stmt = stmt.order_by(HandoffTicket.created_at.asc(), HandoffTicket.id.asc()).limit(limit + 1)
    if cursor is not None:
        sort_time, row_id = decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                HandoffTicket.created_at > sort_time,
                and_(HandoffTicket.created_at == sort_time, HandoffTicket.id > row_id),
            )
        )

    rows = list((await session.execute(stmt)).scalars().all())
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = encode_cursor(items[-1].created_at, items[-1].id) if has_more and items else None
    return items, next_cursor


async def cancel_open_tickets_for_conversation(
    session: AsyncSession, *, conversation_id: uuid.UUID
) -> None:
    """会话被关闭/删除时清理未结工单，避免坐席队列里出现永远处理不了的僵尸单。"""
    await session.execute(
        update(HandoffTicket)
        .where(HandoffTicket.conversation_id == conversation_id, _open_status_condition())
        .values(status=TICKET_STATUS_RESOLVED, resolved_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )


async def _set_conversation_status(
    session: AsyncSession, *, conversation_id: uuid.UUID, status: str, now: datetime
) -> Conversation:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise NotFoundError("会话")
    conversation.status = status
    conversation.updated_at = now
    return conversation


def _event(
    event_type: str,
    ticket: HandoffTicket,
    conversation: Conversation,
    *,
    data_extra: dict[str, Any] | None = None,
) -> RealtimeEvent:
    data: dict[str, Any] = {
        "ticket_id": str(ticket.id),
        "conversation_id": str(ticket.conversation_id),
        "status": ticket.status,
        "reason": ticket.reason,
        "requested_by": ticket.requested_by,
        "summary": ticket.summary,
        "channel": conversation.channel,
        "owner_type": conversation.owner_type,
        "agent_id": str(ticket.agent_id) if ticket.agent_id else None,
        "conversation_title": conversation.title,
        "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
    }
    if data_extra:
        data.update(data_extra)
    return RealtimeEvent(
        type=event_type,
        tenant_id=ticket.tenant_id,
        conversation_id=ticket.conversation_id,
        ticket_id=ticket.id,
        data=data,
    )


# 会话被关闭时保留工单历史，只是关闭未结的那张
__all__ = [
    "HandoffResult",
    "cancel_open_tickets_for_conversation",
    "cancel_ticket",
    "claim_ticket",
    "get_open_ticket",
    "get_ticket",
    "list_tickets",
    "request_handoff",
    "resolve_ticket",
]
