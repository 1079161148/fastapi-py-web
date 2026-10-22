"""会话与消息路由（设计文档 7）。

/api 层只做：参数校验 → 调 service → 组装响应；
越权与业务规则全部在 services/conversations.py。
"""

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    enforce_chat_rate,
    get_chat_principal,
    provide_embedding_client,
    provide_llm_client,
    provide_optional_llm_client,
    provide_reranker,
    provide_speech_client,
    provide_transcription_client,
)
from app.api.v1.presenters import message_info, realtime_frames
from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.pubsub import conversation_channel, open_subscription
from app.db.models.conversation import (
    CONVERSATION_STATUS_AI,
    CONVERSATION_STATUS_PENDING_HANDOFF,
    OWNER_TYPE_USER,
    Message,
)
from app.db.models.handoff import REQUESTED_BY_GUEST, REQUESTED_BY_USER
from app.db.session import get_session
from app.llm.base import ROLE_USER, EmbeddingClient, LLMClient, RerankClient
from app.schemas.chat import (
    ConversationCreateRequest,
    ConversationInfo,
    ConversationListResponse,
    ConversationUpdateRequest,
    MessageInfo,
    MessageListResponse,
    SendMessageRequest,
)
from app.schemas.handoff import (
    HandoffCancelResponse,
    HandoffRequestPayload,
    HandoffResponse,
    HandoffTicketInfo,
)
from app.schemas.leave_message import LeaveMessageCreate, LeaveMessageInfo
from app.schemas.support import RatingInfo, RatingRequest
from app.services import assets as asset_service
from app.services import chat as chat_service
from app.services import conversations as conversation_service
from app.services import handoff as handoff_service
from app.services import leave_message as leave_service
from app.services import messages as message_service
from app.services import tenant_settings as tenant_settings_service
from app.services.conversations import (
    DEFAULT_LIST_LIMIT,
    DEFAULT_MESSAGE_LIMIT,
    MAX_LIMIT,
    ConversationOwner,
)
from app.speech import SpeechClient, TranscriptionClient

router = APIRouter(prefix="/conversations", tags=["conversations"])

settings = get_settings()


def _sse_frame(event: chat_service.StreamEvent) -> str:
    """SSE 帧：事件名 + JSON 数据（JSON 内换行已转义，不会破坏帧边界）。"""
    return f"event: {event.event}\ndata: {json.dumps(event.data, ensure_ascii=False)}\n\n"


@router.post("", response_model=ConversationInfo, status_code=201)
async def create_conversation(
    payload: ConversationCreateRequest,
    owner: ConversationOwner = Depends(get_chat_principal),
    session: AsyncSession = Depends(get_session),
) -> ConversationInfo:
    conversation = await conversation_service.create_conversation(
        session, owner=owner, title=payload.title
    )
    return ConversationInfo.model_validate(conversation)


@router.get("", response_model=ConversationListResponse)
async def list_conversations(
    owner: ConversationOwner = Depends(get_chat_principal),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(default=None),
) -> ConversationListResponse:
    items, next_cursor = await conversation_service.list_conversations(
        session, owner=owner, limit=limit, cursor=cursor
    )
    return ConversationListResponse(
        items=[ConversationInfo.model_validate(item) for item in items], next_cursor=next_cursor
    )


@router.get("/{conversation_id}", response_model=ConversationInfo)
async def get_conversation(
    conversation_id: uuid.UUID,
    owner: ConversationOwner = Depends(get_chat_principal),
    session: AsyncSession = Depends(get_session),
) -> ConversationInfo:
    conversation = await conversation_service.get_conversation(
        session, owner=owner, conversation_id=conversation_id
    )
    return ConversationInfo.model_validate(conversation)


@router.patch("/{conversation_id}", response_model=ConversationInfo)
async def update_conversation(
    conversation_id: uuid.UUID,
    payload: ConversationUpdateRequest,
    owner: ConversationOwner = Depends(get_chat_principal),
    session: AsyncSession = Depends(get_session),
) -> ConversationInfo:
    conversation = await conversation_service.update_conversation(
        session,
        owner=owner,
        conversation_id=conversation_id,
        title=payload.title,
        status=payload.status,
        voice_reply_enabled=payload.voice_reply_enabled,
    )
    return ConversationInfo.model_validate(conversation)


