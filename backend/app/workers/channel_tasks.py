"""渠道异步任务（设计文档 9.1 / 12）。

两个任务，动机不同：

- `process_channel_inbound`：webhook 落库后生成回复并投递。
  微信/钉钉要求 5s 内响应，AI 生成通常 2-10s，只能异步。
- `poll_email_channels`：邮件没有回调，只能 IMAP 轮询（设计文档 3.1）。

失败语义：任务异常由 arq 记录并重试；状态回写保证"卡在 processing"的
记录在超时后能被重新处理（见 channel_service.PROCESSING_TIMEOUT_SECONDS）。
"""

import uuid
from typing import Any

from app.channels import ChannelRouter, build_adapter
from app.channels.base import ChannelAdapter
from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.core.tenant import set_current_tenant
from app.db.models.channel import CHANNEL_EMAIL, MSG_STATUS_RECEIVED
from app.db.session import async_session_factory
from app.llm.base import EmbeddingClient, LLMClient, LLMNotConfiguredError, RerankClient
from app.llm.factory import get_embedding_client, get_llm_client, get_rerank_client
from app.services import channels as channel_service

logger = get_logger("worker.channels")


def _build_llm() -> LLMClient | None:
    """模型未配置时返回 None：由调用方标记失败，而不是让任务反复重试。"""
    try:
        return get_llm_client()
    except LLMNotConfiguredError as exc:
        logger.warning("channel_llm_not_configured", missing=exc.missing)
        return None


def _build_optional_clients() -> tuple[EmbeddingClient | None, RerankClient | None]:
    try:
        embedder: EmbeddingClient | None = get_embedding_client()
    except LLMNotConfiguredError as exc:
        # 检索可降级：没有嵌入模型就纯模型回答（与站内链路行为一致）
        logger.warning("channel_embedding_not_configured", missing=exc.missing)
        embedder = None
    return embedder, get_rerank_client()


async def _mark_failed(
    tenant_id: uuid.UUID, channel: str, external_msg_id: str, message: str
) -> None:
    """把失败落到流水上：静默返回会让记录永远停在 received，只能靠翻日志发现。"""
    async with async_session_factory() as session:
        await channel_service.fail_message(
            session,
            tenant_id=tenant_id,
            channel=channel,
            external_msg_id=external_msg_id,
            message=message,
        )


async def process_channel_inbound(
    ctx: dict[str, Any], tenant_id: str, channel: str, external_msg_id: str
) -> None:
    """生成回复并投递到渠道（webhook 入队的任务）。"""
    tenant_uuid = uuid.UUID(tenant_id)
    set_current_tenant(tenant_uuid)

    llm = _build_llm()
    if llm is None:
        await _mark_failed(
            tenant_uuid,
            channel,
            external_msg_id,
            "AI 模型未配置：请设置 AI_API_KEY / AI_BASE_URL / AI_MODEL",
        )
        return

    router = ChannelRouter()
    try:
        # 预检凭证：渠道未配置时不必白花一次模型调用
        router.adapter_for(channel)
    except AppError as exc:
        await _mark_failed(tenant_uuid, channel, external_msg_id, exc.message)
        logger.warning(
            "channel_adapter_unavailable",
            channel=channel,
            msg_id=external_msg_id,
            code=exc.code,
        )
        return

    embedder, reranker = _build_optional_clients()
    await channel_service.process_inbound(
        tenant_id=tenant_uuid,
        channel=channel,
        external_msg_id=external_msg_id,
        router=router,
        llm=llm,
        embedder=embedder,
        reranker=reranker,
    )


async def poll_email_channels(ctx: dict[str, Any]) -> None:
    """IMAP 轮询：拉未读 → 按收件地址路由租户 → 幂等入站 → 生成并回信。

    没配置 IMAP 时静默跳过（每个周期记一行 info 即可）：邮件是可选渠道，
    缺凭证不应刷错误日志。
    """
    settings = get_settings()
    if not (settings.imap_host and settings.imap_username and settings.imap_password):
        logger.info("email_poll_skipped", reason="IMAP 未配置")
        return

    async with async_session_factory() as session:
        accounts = await channel_service.list_channel_accounts(session, CHANNEL_EMAIL)
    if not accounts:
        return

    adapter = build_adapter(CHANNEL_EMAIL)
    try:
        raw_messages = await _fetch(adapter, settings.email_poll_batch)
    except Exception:  # noqa: BLE001 - 邮件服务商/网络异常必须留痕，不能静默空转
        logger.exception("email_fetch_failed")
        return

    if not raw_messages:
        return

    llm = _build_llm()
    if llm is None:
        # 不 mark_seen：配置修好后这些未读邮件还能补处理
        logger.warning("email_poll_deferred", reason="AI 模型未配置", count=len(raw_messages))
        return

    embedder, reranker = _build_optional_clients()
    router = ChannelRouter()
    processed = 0
    for raw in raw_messages:
        try:
            inbound = adapter.parse_inbound(raw, headers={})
        except AppError as exc:
            # 单封解析失败不能中断整批（可能是纯附件邮件/恶意报文）
            logger.warning("email_parse_failed", code=exc.code, message=exc.message)
            continue
        if not inbound.account_id:
            logger.warning("email_recipient_missing")
            continue

        tenant_uuid = await _resolve_tenant(inbound.account_id)
        if tenant_uuid is None:
            continue

        set_current_tenant(tenant_uuid)
        async with async_session_factory() as session:
            result = await channel_service.intake(
                session, tenant_id=tenant_uuid, channel=CHANNEL_EMAIL, inbound=inbound
            )
        if result.status != MSG_STATUS_RECEIVED:
            continue

        await channel_service.process_inbound(
            tenant_id=tenant_uuid,
            channel=CHANNEL_EMAIL,
            external_msg_id=inbound.external_msg_id,
            router=router,
            llm=llm,
            embedder=embedder,
            reranker=reranker,
        )
        processed += 1

    # 只标记本批已处理过的邮件：整批 mark_seen 会把解析失败的也吃掉
    if processed:
        mark_seen = getattr(adapter, "mark_seen", None)
        if mark_seen is not None:
            try:
                await mark_seen(raw_messages)
            except Exception:  # noqa: BLE001 - 已读标记失败不影响回复，幂等键兜底
                logger.exception("email_mark_seen_failed")

    logger.info("email_polled", fetched=len(raw_messages), processed=processed)


async def _fetch(adapter: ChannelAdapter, limit: int) -> list[bytes]:
    """fetch_unseen 只存在于邮件适配器，用 getattr 避免把该能力塞进通用契约。"""
    fetch = getattr(adapter, "fetch_unseen", None)
    if fetch is None:
        return []
    return list(await fetch(limit))


async def _resolve_tenant(account: str) -> uuid.UUID | None:
    async with async_session_factory() as session:
        try:
            return await channel_service.resolve_tenant_id(
                session, channel=CHANNEL_EMAIL, external_account_id=account
            )
        except AppError:
            # 收件地址未接入任何租户：可能是同一个 IMAP 信箱里的其他业务邮件
            logger.info("email_account_unrouted", account=account)
            return None


__all__ = ["poll_email_channels", "process_channel_inbound"]
