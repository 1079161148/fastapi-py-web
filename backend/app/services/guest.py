"""访客会话服务（设计文档 5.5）。

生命周期：

    active --(登录合并)--> merged
    active --(超过 TTL)--> 过期后拒绝使用（清理任务在 M6 落地）

安全约束：
- guest_id 只是会话标识，不是认证凭证（设计文档 8）。它的价值被限制在
  "某个租户下某段对话的工作台"，合并时必须再有 access token 才生效。
- 所有查询都在租户上下文内执行，RLS 兜底：跨租户的 guest_id 直接不可见。
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AppError, NotFoundError
from app.core.pubsub import EVENT_GUEST_MERGED, RealtimeEvent, publish
from app.db.models.conversation import (
    GUEST_STATUS_ACTIVE,
    GUEST_STATUS_MERGED,
    OWNER_TYPE_GUEST,
    OWNER_TYPE_USER,
    Conversation,
    GuestSession,
)
from app.db.models.tenant import Tenant
from app.db.models.user import User

MERGE_STATUS_MERGED = "merged"
MERGE_STATUS_ALREADY_MERGED = "already_merged"
MERGE_STATUS_NO_SESSION = "no_session"


@dataclass(frozen=True, slots=True)
class MergeResult:
    merged_conversations: int
    status: str


class GuestSessionInvalidError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code="GUEST_SESSION_INVALID",
            message="访客会话已失效，请刷新页面重新开始",
            status_code=401,
        )


async def ensure_tenant(session: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    """签发前确认租户存在且启用；避免为任意 UUID 造出孤儿数据。"""
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None or tenant.status != "active":
        raise NotFoundError("租户")
    return tenant


async def get_active_session(
    session: AsyncSession, *, tenant_id: uuid.UUID, guest_id: uuid.UUID
) -> GuestSession:
    """取可用的访客会话；不存在/已合并不匹配/已过期一律视为失效。"""
    result = await session.execute(select(GuestSession).where(GuestSession.guest_id == guest_id))
    guest = result.scalar_one_or_none()
    if guest is None or guest.tenant_id != tenant_id:
        raise GuestSessionInvalidError()
    if guest.status != GUEST_STATUS_ACTIVE:
        raise GuestSessionInvalidError()
    if guest.expires_at <= datetime.now(UTC):
        raise GuestSessionInvalidError()
    return guest


async def start_session(session: AsyncSession, *, tenant_id: uuid.UUID) -> GuestSession:
    settings = get_settings()
    now = datetime.now(UTC)
    guest = GuestSession(
        guest_id=uuid.uuid4(),
        tenant_id=tenant_id,
        status=GUEST_STATUS_ACTIVE,
        expires_at=now + timedelta(days=settings.guest_session_ttl_days),
        created_at=now,
        updated_at=now,
    )
    session.add(guest)
    await session.commit()
    return guest


async def count_conversations(
    session: AsyncSession, *, tenant_id: uuid.UUID, guest_id: uuid.UUID
) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(Conversation)
        .where(
            Conversation.tenant_id == tenant_id,
            Conversation.guest_id == guest_id,
            Conversation.owner_type == OWNER_TYPE_GUEST,
        )
    )
    return int(result.scalar_one())


async def merge_guest(
    session: AsyncSession, *, user: User, guest_id: uuid.UUID | None
) -> MergeResult:
    """把访客会话并入当前用户（设计文档 5.5 第③步）。

    幂等约定：重复提交、guest 已作废、没有 guest cookie 都返回成功
    （status 为 already_merged / no_session），不报错——合并是"登录后顺带
    完成"的动作，用户不该看到失败提示。
    """
    if guest_id is None:
        return MergeResult(merged_conversations=0, status=MERGE_STATUS_NO_SESSION)

    # FOR UPDATE 锁住 guest 行：并发合并（多标签页同时登录）只在第一个事务里转移，
    # 后到者读到 merged 状态直接返回，杜绝所有权被写两次
    result = await session.execute(
        select(GuestSession).where(GuestSession.guest_id == guest_id).with_for_update()
    )
    guest = result.scalar_one_or_none()
    # RLS 下跨租户 guest 不可见 → 与"不存在"同处理，不泄露其它租户是否存在该访客
    if guest is None or guest.tenant_id != user.tenant_id:
        return MergeResult(merged_conversations=0, status=MERGE_STATUS_NO_SESSION)
    if guest.status != GUEST_STATUS_ACTIVE:
        return MergeResult(merged_conversations=0, status=MERGE_STATUS_ALREADY_MERGED)

    conversations = (
        await session.execute(
            select(Conversation).where(
                Conversation.tenant_id == user.tenant_id,
                Conversation.guest_id == guest_id,
                Conversation.owner_type == OWNER_TYPE_GUEST,
            )
        )
    ).scalars().all()

    now = datetime.now(UTC)
    for conversation in conversations:
        conversation.owner_type = OWNER_TYPE_USER
        conversation.user_id = user.id
        conversation.guest_id = None
        conversation.updated_at = now

    guest.status = GUEST_STATUS_MERGED
    guest.merged_user_id = user.id
    guest.merged_at = now
    guest.updated_at = now

    # 所有权转移与 guest 作废在同一事务提交，避免出现"guest 已废但对话还没转"的中间态
    await session.commit()

    if conversations:
        # 坐席端要立刻知道"正在聊的这个人已经登录了"（设计文档 5.5），
        # 一条事件带全部会话 ID，不按会话拆成 N 条刷屏
        await publish(
            [
                RealtimeEvent(
                    type=EVENT_GUEST_MERGED,
                    tenant_id=user.tenant_id,
                    data={
                        "user_id": str(user.id),
                        "conversation_ids": [str(item.id) for item in conversations],
                    },
                )
            ]
        )

    return MergeResult(merged_conversations=len(conversations), status=MERGE_STATUS_MERGED)