@router.get("/{conversation_id}/messages", response_model=MessageListResponse)
async def list_messages(
    conversation_id: uuid.UUID,
    owner: ConversationOwner = Depends(get_chat_principal),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=DEFAULT_MESSAGE_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(default=None),
) -> MessageListResponse:
    items, next_cursor = await conversation_service.list_messages(
        session, owner=owner, conversation_id=conversation_id, limit=limit, cursor=cursor
    )
    return MessageListResponse(
        items=[message_info(item) for item in items], next_cursor=next_cursor
    )


async def _read_audio(file: UploadFile, max_bytes: int) -> bytes:
    """多读 1 字节判断超限：不信任 Content-Length（客户端可伪造）。"""
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise AppError(
            code="AUDIO_TOO_LARGE",
            message=f"音频超过 {settings.voice_max_mb}MB 上限，请缩短录音",
            status_code=413,
        )
    return data


@router.post("/{conversation_id}/messages/audio")
async def stream_audio_message(
    conversation_id: uuid.UUID,
    file: Annotated[UploadFile, File()],
    owner: ConversationOwner = Depends(enforce_chat_rate),
    session: AsyncSession = Depends(get_session),
    llm: LLMClient = Depends(provide_llm_client),
    embedder: EmbeddingClient | None = Depends(provide_embedding_client),
    reranker: RerankClient | None = Depends(provide_reranker),
    transcriber: TranscriptionClient = Depends(provide_transcription_client),
    speech: SpeechClient | None = Depends(provide_speech_client),
    duration_seconds: Annotated[float, Form()] = 0.0,
) -> StreamingResponse:
    """语音消息一轮对话（设计文档 9.3）。

    事件序列：`transcribing` → `message` → `sources` → `delta*` → `done`
    →（会话开了语音回复时）`audio` 或 `audio_failed`。

    multipart 上传而不是预签名直传：单条录音上限 60s/8MB，中转成本可忽略；
    预签名需要对象存储支持（当前 local 后端做不到），两套前端上传路径的
    复杂度远高于收益。真接 MinIO 后再按需切换（见 docs 的取舍记录）。
    """
    conversation = await conversation_service.get_conversation(
        session, owner=owner, conversation_id=conversation_id
    )
    audio = await _read_audio(file, settings.voice_max_mb * 1024 * 1024)
    # 先取出标量再进生成器：响应开始后请求级 session 已关闭
    voice_reply = conversation.voice_reply_enabled

    async def event_stream() -> AsyncIterator[str]:
        async for event in chat_service.stream_audio_reply(
            tenant_id=owner.tenant_id,
            conversation_id=conversation_id,
            audio=audio,
            declared_duration=duration_seconds or None,
            transcriber=transcriber,
            llm=llm,
            embedder=embedder,
            reranker=reranker,
            speech=speech,
            voice_reply=voice_reply,
        ):
            yield _sse_frame(event)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{conversation_id}/messages/stream")
