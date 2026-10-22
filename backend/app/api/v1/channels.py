"""渠道路由（设计文档 7 / 9.1）。

两类接口严格分开，鉴权方式完全不同：

- 入站 webhook：**没有登录态**，靠平台签名（微信 sha1 / 钉钉 HMAC）或共享密钥
  验签；只做"验签 → 解析 → 落库 → 入队"，秒级响应，AI 生在 worker 里
- 租户管理：要求 owner/admin，管开关、平台账号标识与限频

租户解析顺序（webhook）：报文里的平台账号 → `channel_configs` 反查 tenant_id
→ `set_current_tenant`，之后所有查询自动被 RLS 收敛到该租户。
"""

import uuid
from typing import Any

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant_member, provide_job_queue, require_tenant_admin
from app.channels import build_adapter, credentials_hint, extract_account_id
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.core.tenant import require_current_tenant_id
from app.db.models.channel import (
    ALL_CHANNELS,
    CHANNEL_WECHAT,
    MSG_STATUS_FAILED,
    MSG_STATUS_RECEIVED,
    ChannelConfig,
)
from app.db.models.tenant import TenantMember
from app.db.session import get_session
from app.schemas.channel import (
    ChannelConfigInfo,
    ChannelConfigUpdate,
    ChannelMessageInfo,
    ChannelMessageListResponse,
)
from app.services import channels as channel_service

router = APIRouter(prefix="/channels", tags=["channels"])

logger = get_logger("api.channels")

# 平台回调都是小报文；限制上限避免畸形请求占满内存（与适配器内部检查一致）
MAX_BODY_BYTES = 1024 * 1024


def _header_map(request: Request) -> dict[str, str]:
    """header 名统一小写：各平台大小写习惯不同，比较时必须归一。"""
    return {key.lower(): value for key, value in request.headers.items()}


async def _read_body(request: Request) -> bytes:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise AppError(
            code="CHANNEL_PAYLOAD_TOO_LARGE",
            message="回调报文超过 1MB 上限",
            status_code=413,
            expose=True,
        )
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise AppError(
            code="CHANNEL_PAYLOAD_TOO_LARGE",
            message="回调报文超过 1MB 上限",
            status_code=413,
            expose=True,
        )
    return body


@router.post("/{channel}/webhook")
async def inbound_webhook(
    channel: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    queue: ArqRedis = Depends(provide_job_queue),
) -> Response:
    """统一入站入口（设计文档 9.1）。

    顺序不可调整：必须先反查租户（才能拿到该租户的 webhook_secret/凭证），
    再验签，再解析。反查本身无副作用，且 404 与 403 的区分只会泄露
    "某账号是否已接入"，不构成可利用信息。
    """
    body = await _read_body(request)
    headers = _header_map(request)
    query = dict(request.query_params)

    # 邮件等无平台签名的渠道由调用方用 ?account= 指定；微信/钉钉从报文里提取
    account = query.get("account", "").strip() or extract_account_id(channel, body)
    tenant_id = await channel_service.resolve_tenant_id(
        session, channel=channel, external_account_id=account
    )
    await channel_service.activate_tenant(session, tenant_id)

    config = await channel_service.get_config(session, tenant_id=tenant_id, channel=channel)
    adapter = build_adapter(channel, webhook_secret=config.webhook_secret)
    adapter.verify(query=query, headers=headers, body=body)
    inbound = adapter.parse_inbound(body, headers=headers)

    result = await channel_service.intake(
        session, tenant_id=tenant_id, channel=channel, inbound=inbound
    )
    # 入队条件：新消息，或平台重推时上一次处理失败（需要重试）。
    # 重推且仍在 received/processing 时不再入队——已经有一个任务在路上了，
    # 重复入队只会让同一条消息被并发处理
    should_enqueue = (result.created and result.status == MSG_STATUS_RECEIVED) or (
        not result.created and result.status == MSG_STATUS_FAILED
    )
    if should_enqueue:
        await _enqueue_processing(
            queue,
            session,
            tenant_id=tenant_id,
            channel=channel,
            external_msg_id=inbound.external_msg_id,
        )

    ack = adapter.ack(inbound)
    return Response(content=ack.body, media_type=ack.media_type)


