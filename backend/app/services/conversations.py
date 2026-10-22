"""会话与消息服务（设计文档 6 / 7 / 8）。

越权校验集中在此层（设计文档 8：每个资源查询强制带 owner 条件）：
路由只负责把"当前身份"翻译成 ConversationOwner，取数据一律经过
`get_conversation`，先校验归属再返回，越权一律 404（不暴露资源是否存在）。

owner 二选一：
- user  ：登录用户，按 user_id 过滤
- guest ：匿名访客，按 guest_id 过滤；登录合并后自动变为 user
"""

import random
import uuid
from dataclasses import dataclass

from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, NotFoundError
from app.core.logging import get_logger
from app.db.models.conversation import (
    CONVERSATION_STATUS_AI,
    OWNER_TYPE_USER,
    Conversation,
    Message,
)
from app.db.models.support import REVIEW_STATUS_PENDING, QualityReview
from app.services import tenant_settings
from app.services.pagination import decode_cursor, encode_cursor

logger = get_logger("conversations")

DEFAULT_LIST_LIMIT = 20
DEFAULT_MESSAGE_LIMIT = 50
MAX_LIMIT = 100


@dataclass(frozen=True, slots=True)
class ConversationOwner:
    """当前对话主体：由 API 依赖层解析，service 层只读。"""

    tenant_id: uuid.UUID
    owner_type: str
    user_id: uuid.UUID | None = None
    guest_id: uuid.UUID | None = None


def _owner_condition(owner: ConversationOwner) -> ColumnElement[bool]:
    if owner.owner_type == OWNER_TYPE_USER:
        if owner.user_id is None:
            raise ValueError("OWNER_TYPE_USER 缺少 user_id：属于调用方编程错误")
        return Conversation.user_id == owner.user_id
    if owner.guest_id is None:
        raise ValueError("OWNER_TYPE_GUEST 缺少 guest_id：属于调用方编程错误")
    return Conversation.guest_id == owner.guest_id


async def get_conversation(
    session: AsyncSession, *, owner: ConversationOwner, conversation_id: uuid.UUID
) -> Conversation:
    result = await session.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.tenant_id == owner.tenant_id,
            _owner_condition(owner),
        )
    )
    conversation = result.scalar_one_or_none()
    if conversation is None:
        raise NotFoundError("会话")
    return conversation


async def create_conversation(
    session: AsyncSession, *, owner: ConversationOwner, title: str = "", channel: str = "web"
) -> Conversation:
    """新建会话。channel 标记来源渠道（web/email/wechat/dingtalk）。

    渠道会话由 M4 的入站编排创建：同一外部用户复用同一会话，
    因此这里不做"同 channel 去重"——去重逻辑在 channels 服务里，需要
    结合外部身份与主题前缀，放在这层会污染通用的会话创建路径。
    """
    conversation = Conversation(
        tenant_id=owner.tenant_id,
        channel=channel,
        owner_type=owner.owner_type,
        user_id=owner.user_id,
        guest_id=owner.guest_id,
        status=CONVERSATION_STATUS_AI,
        title=title,
    )
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)
    await _maybe_sample_for_quality(session, conversation)
    return conversation


async def _maybe_sample_for_quality(
    session: AsyncSession, conversation: Conversation
) -> None:
    """按租户配置的比例把新会话送进质检队列（M9）。

    抽样失败绝不能让会话创建失败：用户只是想开个对话，
    质检是内部运营动作，两者的失败代价差两个数量级。
    """
    try:
        config = await tenant_settings.load(session, conversation.tenant_id)
        if config.quality_sample_ratio <= 0:
            return
        if random.random() >= config.quality_sample_ratio:
            return
        session.add(
            QualityReview(
                tenant_id=conversation.tenant_id,
                conversation_id=conversation.id,
                status=REVIEW_STATUS_PENDING,
                sampled_at=conversation.created_at,
            )
        )
        await session.commit()
    except Exception:  # noqa: BLE001
        await session.rollback()
        logger.exception("quality_auto_sample_failed", conversation_id=str(conversation.id))


async def list_conversations(
    session: AsyncSession, *, owner: ConversationOwner, limit: int, cursor: str | None
) -> tuple[list[Conversation], str | None]:
    stmt = (
        select(Conversation)
        .where(Conversation.tenant_id == owner.tenant_id, _owner_condition(owner))
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        .limit(limit + 1)
    )
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
    next_cursor = (
        encode_cursor(items[-1].updated_at, items[-1].id) if has_more and items else None
    )
    return items, next_cursor


async def update_conversation(
    session: AsyncSession,
    *,
    owner: ConversationOwner,
    conversation_id: uuid.UUID,
    title: str | None = None,
    status: str | None = None,
    voice_reply_enabled: bool | None = None,
) -> Conversation:
    conversation = await get_conversation(
        session, owner=owner, conversation_id=conversation_id
    )
    if title is not None:
        conversation.title = title
    if status is not None:
        conversation.status = status
    if voice_reply_enabled is not None:
        conversation.voice_reply_enabled = voice_reply_enabled
    await session.commit()
    await session.refresh(conversation)
    return conversation


async def get_message_owned(
    session: AsyncSession, *, owner: ConversationOwner, message_id: uuid.UUID
) -> Message:
    """按归属取单条消息。

    messages 表没有 owner 列，必须 join 会话再套 owner 条件；
    越权与不存在一律 404（不暴露消息是否存在，与 get_conversation 一致）。
    """
    result = await session.execute(
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.id == message_id,
            Message.tenant_id == owner.tenant_id,
            _owner_condition(owner),
        )
    )
    message = result.scalar_one_or_none()
    if message is None:
        raise NotFoundError("消息")
    return message


async def list_messages(
    session: AsyncSession,
    *,
    owner: ConversationOwner,
    conversation_id: uuid.UUID,
    limit: int,
    cursor: str | None,
) -> tuple[list[Message], str | None]:
    """按时间正序返回，next_cursor 用于向更早翻页。"""
    # 先校验归属：messages 表本身没有 owner 列，靠会话归属收敛权限
    await get_conversation(session, owner=owner, conversation_id=conversation_id)

    stmt = (
        select(Message)
        .where(
            Message.tenant_id == owner.tenant_id,
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


def clamp_limit(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))


async def rate_message(
    session: AsyncSession,
    *,
    owner: ConversationOwner,
    message_id: uuid.UUID,
    score: int,
) -> Message:
    """给一条 assistant 消息打满意度分（M8）。

    - 重复打分覆盖旧值（幂等，用户改主意不用先撤销）
    - 只能评自己会话里的 assistant 消息：归属由 get_message_owned 校验，
      越权与不存在一律 404；评自己的消息是明确的参数错误，422
    """
    message = await get_message_owned(session, owner=owner, message_id=message_id)
    if message.role != "assistant":
        raise AppError(
            code="RATING_NOT_ALLOWED",
            message="只能评价客服的回复",
            status_code=422,
            expose=True,
        )
    message.rating = score
    await session.commit()
    await session.refresh(message)
    return message
