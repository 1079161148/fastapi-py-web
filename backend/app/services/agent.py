"""坐席工作台服务（设计文档 5.3 / 5.4 / 9.1）。

与 `conversations.py` 的边界：
- 那边是"用户看自己的会话"，按 owner 条件过滤
- 这边是"坐席看本租户待处理的会话"，按 tenant 条件过滤 + 角色门禁

两条权限路径刻意不合并：混在一起意味着越权校验依赖调用方"记得传对参数"，
而这正是最容易出错的地方。

坐席回复的出站：
- web 渠道：SSE 广播即投递（用户端订阅自己的会话频道）
- 其他渠道：调 ChannelRouter 回到原渠道（微信/钉钉/邮件），
  失败不抛 500——消息已经落库，坐席需要知道的是"发出去了没有"，
  而不是让整次操作失败、消息丢失
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels import ChannelRouter
from app.channels.base import ChannelError
from app.core.exceptions import AppError, NotFoundError
from app.core.logging import get_logger
from app.db.models.channel import CHANNEL_WEB, DIRECTION_INBOUND, ChannelMessage
from app.db.models.conversation import (
    CONVERSATION_STATUS_HUMAN,
    Conversation,
    Message,
)
from app.db.models.handoff import (
    OPEN_TICKET_STATUSES,
    TICKET_STATUS_CLAIMED,
    HandoffTicket,
)
from app.llm.base import ROLE_ASSISTANT
from app.services import handoff as handoff_service
from app.services import messages as message_service
from app.services.pagination import decode_cursor, encode_cursor

logger = get_logger("agent")

# 坐席回复的消息来源标记：前端据此渲染成"坐席"而不是"AI"
SOURCE_AGENT = "agent"

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
# 默认只看"需要人"的会话；质检场景可显式传全部状态
DEFAULT_CONVERSATION_STATUSES = ("pending_handoff", "human")


@dataclass(frozen=True, slots=True)
class AgentReply:
    message: Message
    delivered: bool
    delivery_error: str


@dataclass(frozen=True, slots=True)
class AgentConversation:
    """会话 + 进行中工单：坐席列表要同时看到"谁在等"和"谁在处理"。"""

    conversation: Conversation
    ticket: HandoffTicket | None


async def get_conversation(
    session: AsyncSession, *, tenant_id: uuid.UUID, conversation_id: uuid.UUID
) -> Conversation:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or conversation.tenant_id != tenant_id:
        raise NotFoundError("会话")
    return conversation


async def list_conversations(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    statuses: tuple[str, ...] | None,
    limit: int,
    cursor: str | None,
    channel: str | None = None,
    owner_type: str | None = None,
) -> tuple[list[AgentConversation], str | None]:
    """坐席视角的会话列表：最近有动静的排前面（按活跃度处理，而非创建时间）。

    channel / owner_type 供管理后台筛选（M7）：渠道运营要按来源看会话，
    客服主管要区分"登录用户"与"匿名访客"的实际流量。
    """
    stmt = select(Conversation).where(Conversation.tenant_id == tenant_id)
    targets = statuses or DEFAULT_CONVERSATION_STATUSES
    stmt = stmt.where(Conversation.status.in_(targets))
    if channel is not None:
        stmt = stmt.where(Conversation.channel == channel)
    if owner_type is not None:
        stmt = stmt.where(Conversation.owner_type == owner_type)
    stmt = stmt.order_by(Conversation.updated_at.desc(), Conversation.id.desc()).limit(limit + 1)
    if cursor is not None:
        sort_time, row_id = decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                Conversation.updated_at < sort_time,
                and_(Conversation.updated_at == sort_time, Conversation.id < row_id),
            )
        )

    rows = list((await session.execute(stmt)).scalars().all())
    has_more = len(rows) > limit
    items = rows[:limit]
    tickets = await _open_tickets_for(session, [item.id for item in items])
    next_cursor = (
        encode_cursor(items[-1].updated_at, items[-1].id) if has_more and items else None
    )
    return (
        [AgentConversation(conversation=item, ticket=tickets.get(item.id)) for item in items],
        next_cursor,
    )


async def _open_tickets_for(
    session: AsyncSession, conversation_ids: list[uuid.UUID]
) -> dict[uuid.UUID, HandoffTicket]:
    """一次查出这批会话的未结工单，避免列表里逐条 N+1。"""
    if not conversation_ids:
        return {}
    rows = (
        await session.execute(
            select(HandoffTicket).where(
                HandoffTicket.conversation_id.in_(conversation_ids),
                HandoffTicket.status.in_(OPEN_TICKET_STATUSES),
            )
        )
    ).scalars().all()
    return {row.conversation_id: row for row in rows}


async def list_messages(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    limit: int,
    cursor: str | None,
) -> tuple[list[Message], str | None]:
    await get_conversation(session, tenant_id=tenant_id, conversation_id=conversation_id)
    stmt = (
        select(Message)
        .where(
            Message.tenant_id == tenant_id,
            Message.conversation_id == conversation_id,
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(limit + 1)
    )
    if cursor is not None:
        sort_time, row_id = decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                Message.created_at < sort_time,
                and_(Message.created_at == sort_time, Message.id < row_id),
            )
        )

    rows = list((await session.execute(stmt)).scalars().all())
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1].created_at, page[-1].id) if has_more and page else None
    page.reverse()
    return page, next_cursor


async def reply(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    agent_id: uuid.UUID,
    content: str,
    router: ChannelRouter,
) -> AgentReply:
    """坐席回复：落库 → 广播 →（非 web 渠道）回落原渠道。

    前置条件：会话的工单必须由当前坐席认领且会话已处于 human。
    没有认领就回复会绕过"谁在处理"的归属，多个坐席同时说话会乱。
    """
    text = content.strip()
    if not text:
        raise AppError(code="VALIDATION_ERROR", message="回复内容不能为空", status_code=422)

    conversation = await get_conversation(
        session, tenant_id=tenant_id, conversation_id=conversation_id
    )
    ticket = await _require_claimed_ticket(
        session, conversation_id=conversation_id, agent_id=agent_id
    )

    now = datetime.now(UTC)
    message = Message(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        message_type="text",
        role=ROLE_ASSISTANT,
        content=text,
        source=SOURCE_AGENT,
        created_at=now,
        updated_at=now,
    )
    session.add(message)
    conversation.updated_at = now
    if conversation.status != CONVERSATION_STATUS_HUMAN:
        # 状态漂移（如被其他流程改成 ai）：以工单为准修正，避免 AI 与坐席同时说话
        conversation.status = CONVERSATION_STATUS_HUMAN
    await session.commit()
    await session.refresh(message)
    await message_service.publish_message_event(message)

    delivered, delivery_error = await _deliver(
        session, conversation=conversation, message=message, router=router
    )
    logger.info(
        "agent_replied",
        conversation_id=str(conversation_id),
        agent_id=str(agent_id),
        ticket_id=str(ticket.id),
        channel=conversation.channel,
        delivered=delivered,
    )
    return AgentReply(message=message, delivered=delivered, delivery_error=delivery_error)


async def _require_claimed_ticket(
    session: AsyncSession, *, conversation_id: uuid.UUID, agent_id: uuid.UUID
) -> HandoffTicket:
    ticket = await handoff_service.get_open_ticket(session, conversation_id=conversation_id)
    if ticket is None:
        raise AppError(
            code="HANDOFF_NOT_CLAIMED",
            message="该会话没有进行中的工单，无法以坐席身份回复",
            status_code=409,
        )
    if ticket.status != TICKET_STATUS_CLAIMED or ticket.agent_id != agent_id:
        raise AppError(
            code="HANDOFF_NOT_CLAIMED",
            message="请先认领该工单再回复（或等待当前坐席结束）",
            status_code=409,
        )
    return ticket


async def _deliver(
    session: AsyncSession,
    *,
    conversation: Conversation,
    message: Message,
    router: ChannelRouter,
) -> tuple[bool, str]:
    """非 web 渠道回落原渠道；web 渠道由 SSE 广播完成投递。"""
    if conversation.channel == CHANNEL_WEB:
        return True, ""

    external_user_id, reply_context = await _last_inbound_context(
        session, conversation_id=conversation.id
    )
    try:
        await router.deliver(
            channel=conversation.channel,
            conversation_id=conversation.id,
            external_user_id=external_user_id,
            content=message.content,
            reply_context=reply_context,
        )
    except ChannelError as exc:
        # 消息已落库：坐席能看到"未送达"，用户下次进来仍能看到内容
        logger.warning(
            "agent_reply_delivery_failed",
            conversation_id=str(conversation.id),
            channel=conversation.channel,
            error=exc.message,
        )
        return False, exc.message
    return True, ""


async def _last_inbound_context(
    session: AsyncSession, *, conversation_id: uuid.UUID
) -> tuple[str, dict[str, str]]:
    """取最近一条入站渠道消息的上下文（钉钉 sessionWebhook / 邮件主题等）。

    出站需要它才能回到原会话（例如钉钉被动回复必须带 sessionWebhook）。
    """
    row = (
        await session.execute(
            select(ChannelMessage)
            .where(
                ChannelMessage.conversation_id == conversation_id,
                ChannelMessage.direction == DIRECTION_INBOUND,
            )
            .order_by(ChannelMessage.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return "", {}
    context = {str(key): str(value) for key, value in (row.reply_context or {}).items()}
    return row.external_user_id, context


def clamp_limit(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))


__all__ = [
    "AgentConversation",
    "AgentReply",
    "SOURCE_AGENT",
    "clamp_limit",
    "get_conversation",
    "list_conversations",
    "list_messages",
    "reply",
]