@router.get("/{channel}/webhook")
async def verify_webhook(
    channel: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """微信服务器配置校验（GET + echostr）。

    GET 请求没有报文，无法从报文里取账号，因此回调地址必须带 `?account=gh_xxx`。
    """
    if channel != CHANNEL_WECHAT:
        raise AppError(
            code="METHOD_NOT_ALLOWED",
            message="该渠道不支持 GET 校验",
            status_code=405,
            expose=True,
        )

    query = dict(request.query_params)
    account = query.get("account", "").strip()
    tenant_id = await channel_service.resolve_tenant_id(
        session, channel=channel, external_account_id=account
    )
    await channel_service.activate_tenant(session, tenant_id)

    config = await channel_service.get_config(session, tenant_id=tenant_id, channel=channel)
    adapter = build_adapter(channel, webhook_secret=config.webhook_secret)
    adapter.verify(query=query, headers=_header_map(request), body=b"")
    echo = getattr(adapter, "echo", None)
    if echo is None:
        raise AppError(code="METHOD_NOT_ALLOWED", message="该渠道不支持 GET 校验", status_code=405)
    return Response(content=echo(query=query), media_type="text/plain")


async def _enqueue_processing(
    queue: ArqRedis,
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel: str,
    external_msg_id: str,
) -> None:
    """入队失败必须显式 503：平台会重推，而幂等键保证重推不会重复回复。"""
    try:
        await queue.enqueue_job(
            "process_channel_inbound",
            str(tenant_id),
            channel,
            external_msg_id,
            # 随机后缀：失败重试需要新 job，固定 job_id 会被 arq 以"已存在"挡掉
            _job_id=f"ch-{channel}-{uuid.uuid4().hex[:12]}",
        )
    except Exception as exc:  # noqa: BLE001 - Redis 不可用是运维场景，必须给出可操作提示
        logger.warning(
            "channel_enqueue_failed",
            channel=channel,
            msg_id=external_msg_id,
            error=str(exc),
        )
        await channel_service.fail_message(
            session,
            tenant_id=tenant_id,
            channel=channel,
            external_msg_id=external_msg_id,
            message="任务入队失败：请检查 Redis 与 worker 是否运行",
        )
        raise AppError(
            code="QUEUE_UNAVAILABLE",
            message="渠道消息入队失败：请确认 Redis 已启动、arq worker 正在运行",
            status_code=503,
            expose=True,
        ) from exc


# ---------------------------------------------------------------- 租户管理


def _config_info(channel: str, config: ChannelConfig | None) -> ChannelConfigInfo:
    hint = credentials_hint(channel)
    if config is None:
        info = ChannelConfigInfo.empty(channel)
        info.configured = hint.configured
        info.missing = list(hint.missing)
        return info
    return ChannelConfigInfo(
        channel=config.channel,
        enabled=config.enabled,
        external_account_id=config.external_account_id,
        credentials=dict(config.credentials),
        rate_limit=config.rate_limit,
        configured=hint.configured,
        missing=list(hint.missing),
        webhook_path=f"/api/v1/channels/{config.channel}/webhook",
    )


@router.get("", response_model=list[ChannelConfigInfo])
async def list_channels(
    member: TenantMember = Depends(get_current_tenant_member),
    session: AsyncSession = Depends(get_session),
) -> list[ChannelConfigInfo]:
    """全部渠道的配置与可用状态（未落库的渠道返回占位行）。"""
    tenant_id = require_current_tenant_id()
    configs = {
        config.channel: config
        for config in await channel_service.list_configs(session, tenant_id=tenant_id)
    }
    return [_config_info(channel, configs.get(channel)) for channel in ALL_CHANNELS]


@router.put("/{channel}", response_model=ChannelConfigInfo)
async def upsert_channel(
    channel: str,
    payload: ChannelConfigUpdate,
    member: TenantMember = Depends(require_tenant_admin),
    session: AsyncSession = Depends(get_session),
) -> ChannelConfigInfo:
    """新建或更新渠道配置。`configured=false` 表示环境变量凭证缺失，渠道实际不可用。"""
    tenant_id = require_current_tenant_id()
    config = await channel_service.upsert_config(
        session,
        tenant_id=tenant_id,
        channel=channel,
        enabled=payload.enabled,
        external_account_id=payload.external_account_id,
        credentials=_as_str_map(payload.credentials),
        rate_limit=payload.rate_limit,
        webhook_secret=payload.webhook_secret,
    )
    return _config_info(channel, config)


@router.get("/{channel}/messages", response_model=ChannelMessageListResponse)
async def list_channel_messages(
    channel: str,
    conversation_id: uuid.UUID = Query(description="会话 ID（必填，避免一次拉全租户流水）"),
    limit: int = Query(default=50, ge=1, le=200),
    member: TenantMember = Depends(get_current_tenant_member),
    session: AsyncSession = Depends(get_session),
) -> ChannelMessageListResponse:
    """渠道收发流水：排查"平台重推/投递失败"的第一手材料。"""
    tenant_id = require_current_tenant_id()
    records = await channel_service.list_messages(
        session, tenant_id=tenant_id, conversation_id=conversation_id, limit=limit
    )
    return ChannelMessageListResponse(
        items=[ChannelMessageInfo.model_validate(record) for record in records]
    )


def _as_str_map(value: dict[str, Any] | None) -> dict[str, object] | None:
    """credentials 只允许扁平键值：嵌套结构对适配器无意义，还会撑大 JSONB。"""
    if value is None:
        return None
    return {str(key): value for key, value in value.items()}