async def stream_message(
    conversation_id: uuid.UUID,
    payload: SendMessageRequest,
    owner: ConversationOwner = Depends(enforce_chat_rate),
    session: AsyncSession = Depends(get_session),
    llm: LLMClient | None = Depends(provide_optional_llm_client),
    embedder: EmbeddingClient | None = Depends(provide_embedding_client),
    reranker: RerankClient | None = Depends(provide_reranker),
    speech: SpeechClient | None = Depends(provide_speech_client),
) -> StreamingResponse:
    """流式对话（设计文档 5.1）：SSE 事件依次为 message → sources → delta* → done/error。

    embedder 允许为 None（未配置嵌入模型）：检索层会降级为纯模型回答，
    并通过 sources 事件的 degraded 标记告知前端"本次没有知识库依据"。

    会话开启语音回复且 TTS 已配置时，done 之后追加 `audio` 事件（M5）。

    租户运营配置（M8）两条短路规则，都在进入生成链路之前判定：
    - 命中转人工触发词：建工单 + 返回固定提示，不调模型
    - LLM 未配置且配了兜底回复：返回兜底文案；未配则保持 503（明确失败）
    """
    # 归属校验必须在响应头之前完成：越权/不存在直接返回 404 JSON，
    # 而不是先 200 再在流里塞错误（前端无法用状态码分支）
    conversation = await conversation_service.get_conversation(
        session, owner=owner, conversation_id=conversation_id
    )
    voice_reply = conversation.voice_reply_enabled

    tenant_settings = await tenant_settings_service.load(session, owner.tenant_id)
    trigger = tenant_settings_service.match_trigger(
        payload.content, tenant_settings.handoff_triggers
    )

    def locked_stream(content: str, status: str) -> StreamingResponse:
        async def event_stream() -> AsyncIterator[str]:
            async for event in chat_service.locked_reply_events(
                session,
                owner.tenant_id,
                conversation_id,
                status=status,
                content=content,
            ):
                yield _sse_frame(event)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    if trigger:
        await handoff_service.request_handoff(
            session,
            conversation=conversation,
            requested_by=(
                REQUESTED_BY_USER
                if owner.owner_type == OWNER_TYPE_USER
                else REQUESTED_BY_GUEST
            ),
            note=f"触发词：{trigger}",
        )
        return locked_stream(
            f"已为你转接人工客服（触发词：{trigger}），请稍候。",
            CONVERSATION_STATUS_PENDING_HANDOFF,
        )

    if llm is None:
        if not tenant_settings.fallback_reply:
            # 没配兜底就明确失败：静默返回空回答比报错更难排查
            raise AppError(
                code="LLM_NOT_CONFIGURED",
                message="AI 模型未配置：请设置 AI_API_KEY / AI_BASE_URL / AI_MODEL，"
                "或在租户设置中配置兜底回复",
                status_code=503,
                expose=True,
            )
        return locked_stream(
            tenant_settings.fallback_reply, CONVERSATION_STATUS_AI
        )

    async def event_stream() -> AsyncIterator[str]:
        async for event in chat_service.stream_reply(
            tenant_id=owner.tenant_id,
            conversation_id=conversation_id,
            content=payload.content,
            llm=llm,
            embedder=embedder,
            reranker=reranker,
            speech=speech,
            voice_reply=voice_reply,
        ):
            yield _sse_frame(event)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 反向代理必须关闭缓冲，否则 token 被攒着一起发，失去流式体验
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/{conversation_id}/messages/image",
    response_model=MessageInfo,
    status_code=201,
)
async def upload_image(
    conversation_id: uuid.UUID,
    file: Annotated[UploadFile, File(description="PNG / JPEG / WebP 图片")],
    owner: ConversationOwner = Depends(get_chat_principal),
    session: AsyncSession = Depends(get_session),
) -> MessageInfo:
    """发送图片消息（M8）。

    图片单独成一条 user 消息，不附在文字消息上：前端拿到 id 就能立刻渲染，
    不必等整轮对话结束；下一轮提问时这条图会作为视觉输入带给模型。

    content 留空而不是写占位文案：占位文本会进入检索与上下文，
    变成"模型以为用户说过这句话"。
    """
    conversation = await conversation_service.get_conversation(
        session, owner=owner, conversation_id=conversation_id
    )
    data = await file.read()
    message_id = uuid.uuid4()
    attachment = await asset_service.store_image(
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        message_id=message_id,
        data=data,
    )

    message = Message(
        id=message_id,
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        message_type=asset_service.MESSAGE_TYPE_IMAGE,
        role=ROLE_USER,
        content="",
        source=conversation.channel,
        attachments=[attachment],
    )
    session.add(message)
    conversation.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(message)

    await message_service.publish_message_event(message)
    return message_info(message)


@router.post(
    "/{conversation_id}/leave-message",
    response_model=LeaveMessageInfo,
    status_code=201,
)
async def submit_leave_message(
    conversation_id: uuid.UUID,
    payload: LeaveMessageCreate,
    owner: ConversationOwner = Depends(get_chat_principal),
    session: AsyncSession = Depends(get_session),
) -> LeaveMessageInfo:
    """离线留言（M8）：坐席不在线、或用户不想排队时的兜底。

    留言不改会话状态、也不建工单：它是"稍后回你"，不是"现在就要人接"。
    """
    conversation = await conversation_service.get_conversation(
        session, owner=owner, conversation_id=conversation_id
    )
    message = await leave_service.create(
        session,
        conversation=conversation,
        tenant_id=conversation.tenant_id,
        owner_type=conversation.owner_type,
        name=payload.name,
        contact=payload.contact,
        content=payload.content,
    )
    return LeaveMessageInfo.model_validate(message)


