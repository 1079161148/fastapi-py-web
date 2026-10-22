"""坐席工作台路由（M6，设计文档 5.3 / 5.4 / 7）。

鉴权差异：
- 普通接口用 Bearer（axios 正常带）
- 通知流 **不用 EventSource**：浏览器原生 EventSource 无法附加 Authorization，
  只能把 access token 塞进 URL，那会把令牌写进访问日志与代理日志。
  这里改用 fetch + ReadableStream（前端复用已有的 SSE 解析），
  重连由前端显式实现（带退避），换来的是凭证不出现在 URL 里。

降级：SSE 不可用时前端轮询 `GET /agent/tickets` 与 `GET /agent/conversations`，
两个接口都是普通 REST，天然支持轮询。
"""

import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import require_agent
from app.api.v1.presenters import message_info, realtime_frames
from app.channels import ChannelRouter
from app.core.config import get_settings
from app.core.pubsub import agent_channel, open_subscription
from app.core.tenant import require_current_tenant_id
from app.db.models.tenant import TenantMember
from app.db.session import get_session
from app.schemas.agent import (
    AgentConversationInfo,
    AgentConversationListResponse,
    AgentReplyRequest,
    AgentReplyResult,
    AgentTicketListResponse,
)
from app.schemas.handoff import HandoffTicketInfo
from app.schemas.leave_message import LeaveMessageInfo, LeaveMessageListResponse
from app.services import agent as agent_service
from app.services import handoff as handoff_service
from app.services import leave_message as leave_service

router = APIRouter(prefix="/agent", tags=["agent"])

settings = get_settings()
# 强制结束别人的工单需要租户级管理角色
FORCE_RESOLVE_ROLES = ("owner", "admin")


@router.get("/notifications/stream")
async def stream_notifications(
    member: TenantMember = Depends(require_agent),
) -> StreamingResponse:
    """坐席通知流（设计文档 5.4）：工单新建/认领/结束、访客新消息、访客登录。

    Redis 建连放在返回响应之前：连不上直接 503，而不是给前端一个
    永远收不到消息的 200 流（那种故障最难排查）。
    """
    tenant_id = require_current_tenant_id()
    subscription = await open_subscription(agent_channel(tenant_id))

    async def event_stream() -> AsyncIterator[str]:
        try:
            yield f'event: ready\ndata: {{"tenant_id": "{tenant_id}"}}\n\n'
            async for frame in realtime_frames(
                subscription,
                heartbeat_seconds=settings.agent_stream_heartbeat_seconds,
                accept=lambda event: event.tenant_id == tenant_id,
            ):
                yield frame
        finally:
            await subscription.close()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/tickets", response_model=AgentTicketListResponse)
async def list_tickets(
    status: str | None = Query(
        default=None, description="逗号分隔：pending,claimed,resolved,cancelled"
    ),
    mine: bool = Query(default=False, description="只看我认领的"),
    limit: int = Query(default=agent_service.DEFAULT_LIMIT, ge=1, le=agent_service.MAX_LIMIT),
    cursor: str | None = Query(default=None),
    member: TenantMember = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
) -> AgentTicketListResponse:
    """工单队列：默认全部状态、按创建时间正序（早来的先处理）。"""
    tenant_id = require_current_tenant_id()
    statuses = [item.strip() for item in status.split(",") if item.strip()] if status else None
    items, next_cursor = await handoff_service.list_tickets(
        session,
        tenant_id=tenant_id,
        statuses=statuses,
        agent_id=member.user_id if mine else None,
        limit=limit,
        cursor=cursor,
    )
    return AgentTicketListResponse(
        items=[HandoffTicketInfo.model_validate(item) for item in items],
        next_cursor=next_cursor,
    )


@router.post("/tickets/{ticket_id}/claim", response_model=HandoffTicketInfo)
async def claim_ticket(
    ticket_id: uuid.UUID,
    member: TenantMember = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
) -> HandoffTicketInfo:
    """认领工单：并发下只有一个坐席成功，其余拿到 409（不是静默抢走）。"""
    tenant_id = require_current_tenant_id()
    ticket = await handoff_service.claim_ticket(
        session, tenant_id=tenant_id, ticket_id=ticket_id, agent_id=member.user_id
    )
    return HandoffTicketInfo.model_validate(ticket)


@router.post("/tickets/{ticket_id}/resolve", response_model=HandoffTicketInfo)
async def resolve_ticket(
    ticket_id: uuid.UUID,
    member: TenantMember = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
) -> HandoffTicketInfo:
    """结束工单：会话交还 AI（用户接着问不用重新开窗口）。"""
    tenant_id = require_current_tenant_id()
    ticket = await handoff_service.resolve_ticket(
        session,
        tenant_id=tenant_id,
        ticket_id=ticket_id,
        agent_id=member.user_id,
        force=member.role in FORCE_RESOLVE_ROLES,
    )
    return HandoffTicketInfo.model_validate(ticket)


