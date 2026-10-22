"""渠道接入编排（设计文档 9.1）。

入站三段式，每段职责单一：

1. `intake`：验签后的落库（**幂等**）+ 外部身份映射 + 会话定位，秒级完成
2. `process_inbound`（worker）：生成回复（复用 chat 编排）→ 出站投递 → 回写状态
3. `ChannelRouter`：按 `conversation.channel` 选适配器投递

为什么 webhook 不做同步生成：微信/钉钉要求 5s 内响应，而 AI 生成通常 2-10s。
同步处理必然触发平台重推，进而在幂等键之外浪费模型额度；异步化后
重推只会命中唯一键直接跳过。

幂等三层：
- 入站唯一键 (tenant_id, channel, external_msg_id) → 平台重推不重复生成
- 出站流水键固定为 `out-{入站记录 id}` → 投递失败重试不会重复发
- 生成结果先落出站流水再发送 → 重试只重投，不重新生成（省模型额度）

外部用户归属：渠道用户没有登录账号，用 guest_sessions 承载会话归属
（conversations.owner_type = "guest"）。这样不必给 conversations 增加
第三种 owner，既有 owner 一致性约束与合并逻辑完全不用改。
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Text, cast, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import ChannelError, InboundMessage
from app.channels.router import ChannelRouter
from app.core.config import get_settings
from app.core.exceptions import AppError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.tenant import set_current_tenant
from app.db.models.channel import (
    ALL_CHANNELS,
    DIRECTION_INBOUND,
    DIRECTION_OUTBOUND,
    MSG_STATUS_FAILED,
    MSG_STATUS_PROCESSING,
    MSG_STATUS_RECEIVED,
    MSG_STATUS_REPLIED,
    MSG_STATUS_SKIPPED,
    ChannelConfig,
    ChannelIdentity,
    ChannelMessage,
)
from app.db.models.conversation import (
    GUEST_STATUS_ACTIVE,
    OWNER_TYPE_GUEST,
    Conversation,
    GuestSession,
)
from app.db.session import async_session_factory
from app.llm.base import EmbeddingClient, LLMClient, RerankClient
from app.services import chat as chat_service
from app.services import guest as guest_service
from app.services.conversations import ConversationOwner, create_conversation

logger = get_logger("channels")

# 状态为 processing 超过该时长视为 worker 已崩溃，允许重新处理（避免死信）
PROCESSING_TIMEOUT_SECONDS = 300
# guest 会话续期阈值：剩余不足一天才写库，避免每条消息都 UPDATE
GUEST_RENEW_THRESHOLD = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class IntakeResult:
    """入站落库结果。status 直接对应 channel_messages.status。"""

    status: str
    conversation_id: uuid.UUID | None
    created: bool
    reason: str = ""


# ---------------------------------------------------------------- 配置与反查


async def resolve_tenant_id(
    session: AsyncSession, *, channel: str, external_account_id: str
) -> uuid.UUID:
    """webhook 没有租户上下文，按平台账号反查租户（迁移 0004 的 SECURITY DEFINER 函数）。

    这是唯一受控越权点：函数只返回 tenant_id，不泄露配置与凭证。
    """
    if not external_account_id:
        raise NotFoundError("渠道配置")
    value = (
        await session.execute(select(func.channel_tenant(channel, external_account_id)))
    ).scalar_one_or_none()
    if value is None:
        # 未配置 / 已禁用 / 账号标识填错，都按"不存在"处理，不区分（避免探测）
        raise NotFoundError("渠道配置")
    return uuid.UUID(str(value))


async def list_channel_accounts(
    session: AsyncSession, channel: str
) -> list[tuple[uuid.UUID, str]]:
    """列出某渠道所有启用的 (租户, 外部账号)，供邮件轮询使用。"""
    table = func.channel_accounts(channel).table_valued("tenant_id", "external_account_id")
    rows = await session.execute(select(table.c.tenant_id, table.c.external_account_id))
    return [(uuid.UUID(str(tenant_id)), str(account)) for tenant_id, account in rows.all()]


async def activate_tenant(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """切换到指定租户：**先结束当前事务**，再设置上下文。

    RLS 的 SET LOCAL 只在事务开始时注入（见 db/rls.py）。webhook 的反查发生在
    "还不知道租户"的事务里；若不换事务，后续查询仍在旧上下文（无租户）中执行，
    表现为"配置明明存在却查不到"。
    """
    await session.commit()
    set_current_tenant(tenant_id)


async def find_config(
    session: AsyncSession, *, tenant_id: uuid.UUID, channel: str
) -> ChannelConfig | None:
    return (
        await session.execute(
            select(ChannelConfig).where(
                ChannelConfig.tenant_id == tenant_id, ChannelConfig.channel == channel
            )
        )
    ).scalar_one_or_none()


async def get_config(
    session: AsyncSession, *, tenant_id: uuid.UUID, channel: str
) -> ChannelConfig:
    config = await find_config(session, tenant_id=tenant_id, channel=channel)
    if config is None:
        raise NotFoundError("渠道配置")
    return config


async def list_configs(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[ChannelConfig]:
    return list(
        (
            await session.execute(
                select(ChannelConfig)
                .where(ChannelConfig.tenant_id == tenant_id)
                .order_by(ChannelConfig.channel)
            )
        )
        .scalars()
        .all()
    )


async def upsert_config(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel: str,
    enabled: bool | None = None,
    external_account_id: str | None = None,
    credentials: dict[str, object] | None = None,
    rate_limit: int | None = None,
    webhook_secret: str | None = None,
) -> ChannelConfig:
    """新建或更新渠道配置。只更新显式传入的字段（None 表示"保持不变"）。"""
    if channel not in ALL_CHANNELS:
        raise AppError(
            code="CHANNEL_UNKNOWN",
            message=f"不支持的渠道：{channel}",
            status_code=400,
            expose=True,
        )

    config = await find_config(session, tenant_id=tenant_id, channel=channel)
    if config is None:
        config = ChannelConfig(
            tenant_id=tenant_id,
            channel=channel,
            enabled=True if enabled is None else enabled,
            external_account_id=external_account_id or "",
            credentials=credentials or {},
            rate_limit=60 if rate_limit is None else rate_limit,
            webhook_secret=webhook_secret or "",
        )
        session.add(config)
    else:
        if enabled is not None:
            config.enabled = enabled
        if external_account_id is not None:
            config.external_account_id = external_account_id
        if credentials is not None:
            config.credentials = credentials
        if rate_limit is not None:
            config.rate_limit = rate_limit
        if webhook_secret is not None:
            config.webhook_secret = webhook_secret

    try:
        await session.commit()
    except IntegrityError as exc:
        # (channel, external_account_id) 全局唯一：一个公众号/机器人只能绑定一个租户，
        # 否则 webhook 无法确定租户。这里翻译成人话，而不是把数据库异常抛给运维
        await session.rollback()
        raise ConflictError(
            "该平台账号已绑定到其他租户：一个公众号/机器人/邮箱只能接入一个租户"
        ) from exc
    await session.refresh(config)
    return config


# ---------------------------------------------------------------- 入站


async def intake(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel: str,
    inbound: InboundMessage,
) -> IntakeResult:
    """落库（幂等）+ 定位会话。调用前必须已 set_current_tenant(tenant_id)。"""
    external_msg_id = inbound.external_msg_id[:255]
    if not external_msg_id:
        # 平台没给消息 ID 时绝不能用随机值：那样重推必然重复回复
        raise AppError(
            code="CHANNEL_MESSAGE_ID_MISSING",
            message=f"{channel} 回调缺少消息 ID，无法保证幂等",
            status_code=400,
            expose=True,
        )

    non_text = inbound.message_type != "text" or not inbound.content.strip()
    values = {
        "id": uuid.uuid4(),
        "tenant_id": tenant_id,
        "channel": channel,
        "external_msg_id": external_msg_id,
        "direction": DIRECTION_INBOUND,
        "status": MSG_STATUS_SKIPPED if non_text else MSG_STATUS_RECEIVED,
        "external_user_id": inbound.external_user_id[:255],
        "content": inbound.content,
        "reply_context": dict(inbound.reply_context),
    }
    inserted = (
        await session.execute(
            pg_insert(ChannelMessage)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["tenant_id", "channel", "external_msg_id"])
            .returning(ChannelMessage.id)
        )
    ).scalar_one_or_none()

    if inserted is None:
        await session.commit()
        existing = await load_message(
            session, tenant_id=tenant_id, channel=channel, external_msg_id=external_msg_id
        )
        logger.info(
            "channel_inbound_duplicate", channel=channel, external_msg_id=external_msg_id
        )
        # 返回原记录状态而不是固定的 "duplicate"：平台重推 + 上次失败时
        # 调用方需要能重新入队重试，否则这条消息就永久卡在 failed 上
        return IntakeResult(
            status=existing.status if existing else MSG_STATUS_FAILED,
            conversation_id=existing.conversation_id if existing else None,
            created=False,
            reason="平台重推的同一条消息",
        )

    await session.commit()

    if non_text:
        # 图片/语音/事件推送本期不处理，显式标记 skipped（M5 语音在此扩展）
        logger.info(
            "channel_inbound_skipped",
            channel=channel,
            message_type=inbound.message_type,
        )
        return IntakeResult(
            status=MSG_STATUS_SKIPPED,
            conversation_id=None,
            created=True,
            reason=f"暂不支持的消息类型：{inbound.message_type}",
        )

    identity = await ensure_identity(
        session, tenant_id=tenant_id, channel=channel, inbound=inbound
    )
    conversation = await find_or_create_conversation(
        session,
        tenant_id=tenant_id,
        channel=channel,
        identity=identity,
        conversation_ref=inbound.conversation_ref,
        content=inbound.content,
    )
    await session.execute(
        update(ChannelMessage)
        .where(ChannelMessage.id == inserted)
        .values(conversation_id=conversation.id)
    )
    await session.commit()
    return IntakeResult(
        status=MSG_STATUS_RECEIVED, conversation_id=conversation.id, created=True
    )


async def ensure_identity(
    session: AsyncSession, *, tenant_id: uuid.UUID, channel: str, inbound: InboundMessage
) -> ChannelIdentity:
    """外部用户 → 内部 guest 会话的映射（不存在则创建）。"""
    identity = (
        await session.execute(
            select(ChannelIdentity).where(
                ChannelIdentity.tenant_id == tenant_id,
                ChannelIdentity.channel == channel,
                ChannelIdentity.external_user_id == inbound.external_user_id[:255],
            )
        )
    ).scalar_one_or_none()

    if identity is None:
        guest = await guest_service.start_session(session, tenant_id=tenant_id)
        await session.execute(
            pg_insert(ChannelIdentity)
            .values(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                channel=channel,
                external_user_id=inbound.external_user_id[:255],
                guest_id=guest.guest_id,
                display_name=inbound.display_name[:200],
            )
            .on_conflict_do_nothing(
                index_elements=["tenant_id", "channel", "external_user_id"]
            )
        )
        await session.commit()
        identity = (
            await session.execute(
                select(ChannelIdentity).where(
                    ChannelIdentity.tenant_id == tenant_id,
                    ChannelIdentity.channel == channel,
                    ChannelIdentity.external_user_id == inbound.external_user_id[:255],
                )
            )
        ).scalar_one()
        # 并发建号时，本进程刚建的 guest 可能成为孤儿：无会话引用，
        # 到期由清理任务回收，不影响正确性（不能在这里删，可能已被并发方引用）

    return await _ensure_guest_alive(session, tenant_id=tenant_id, identity=identity)


async def _ensure_guest_alive(
    session: AsyncSession, *, tenant_id: uuid.UUID, identity: ChannelIdentity
) -> ChannelIdentity:
    """渠道用户是长期存在的，而 guest 会话有 TTL。

    过期或被合并后必须换新的归属，否则新消息会写进一段作废的会话。
    """
    settings = get_settings()
    now = datetime.now(UTC)

    if identity.guest_id is not None:
        guest = await session.get(GuestSession, identity.guest_id)
        if guest is not None and guest.status == GUEST_STATUS_ACTIVE:
            if guest.expires_at - now > GUEST_RENEW_THRESHOLD:
                return identity
            guest.expires_at = now + timedelta(days=settings.guest_session_ttl_days)
            guest.updated_at = now
            await session.commit()
            return identity

    guest = await guest_service.start_session(session, tenant_id=tenant_id)
    identity.guest_id = guest.guest_id
    await session.commit()
    return identity


async def find_or_create_conversation(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel: str,
    identity: ChannelIdentity,
    conversation_ref: str,
    content: str,
) -> Conversation:
    """定位会话：显式引用 > 同渠道最近会话 > 新建。

    邮件主题里的 `[#xxxxxxxx]` 是显式引用：同一发件人可能同时开多个工单，
    只用"最近会话"必然串话题。引用命中失败（会话被删）时直接新建，
    不回退到最近会话——宁可多一个会话，也不要把两个话题揉在一起。
    """
    if identity.guest_id is None:
        raise AppError(
            code="CHANNEL_IDENTITY_INVALID",
            message="渠道用户身份缺少会话归属",
            status_code=500,
        )

    owner = ConversationOwner(
        tenant_id=tenant_id, owner_type=OWNER_TYPE_GUEST, guest_id=identity.guest_id
    )

    if conversation_ref:
        referenced = (
            await session.execute(
                select(Conversation)
                .where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.guest_id == identity.guest_id,
                    Conversation.channel == channel,
                    cast(Conversation.id, Text).like(f"{conversation_ref}%"),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if referenced is not None:
            return referenced
        return await create_conversation(
            session, owner=owner, title=content.strip()[:50], channel=channel
        )

    latest = (
        await session.execute(
            select(Conversation)
            .where(
                Conversation.tenant_id == tenant_id,
                Conversation.guest_id == identity.guest_id,
                Conversation.channel == channel,
            )
            .order_by(Conversation.updated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is not None:
        return latest

    return await create_conversation(
        session, owner=owner, title=content.strip()[:50], channel=channel
    )


async def load_message(
    session: AsyncSession, *, tenant_id: uuid.UUID, channel: str, external_msg_id: str
) -> ChannelMessage | None:
    return (
        await session.execute(
            select(ChannelMessage).where(
                ChannelMessage.tenant_id == tenant_id,
                ChannelMessage.channel == channel,
                ChannelMessage.external_msg_id == external_msg_id,
            )
        )
    ).scalar_one_or_none()


async def list_messages(
    session: AsyncSession, *, tenant_id: uuid.UUID, conversation_id: uuid.UUID, limit: int = 50
) -> list[ChannelMessage]:
    return list(
        (
            await session.execute(
                select(ChannelMessage)
                .where(
                    ChannelMessage.tenant_id == tenant_id,
                    ChannelMessage.conversation_id == conversation_id,
                )
                .order_by(ChannelMessage.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


async def mark_status(
    session: AsyncSession, record: ChannelMessage, status: str, error_msg: str = ""
) -> None:
    record.status = status
    record.error_msg = error_msg[:500]
    await session.commit()


async def fail_message(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel: str,
    external_msg_id: str,
    message: str,
) -> None:
    """按幂等键标记失败（入队失败等发生在 intake 之后的异常用）。"""
    record = await load_message(
        session, tenant_id=tenant_id, channel=channel, external_msg_id=external_msg_id
    )
    if record is None:
        return
    await mark_status(session, record, MSG_STATUS_FAILED, message)


# ---------------------------------------------------------------- 处理与投递


async def process_inbound(
    *,
    tenant_id: uuid.UUID,
    channel: str,
    external_msg_id: str,
    router: ChannelRouter,
    llm: LLMClient,
    embedder: EmbeddingClient | None = None,
    reranker: RerankClient | None = None,
) -> str:
    """生成回复并通过 ChannelRouter 投递（worker 与邮件轮询共用）。

    出站统一走 router，而不是直接持有某个适配器：坐席手动回复、失败重投
    将来都从同一个出口发出，渠道限频/错误映射只实现一次。
    返回最终状态，便于日志与断言。
    """
    set_current_tenant(tenant_id)
    async with async_session_factory() as session:
        record = await load_message(
            session, tenant_id=tenant_id, channel=channel, external_msg_id=external_msg_id
        )
        if record is None:
            logger.warning("channel_record_missing", channel=channel, msg_id=external_msg_id)
            return "missing"

        if record.status in (MSG_STATUS_REPLIED, MSG_STATUS_SKIPPED):
            return record.status

        if record.status == MSG_STATUS_PROCESSING and not _stale(record.updated_at):
            # 另一个 worker 正在处理：重复任务直接退出，避免双重回复
            logger.info("channel_message_in_progress", channel=channel, msg_id=external_msg_id)
            return MSG_STATUS_PROCESSING

        if record.conversation_id is None:
            await mark_status(session, record, MSG_STATUS_FAILED, "缺少会话关联：入站解析未完成")
            return MSG_STATUS_FAILED

        record.status = MSG_STATUS_PROCESSING
        await session.commit()

        outbound = await _get_or_create_outbound(
            session, tenant_id=tenant_id, channel=channel, record=record
        )
        if outbound.status == MSG_STATUS_REPLIED:
            # 上次已投递成功、只是入站状态没回写：补状态即可，不重复发送
            await mark_status(session, record, MSG_STATUS_REPLIED)
            return MSG_STATUS_REPLIED

        if not outbound.content:
            reply: chat_service.ReplyResult | None = None
            failed_reason = ""
            failed_code = ""
            try:
                reply = await chat_service.collect_reply(
                    tenant_id=tenant_id,
                    conversation_id=record.conversation_id,
                    content=record.content,
                    llm=llm,
                    embedder=embedder,
                    reranker=reranker,
                    source=channel,
                )
            except chat_service.ReplyFailedError as exc:
                failed_reason, failed_code = exc.message, exc.code
            except AppError as exc:
                # 模型未配置等配置类错误：明确失败，等修好配置后重试
                failed_reason, failed_code = exc.message, exc.code
            finally:
                # stream_reply 收尾会清空租户上下文（它自己开 session），
                # 这里必须恢复：否则本会话后续 UPDATE 会被 RLS 过滤成 0 行，
                # 表现为 StaleDataError 或状态永远不更新
                await activate_tenant(session, tenant_id)

            if reply is None:
                await mark_status(session, record, MSG_STATUS_FAILED, failed_reason)
                logger.warning(
                    "channel_reply_failed",
                    channel=channel,
                    msg_id=external_msg_id,
                    code=failed_code,
                )
                return MSG_STATUS_FAILED

            outbound.content = reply.content
            await session.commit()

        try:
            platform_message_id = await router.deliver(
                channel=channel,
                conversation_id=record.conversation_id,
                external_user_id=record.external_user_id,
                content=outbound.content,
                reply_context=dict(record.reply_context or {}),
            )
        except ChannelError as exc:
            await mark_status(session, outbound, MSG_STATUS_FAILED, exc.message)
            await mark_status(session, record, MSG_STATUS_FAILED, exc.message)
            logger.warning(
                "channel_send_failed",
                channel=channel,
                msg_id=external_msg_id,
                code=exc.code,
                message=exc.message,
            )
            return MSG_STATUS_FAILED

        await mark_status(session, outbound, MSG_STATUS_REPLIED)
        await mark_status(session, record, MSG_STATUS_REPLIED)
        # 平台消息 ID 只记日志：它不参与幂等（幂等键是本地的确定性流水号），
        # 需要审计时从日志检索即可，不必为它加列
        logger.info(
            "channel_replied",
            channel=channel,
            msg_id=external_msg_id,
            platform_message_id=platform_message_id,
        )
        return MSG_STATUS_REPLIED


async def _get_or_create_outbound(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel: str,
    record: ChannelMessage,
) -> ChannelMessage:
    """出站流水（键固定为 out-{入站记录 id}）：既做"已发过"标记，也缓存回复全文。"""
    key = f"out-{record.id}"
    existing = await load_message(
        session, tenant_id=tenant_id, channel=channel, external_msg_id=key
    )
    if existing is not None:
        return existing

    outbound = ChannelMessage(
        tenant_id=tenant_id,
        channel=channel,
        external_msg_id=key,
        conversation_id=record.conversation_id,
        direction=DIRECTION_OUTBOUND,
        status=MSG_STATUS_RECEIVED,
        external_user_id=record.external_user_id,
        content="",
        reply_context=dict(record.reply_context or {}),
    )
    session.add(outbound)
    await session.commit()
    return outbound


def _stale(updated_at: datetime) -> bool:
    """processing 状态是否已超时（worker 崩溃后允许重新处理）。"""
    reference = updated_at if updated_at.tzinfo else updated_at.replace(tzinfo=UTC)
    return (datetime.now(UTC) - reference).total_seconds() > PROCESSING_TIMEOUT_SECONDS


__all__ = [
    "IntakeResult",
    "ensure_identity",
    "fail_message",
    "find_config",
    "find_or_create_conversation",
    "get_config",
    "intake",
    "list_channel_accounts",
    "list_configs",
    "list_messages",
    "load_message",
    "mark_status",
    "process_inbound",
    "resolve_tenant_id",
    "upsert_config",
]