@router.post("/{conversation_id}/messages/{message_id}/rating", response_model=RatingInfo)
async def rate_message(
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    payload: RatingRequest,
    owner: ConversationOwner = Depends(get_chat_principal),
    session: AsyncSession = Depends(get_session),
) -> RatingInfo:
    """给客服回复打满意度分（M8）。

    会话 id 只用于路由可读性：归属校验以消息为准（get_message_owned），
    消息与会话不匹配时同样 404，不额外泄露信息。
    """
    message = await conversation_service.rate_message(
        session, owner=owner, message_id=message_id, score=payload.score
    )
    return RatingInfo(message_id=message.id, score=message.rating)


@router.post("/{conversation_id}/handoff", response_model=HandoffResponse)
async def request_handoff(
    conversation_id: uuid.UUID,
    payload: HandoffRequestPayload,
    owner: ConversationOwner = Depends(get_chat_principal),
    session: AsyncSession = Depends(get_session),
) -> HandoffResponse:
    """转人工（设计文档 5.3）：重复请求返回已有工单，created=false。

    会话状态置 pending_handoff，但 AI **继续回答**——坐席还没来，
    让用户干等比"AI 先说点"更糟；只有坐席认领后（human）才会锁住 AI。

    非工作时间（M8 租户配置）：after_hours_action=leave_message 时拒绝排队，
    明确告诉用户去留言——建了也没人接的单只会让用户干等。
    """
    conversation = await conversation_service.get_conversation(
        session, owner=owner, conversation_id=conversation_id
    )
    tenant_settings = await tenant_settings_service.load(session, owner.tenant_id)
    if (
        tenant_settings.after_hours_action == "leave_message"
        and not tenant_settings_service.is_within_work_hours(tenant_settings)
    ):
        raise AppError(
            code="OUT_OF_WORK_HOURS",
            message="当前不在工作时间，暂时无法转人工：请提交离线留言，"
            "坐席会在工作时间回复你",
            status_code=409,
            expose=True,
        )
    result = await handoff_service.request_handoff(
        session,
        conversation=conversation,
        requested_by=(
            REQUESTED_BY_USER if owner.owner_type == OWNER_TYPE_USER else REQUESTED_BY_GUEST
        ),
        note=payload.note,
    )
    return HandoffResponse(
        ticket=HandoffTicketInfo.model_validate(result.ticket),
        created=result.created,
        conversation_status=conversation.status,
    )


@router.post("/{conversation_id}/handoff/cancel", response_model=HandoffCancelResponse)
async def cancel_handoff(
    conversation_id: uuid.UUID,
    owner: ConversationOwner = Depends(get_chat_principal),
    session: AsyncSession = Depends(get_session),
) -> HandoffCancelResponse:
    """取消排队（设计文档 5.3 的兜底路径）。

    幂等：本来就没有未结工单时返回 cancelled=false，不报错——
    用户连点两次"取消排队"不该看到 404。
    坐席已认领时返回 409：此时单子已经不在队列里，取消语义不成立。
    """
    conversation = await conversation_service.get_conversation(
        session, owner=owner, conversation_id=conversation_id
    )
    ticket = await handoff_service.cancel_ticket(
        session, tenant_id=conversation.tenant_id, conversation_id=conversation.id
    )
    return HandoffCancelResponse(
        cancelled=ticket is not None,
        conversation_status=conversation.status,
    )


@router.get("/{conversation_id}/events")
async def stream_conversation_events(
    conversation_id: uuid.UUID,
    owner: ConversationOwner = Depends(get_chat_principal),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """用户端实时通道（M6）：接收坐席回复与会话状态变化。

    为什么用户端也要 SSE：坐席回复不经过用户的请求，没有这条通道，
    用户只能刷新页面才知道"人来了"。轮询当然也能做，但同一套 Pub/Sub
    基础设施已经在手，SSE 的延迟和成本都更好。

    鉴权在响应头之前完成（归属校验 + Redis 建连），失败直接 404/503，
    不返回一个永远没有消息的 200 流。
    """
    await conversation_service.get_conversation(
        session, owner=owner, conversation_id=conversation_id
    )
    subscription = await open_subscription(conversation_channel(conversation_id))

    async def event_stream() -> AsyncIterator[str]:
        try:
            yield f'event: ready\ndata: {{"conversation_id": "{conversation_id}"}}\n\n'
            async for frame in realtime_frames(
                subscription,
                heartbeat_seconds=settings.agent_stream_heartbeat_seconds,
                # 频道是粗粒度的，事件本身可能属于别的会话（同租户）
                accept=lambda event: event.conversation_id == conversation_id,
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