@router.get("/conversations", response_model=AgentConversationListResponse)
async def list_conversations(
    status: str | None = Query(
        default=None, description="逗号分隔：ai,pending_handoff,human,closed"
    ),
    channel: str | None = Query(
        default=None, description="按来源渠道筛选：web/email/wechat/dingtalk"
    ),
    owner_type: str | None = Query(default=None, description="按主体筛选：user/guest"),
    limit: int = Query(default=agent_service.DEFAULT_LIMIT, ge=1, le=agent_service.MAX_LIMIT),
    cursor: str | None = Query(default=None),
    member: TenantMember = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
) -> AgentConversationListResponse:
    """会话列表（默认 pending_handoff + human），附进行中的工单。

    后台管理可传 `status=ai,pending_handoff,human,closed` 拿全量，
    再按 `channel` / `owner_type` 收窄。
    """
    tenant_id = require_current_tenant_id()
    statuses = tuple(item.strip() for item in status.split(",") if item.strip()) if status else None
    items, next_cursor = await agent_service.list_conversations(
        session,
        tenant_id=tenant_id,
        statuses=statuses,
        limit=limit,
        cursor=cursor,
        channel=channel,
        owner_type=owner_type,
    )
    return AgentConversationListResponse(
        items=[
            AgentConversationInfo(
                id=item.conversation.id,
                title=item.conversation.title,
                channel=item.conversation.channel,
                owner_type=item.conversation.owner_type,
                status=item.conversation.status,
                updated_at=item.conversation.updated_at,
                ticket=(
                    HandoffTicketInfo.model_validate(item.ticket) if item.ticket else None
                ),
            )
            for item in items
        ],
        next_cursor=next_cursor,
    )


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    conversation_id: uuid.UUID,
    limit: int = Query(default=agent_service.DEFAULT_LIMIT, ge=1, le=agent_service.MAX_LIMIT),
    cursor: str | None = Query(default=None),
    member: TenantMember = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """会话历史（坐席视角，不受 owner 限制，但严格按租户收敛）。"""
    tenant_id = require_current_tenant_id()
    items, next_cursor = await agent_service.list_messages(
        session,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        limit=limit,
        cursor=cursor,
    )
    return {
        "items": [message_info(item).model_dump(mode="json") for item in items],
        "next_cursor": next_cursor,
    }


@router.post("/conversations/{conversation_id}/messages", response_model=AgentReplyResult)
async def reply(
    conversation_id: uuid.UUID,
    payload: AgentReplyRequest,
    member: TenantMember = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
) -> AgentReplyResult:
    """坐席回复：落库 → 广播 → 非 web 渠道回落原渠道（微信/钉钉/邮件）。"""
    tenant_id = require_current_tenant_id()
    result = await agent_service.reply(
        session,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        agent_id=member.user_id,
        content=payload.content,
        router=ChannelRouter(),
    )
    return AgentReplyResult(
        message=message_info(result.message),
        delivered=result.delivered,
        delivery_error=result.delivery_error,
    )


@router.get("/leave-messages", response_model=LeaveMessageListResponse)
async def list_leave_messages(
    status: str | None = Query(default=None, description="new / handled"),
    limit: int = Query(default=leave_service.DEFAULT_LIMIT, ge=1, le=leave_service.MAX_LIMIT),
    cursor: str | None = Query(default=None),
    member: TenantMember = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
) -> LeaveMessageListResponse:
    """离线留言列表（M8）：坐席不在线时用户留下的联系方式与问题。"""
    tenant_id = require_current_tenant_id()
    items, next_cursor = await leave_service.list_messages(
        session, tenant_id=tenant_id, status=status, limit=limit, cursor=cursor
    )
    return LeaveMessageListResponse(
        items=[LeaveMessageInfo.model_validate(item) for item in items],
        next_cursor=next_cursor,
    )


@router.post("/leave-messages/{leave_id}/handle", response_model=LeaveMessageInfo)
async def handle_leave_message(
    leave_id: uuid.UUID,
    member: TenantMember = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
) -> LeaveMessageInfo:
    """标记留言已处理（幂等）。"""
    tenant_id = require_current_tenant_id()
    message = await leave_service.mark_handled(
        session, tenant_id=tenant_id, leave_id=leave_id, agent_id=member.user_id
    )
    return LeaveMessageInfo.model_validate(message)
