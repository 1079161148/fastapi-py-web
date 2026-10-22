"""对话编排与流式生成（设计文档 5.1 / 9.4）。

关键设计：生成器自己开数据库会话，不复用请求级 session。
原因：FastAPI 对带 yield 的依赖在响应开始发送前就完成清理（0.106+ 行为），
若在流式过程中继续用请求级 session，会操作已关闭的连接；而 assistant
消息必须等流结束后才能落库。因此这里用独立会话 + 显式设置租户上下文。

落库策略（设计文档 5.1）：user 消息在生成开始前立即落库；
assistant 消息在收齐全文后一次性落库，附 tokens、耗时与引用来源。
流式中途失败不落半截回答——避免历史里出现残缺 AI 消息。

M3 起接入 RAG：先检索（失败则降级为纯模型回答并显式标注），
把命中的片段拼进 system 提示词，并通过 sources 事件先行推给前端。
"""

import base64
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.core.tenant import set_current_tenant
from app.db.models.conversation import (
    CONVERSATION_STATUS_HUMAN,
    OWNER_TYPE_USER,
    Conversation,
    Message,
)
from app.db.models.handoff import (
    REASON_AI_MARKER,
    REASON_KEYWORD,
    REQUESTED_BY_AI,
    REQUESTED_BY_GUEST,
    REQUESTED_BY_USER,
    HandoffTicket,
)
from app.db.models.support import SENSITIVE_ACTION_BLOCK, SENSITIVE_ACTION_HANDOFF
from app.db.models.usage import KIND_ASR, KIND_DIALOGUE
from app.db.session import async_session_factory
from app.llm.base import (
    ROLE_ASSISTANT,
    ROLE_SYSTEM,
    ROLE_USER,
    ChatMessage,
    EmbeddingClient,
    ImagePart,
    LLMClient,
    RerankClient,
    TokenUsage,
)
from app.services import assets as asset_service
from app.services import handoff as handoff_service
from app.services import messages as message_service
from app.services import sensitive as sensitive_service
from app.services import tenant_settings
from app.services import usage as usage_service
from app.services import voice as voice_service
from app.services.retrieval import RetrievedChunk, retrieve_for_chat
from app.speech import SpeechClient, TranscriptionClient
from app.speech.audio import inspect_audio

logger = get_logger("chat")

EVENT_MESSAGE = "message"
EVENT_SOURCES = "sources"
EVENT_DELTA = "delta"
EVENT_DONE = "done"
EVENT_ERROR = "error"
# M5 语音：转写阶段与语音回复（音频晚于 done 到达，先让用户看到文字）
EVENT_TRANSCRIBING = "transcribing"
EVENT_AUDIO = "audio"
EVENT_AUDIO_FAILED = "audio_failed"
# M6 转人工：本轮已进入人工队列（前端据此显示"等待坐席"角标）
EVENT_HANDOFF = "handoff"

TITLE_MAX_LENGTH = 50
# 推给前端的引用摘要长度：整段 chunk 塞进 SSE 既浪费带宽也没必要
SOURCE_SNIPPET_LENGTH = 300

SYSTEM_PROMPT = (
    "你是专业、耐心的中文客服助手。回答要简洁准确、分点清晰；"
    "不确定的信息不要编造，直接说明无法确认并建议转人工。"
)

# 有检索结果时追加的作答约束（设计文档 12：RAG 强制引用、无依据时显式声明）
REFERENCE_INSTRUCTION = (
    "\n\n请优先依据下面的参考资料作答，并在使用处用 [编号] 标注来源；"
    "参考资料不足以回答时，明确说明「资料中未提及」，不要编造。"
)

# AI 主动转人工的标记（设计文档 5.3 "AI 判定" 的落地方式）。
# 不用工具调用：当前编排未引入 LangGraph 工具链，让模型输出一个不可能自然出现
# 的标记是成本最低、最容易验证的做法；标记在落库前一定会被剥离。
HANDOFF_MARKER = "[[HANDOFF]]"
HANDOFF_INSTRUCTION = (
    "\n\n如果问题超出你能确认的范围（资料未提及、需要核验身份或操作后台），"
    f"先如实说明无法确认，然后在回答最后单独输出 {HANDOFF_MARKER}，"
    "系统会据此转接人工坐席。不要解释这个标记。"
)
# 会话已被坐席接管（status=human）时的固定回复：AI 必须闭嘴
HANDOFF_LOCKED_REPLY = "已转接人工坐席，坐席会在这里回复你，请稍候。"
HANDOFF_NOTICE = "已为你转接人工坐席，请稍候；你也可以继续补充信息。"


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """SSE 事件：event 名 + JSON 可序列化的 data。"""

    event: str
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class UserMessageDraft:
    """待落库的用户消息。

    语音消息需要"先上传音频、再写消息"，且音频 key 由 message_id 拼装，
    因此 ID 必须在落库前确定。用显式草稿传递，避免让 stream_reply 长出
    一串只对语音有意义的参数（设计文档 5.1：编排层零分支）。
    """

    id: uuid.UUID
    content: str
    message_type: str = "text"
    attachments: tuple[dict[str, Any], ...] = ()


async def stream_reply(
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    content: str,
    llm: LLMClient,
    embedder: EmbeddingClient | None = None,
    reranker: RerankClient | None = None,
    source: str = "web",
    draft: UserMessageDraft | None = None,
    speech: SpeechClient | None = None,
    voice_reply: bool = False,
    voice: str | None = None,
) -> AsyncIterator[StreamEvent]:
    """生成一轮回复。

    调用前必须已校验会话归属（见 conversations.get_conversation）；
    这里只按 tenant_id 落库，RLS 会再兜一次租户隔离。

    source 标记消息来源渠道（web/email/wechat/dingtalk）：渠道入站也必须
    走同一条编排链路，只是把 source 换成渠道名，而不是另写一份生成逻辑。

    speech/voice_reply 控制 TTS：两者同时具备才在 done 之后补发 `audio` 事件。
    语音**永远晚于文字**——合成慢于生成，不能让用户盯着空白等音频。
    """
    settings = get_settings()
    set_current_tenant(tenant_id)
    try:
        async with async_session_factory() as session:
            # 配额校验必须先于用户消息落库：超限时拒绝本轮请求，
            # 而不是留下"用户说了话、系统没回应"的半截历史
            try:
                await usage_service.enforce_daily_token_quota(session, tenant_id=tenant_id)
            except AppError as exc:
                yield StreamEvent(
                    EVENT_ERROR,
                    {
                        "code": exc.code,
                        "message": exc.message,
                        "retryable": False,
                        "detail": exc.detail,
                    },
                )
                return

            now = datetime.now(UTC)
            user_message = Message(
                id=draft.id if draft is not None else uuid.uuid4(),
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                message_type=draft.message_type if draft is not None else "text",
                role=ROLE_USER,
                content=content,
                source=source,
                attachments=list(draft.attachments) if draft is not None else [],
                created_at=now,
                updated_at=now,
            )
            # 先取会话：status=human 表示坐席已接管，AI 必须闭嘴（设计文档 5.3）
            conversation = await session.get(Conversation, conversation_id)
            locked = (
                conversation is not None and conversation.status == CONVERSATION_STATUS_HUMAN
            )

            # 敏感词（M9）：block 必须发生在落库之前——
            # 被拦下的内容不该留在会话历史里，否则"拦了等于没拦"
            tenant_config = await tenant_settings.load(session, tenant_id)
            sensitive_hits = sensitive_service.match(content, tenant_config.sensitive_words)
            if sensitive_hits and tenant_config.sensitive_action == SENSITIVE_ACTION_BLOCK:
                await sensitive_service.record_hit(
                    session,
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    message_id=None,
                    words=sensitive_hits,
                    action=SENSITIVE_ACTION_BLOCK,
                    content=content,
                )
                yield StreamEvent(
                    EVENT_ERROR,
                    {
                        "code": "SENSITIVE_BLOCKED",
                        "message": "内容包含敏感词，请调整后重试",
                        "retryable": False,
                        "detail": None,
                    },
                )
                return

            session.add(user_message)
            await _touch_conversation(session, tenant_id, conversation_id, content, now)
            await session.commit()
            await message_service.publish_message_event(user_message)

            yield StreamEvent(
                EVENT_MESSAGE,
                {
                    "id": str(user_message.id),
                    "role": ROLE_USER,
                    "content": content,
                    "message_type": user_message.message_type,
                    "attachments": list(user_message.attachments),
                    "audio_url": message_service.audio_url_for(user_message),
                    "created_at": now.isoformat(),
                },
            )

            if locked:
                # 用户消息照常落库（坐席要能看到），但不走生成：
                # 这时再让 AI 插一句会把坐席的回复淹掉
                async for event in locked_reply_events(
                    session, tenant_id, conversation_id
                ):
                    yield event
                return

            # 命中敏感词（非 block）：留痕。handoff 策略下交给坐席判，
            # 不让 AI 在敏感话题上自由发挥
            if sensitive_hits:
                await sensitive_service.record_hit(
                    session,
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    message_id=user_message.id,
                    words=sensitive_hits,
                    action=tenant_config.sensitive_action,
                    content=content,
                )
                if tenant_config.sensitive_action == SENSITIVE_ACTION_HANDOFF:
                    ticket = await _request_handoff_by_sensitive(
                        session, conversation, sensitive_hits[0]
                    )
                else:
                    ticket = None
            else:
                ticket = None

            if ticket is None:
                ticket = await _request_handoff_by_keyword(session, conversation, content)

            outcome = await retrieve_for_chat(
                session,
                tenant_id=tenant_id,
                query=content,
                embedder=embedder,
                reranker=reranker,
            )
            sources = _sources_payload(outcome.items)
            # sources 事件先于 delta：前端可立即显示"依据了哪几条资料"
            yield StreamEvent(
                EVENT_SOURCES,
                {"items": sources, "degraded": outcome.degraded, "reason": outcome.reason},
            )

            prompt = await _build_prompt(
                session,
                tenant_id,
                conversation_id,
                settings.chat_history_limit,
                references=outcome.items,
            )
            started = time.perf_counter()
            parts: list[str] = []
            usage: TokenUsage | None = None

            try:
                async for chunk in llm.chat_stream(prompt):
                    if chunk.delta:
                        parts.append(chunk.delta)
                        yield StreamEvent(EVENT_DELTA, {"content": chunk.delta})
                    if chunk.usage is not None:
                        # 保留完整用量：成本按 prompt / completion 分别计价，
                        # 只存 total 无法算准（设计文档 10）
                        usage = chunk.usage
            except AppError as exc:
                # 上游/配置类错误：已经发出 200 与部分内容，只能以 error 事件收尾
                logger.warning(
                    "chat_stream_failed", code=exc.code, conversation_id=str(conversation_id)
                )
                yield StreamEvent(
                    EVENT_ERROR,
                    {
                        "code": exc.code,
                        "message": exc.message,
                        "retryable": exc.status_code >= 500,
                    },
                )
                return
            except Exception:  # noqa: BLE001 - 流式阶段必须兜底，不能让连接无声断开
                logger.exception("chat_stream_unexpected", conversation_id=str(conversation_id))
                yield StreamEvent(
                    EVENT_ERROR,
                    {
                        "code": "LLM_UPSTREAM_ERROR",
                        "message": "AI 服务暂时不可用，请稍后重试或转人工",
                        "retryable": True,
                    },
                )
                return

            text = "".join(parts)
            marker_hit = False
            if settings.handoff_ai_marker_enabled and HANDOFF_MARKER in text:
                # 标记是给系统的信号，绝不能留在用户可见的回答里
                marker_hit = True
                text = text.replace(HANDOFF_MARKER, "").strip()
                if not text:
                    # 模型只吐了标记：用固定文案兜底，避免出现空回答
                    text = HANDOFF_NOTICE
            if not text.strip():
                # 空回复同样必须显式报错，不写一条空消息污染历史
                logger.warning("chat_stream_empty", conversation_id=str(conversation_id))
                yield StreamEvent(
                    EVENT_ERROR,
                    {
                        "code": "LLM_EMPTY_RESPONSE",
                        "message": "AI 未返回内容，请重试",
                        "retryable": True,
                    },
                )
                return

            latency_ms = int((time.perf_counter() - started) * 1000)
            finished = datetime.now(UTC)
            usage_total = usage.total_tokens if usage is not None else None
            assistant_message = Message(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                message_type="text",
                role=ROLE_ASSISTANT,
                content=text,
                source=source,
                tokens=usage_total,
                latency_ms=latency_ms,
                sources=sources,
                created_at=finished,
                updated_at=finished,
            )
            session.add(assistant_message)
            await session.commit()
            await message_service.publish_message_event(assistant_message)

            # 用量流水：供应商没返回 usage 时也记一行（计数为 0）。
            # 目的是让"请求次数"与"token 用量"分开可查——只统计 token 会让
            # 不返回 usage 的网关看起来像没被调用过。
            await usage_service.record(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                kind=KIND_DIALOGUE,
                model=settings.ai_model,
                prompt_tokens=usage.prompt_tokens if usage is not None else 0,
                completion_tokens=usage.completion_tokens if usage is not None else 0,
            )

            # AI 自己判断答不了：模型在回答末尾输出了标记 → 建单并入队
            if marker_hit and conversation is not None:
                ticket = ticket or await _request_handoff_by_ai(session, conversation)

            if ticket is not None:
                yield StreamEvent(
                    EVENT_HANDOFF,
                    {
                        "ticket_id": str(ticket.id),
                        "status": ticket.status,
                        "reason": ticket.reason,
                        "message": HANDOFF_NOTICE,
                    },
                )

            yield StreamEvent(
                EVENT_DONE,
                {
                    "id": str(assistant_message.id),
                    "role": ROLE_ASSISTANT,
                    "content": text,
                    "message_type": assistant_message.message_type,
                    "attachments": list(assistant_message.attachments or []),
                    "audio_url": None,
                    "tokens": usage_total,
                    "latency_ms": latency_ms,
                    "created_at": finished.isoformat(),
                },
            )

            # 语音回复：文字已经送达，音频是增强——合成失败只影响音频，
            # 绝不能因为 TTS 挂了把已生成的回答回滚掉（设计文档 10 降级）
            if voice_reply:
                yield await _build_audio_event(
                    session, assistant_message, speech=speech, voice=voice, tenant_id=tenant_id
                )
    finally:
        # 生成器可能被客户端断开提前关闭，这里清理上下文避免污染同一任务的后续工作
        set_current_tenant(None)


async def stream_audio_reply(
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    audio: bytes,
    declared_duration: float | None,
    transcriber: TranscriptionClient,
    llm: LLMClient,
    embedder: EmbeddingClient | None = None,
    reranker: RerankClient | None = None,
    speech: SpeechClient | None = None,
    voice_reply: bool = False,
    voice: str | None = None,
    source: str = "web",
) -> AsyncIterator[StreamEvent]:
    """语音消息一轮对话（设计文档 9.3）：上传 → 校验 → ASR → 既有编排 → 可选 TTS。

    为什么 ASR 放在流里而不是响应头之前：转写 P95 约 3s，放在头前前端拿不到
    任何进度；放进流里可以先发 `transcribing` 事件，UI 立刻显示"识别中"。
    代价是转写失败只能以 error 事件收尾（HTTP 已是 200），与文本链路的既有
    约定一致（前端本来就要处理 error 事件）。

    音频在**转写成功之后**才写对象存储：转写失败不留孤儿文件。
    文件名一律由服务端生成，不使用客户端文件名（避免头部注入与网关解析歧义）。
    """
    settings = get_settings()
    yield StreamEvent(EVENT_TRANSCRIBING, {"status": "started"})

    try:
        info = inspect_audio(
            audio,
            declared_duration=declared_duration,
            max_seconds=settings.voice_max_seconds,
            max_bytes=settings.voice_max_mb * 1024 * 1024,
        )
        result = await transcriber.transcribe(
            audio, filename=f"voice{info.suffix}", mime_type=info.mime_type
        )
    except AppError as exc:
        # 格式/时长/上游错误：前端据此提示"重录"或"改用文字"
        yield StreamEvent(
            EVENT_ERROR,
            {
                "code": exc.code,
                "message": exc.message,
                "retryable": exc.status_code >= 500,
            },
        )
        return

    text = result.text.strip()
    if not text:
        # 静音或纯噪声：不算系统故障，但也不能写一条空消息
        yield StreamEvent(
            EVENT_ERROR,
            {
                "code": "ASR_EMPTY",
                "message": "没有识别到有效语音内容，请重新录制或改用文字输入",
                "retryable": True,
            },
        )
        return

    # ASR 按音频时长计费：优先用网关返回的时长，缺失时回退到本地探测值
    await usage_service.record(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        kind=KIND_ASR,
        model=settings.ai_transcription_model,
        units=result.duration_seconds or info.duration_seconds or 0,
    )

    message_id = uuid.uuid4()
    try:
        attachment = await voice_service.store_user_audio(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            message_id=message_id,
            data=audio,
            info=info,
        )
    except Exception:  # noqa: BLE001 - 存储异常必须转成可读错误，不能让流无声断开
        logger.exception("voice_store_failed", conversation_id=str(conversation_id))
        yield StreamEvent(
            EVENT_ERROR,
            {
                "code": "AUDIO_STORE_FAILED",
                "message": "语音文件保存失败，请重试或改用文字输入",
                "retryable": True,
            },
        )
        return

    draft = UserMessageDraft(
        id=message_id, content=text, message_type="audio", attachments=(attachment,)
    )
    async for event in stream_reply(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        content=text,
        llm=llm,
        embedder=embedder,
        reranker=reranker,
        source=source,
        draft=draft,
        speech=speech,
        voice_reply=voice_reply,
        voice=voice,
    ):
        yield event


async def _build_audio_event(
    session: AsyncSession,
    message: Message,
    *,
    speech: SpeechClient | None,
    voice: str | None,
    tenant_id: uuid.UUID,
) -> StreamEvent:
    """生成 `audio` 或 `audio_failed` 事件：语音失败不影响本轮回答的有效性。"""
    if speech is None:
        return StreamEvent(
            EVENT_AUDIO_FAILED,
            {
                "message_id": str(message.id),
                "code": "SPEECH_NOT_CONFIGURED",
                "message": "语音合成未配置，本次仅返回文字",
            },
        )
    try:
        attachment = await voice_service.synthesize_for_message(
            session, message=message, speech=speech, voice=voice
        )
    except AppError as exc:
        logger.warning("tts_failed", code=exc.code, message_id=str(message.id))
        return StreamEvent(
            EVENT_AUDIO_FAILED,
            {
                "message_id": str(message.id),
                "code": exc.code,
                "message": exc.message,
            },
        )
    except Exception:  # noqa: BLE001 - 流已发出 done，任何异常都不能让连接硬断
        logger.exception("tts_unexpected_error", message_id=str(message.id))
        return StreamEvent(
            EVENT_AUDIO_FAILED,
            {
                "message_id": str(message.id),
                "code": "TTS_FAILED",
                "message": "语音合成失败，本次仅返回文字",
            },
        )

    return StreamEvent(
        EVENT_AUDIO,
        {
            "message_id": str(message.id),
            "audio_url": voice_service.audio_url(message_id=message.id, tenant_id=tenant_id),
            "mime_type": attachment.get("mime_type"),
            "duration_seconds": attachment.get("duration_seconds"),
        },
    )


@dataclass(frozen=True, slots=True)
class ReplyResult:
    """非流式调用（渠道入站）的结果：转发给渠道只需全文 + 引用。"""

    conversation_id: uuid.UUID
    message_id: uuid.UUID
    content: str
    sources: list[dict[str, Any]]


class ReplyFailedError(AppError):
    """把流里的 error 事件转成异常，让渠道侧能明确标记失败并决定是否重试。"""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(
            code=code,
            message=message,
            status_code=502 if retryable else 400,
            expose=True,
        )
        self.retryable = retryable


async def collect_reply(
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    content: str,
    llm: LLMClient,
    embedder: EmbeddingClient | None = None,
    reranker: RerankClient | None = None,
    source: str = "web",
) -> ReplyResult:
    """把流式回复收齐成一条完整消息（渠道入站用，设计文档 9.1）。

    邮件/微信/钉钉没有 SSE 通道，必须拿到全文才能投递；
    但生成、RAG、落库逻辑必须与站内完全一致，因此复用 stream_reply
    而不是另写一份"非流式对话"。错误事件在这里转成异常：渠道侧需要
    明确的成功/失败边界来决定是否重试投递。
    """
    message_id: uuid.UUID | None = None
    text = ""
    sources: list[dict[str, Any]] = []

    async for event in stream_reply(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        content=content,
        llm=llm,
        embedder=embedder,
        reranker=reranker,
        source=source,
    ):
        if event.event == EVENT_SOURCES:
            sources = list(event.data.get("items") or [])
        elif event.event == EVENT_DONE:
            message_id = uuid.UUID(str(event.data["id"]))
            text = str(event.data["content"])
        elif event.event == EVENT_ERROR:
            raise ReplyFailedError(
                str(event.data.get("code") or "LLM_UPSTREAM_ERROR"),
                str(event.data.get("message") or "AI 服务暂时不可用"),
                retryable=bool(event.data.get("retryable", True)),
            )

    if message_id is None:
        raise ReplyFailedError("LLM_EMPTY_RESPONSE", "AI 未返回内容", retryable=True)

    return ReplyResult(
        conversation_id=conversation_id,
        message_id=message_id,
        content=text,
        sources=sources,
    )


async def locked_reply_events(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    status: str = CONVERSATION_STATUS_HUMAN,
    content: str = HANDOFF_LOCKED_REPLY,
) -> AsyncIterator[StreamEvent]:
    """固定文案回复：不调模型，但照常落库（刷新后仍能看到）。

    两种用途：坐席接管后的提示（默认文案）与运营配置命中后的
    提示（触发词转人工、AI 兜底回复）——后者 status 仍为 ai。
    """
    now = datetime.now(UTC)
    notice = Message(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        message_type="text",
        role=ROLE_ASSISTANT,
        content=content,
        source="system",
        created_at=now,
        updated_at=now,
    )
    session.add(notice)
    await session.commit()
    await message_service.publish_message_event(notice)

    yield StreamEvent(
        EVENT_HANDOFF,
        {
            "status": status,
            "message": content,
        },
    )
    yield StreamEvent(
        EVENT_DONE,
        {
            "id": str(notice.id),
            "role": ROLE_ASSISTANT,
            "content": content,
            "message_type": "text",
            "attachments": [],
            "audio_url": None,
            "tokens": None,
            "latency_ms": 0,
            "created_at": now.isoformat(),
        },
    )


def _matched_keyword(content: str, raw_keywords: str) -> str | None:
    """命中转人工关键词就返回命中的词（记入工单 extra，便于统计误触发）。"""
    for keyword in (item.strip() for item in raw_keywords.split(",")):
        if keyword and keyword in content:
            return keyword
    return None


async def _request_handoff_by_keyword(
    session: AsyncSession, conversation: Conversation | None, content: str
) -> HandoffTicket | None:
    """用户话里带"转人工"就建单。失败不中断回答——用户还能再点一次按钮。"""
    if conversation is None:
        return None
    keyword = _matched_keyword(content, get_settings().handoff_keywords)
    if keyword is None:
        return None
    try:
        result = await handoff_service.request_handoff(
            session,
            conversation=conversation,
            reason=REASON_KEYWORD,
            requested_by=(
                REQUESTED_BY_USER if conversation.owner_type == OWNER_TYPE_USER
                else REQUESTED_BY_GUEST
            ),
            extra={"keyword": keyword},
        )
    except Exception:  # noqa: BLE001 - 建单失败不能毁掉已经生成的回答
        logger.exception("handoff_by_keyword_failed", conversation_id=str(conversation.id))
        return None
    return result.ticket


async def _request_handoff_by_sensitive(
    session: AsyncSession, conversation: Conversation | None, word: str
) -> HandoffTicket | None:
    """命中租户敏感词转人工（M9）。

    复用 REASON_KEYWORD（工单原因保持封闭集合），区分信息放在 extra 里：
    "这个词是敏感词库里的"对事后统计更有用，再开一个 reason 只是增加分支。
    """
    if conversation is None:
        return None
    try:
        result = await handoff_service.request_handoff(
            session,
            conversation=conversation,
            reason=REASON_KEYWORD,
            requested_by=(
                REQUESTED_BY_USER if conversation.owner_type == OWNER_TYPE_USER
                else REQUESTED_BY_GUEST
            ),
            extra={"keyword": word, "source": "sensitive"},
        )
    except Exception:  # noqa: BLE001 - 建单失败不能毁掉已经生成的回答
        logger.exception("handoff_by_sensitive_failed", conversation_id=str(conversation.id))
        return None
    return result.ticket


async def _request_handoff_by_ai(
    session: AsyncSession, conversation: Conversation
) -> HandoffTicket | None:
    try:
        result = await handoff_service.request_handoff(
            session,
            conversation=conversation,
            reason=REASON_AI_MARKER,
            requested_by=REQUESTED_BY_AI,
            extra={"marker": HANDOFF_MARKER},
        )
    except Exception:  # noqa: BLE001 - 同上：回答已经生成，转人工失败只影响队列
        logger.exception("handoff_by_ai_failed", conversation_id=str(conversation.id))
        return None
    return result.ticket


async def _touch_conversation(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    first_content: str,
    now: datetime,
) -> None:
    """更新会话活跃时间；标题为空时用首条用户消息生成（便于会话列表展示）。"""
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or conversation.tenant_id != tenant_id:
        # 调用方已校验归属，走到这里说明数据被并发删除；不再纠缠，让消息照常落库
        logger.warning("conversation_missing", conversation_id=str(conversation_id))
        return
    conversation.updated_at = now
    if not conversation.title:
        conversation.title = _make_title(first_content)


def _make_title(content: str) -> str:
    title = " ".join(content.split())
    return title[:TITLE_MAX_LENGTH]


async def _build_prompt(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    history_limit: int,
    *,
    references: list[RetrievedChunk] | None = None,
) -> list[ChatMessage]:
    """取最近 N 条消息（含刚落的 user 消息）按时间正序拼成 prompt。

    参考资料放进 system 消息而不是改写用户问题：历史记录保持原样，
    下一轮仍能把真实对话内容交给模型。
    """
    rows = (
        await session.execute(
            select(Message)
            .where(
                Message.tenant_id == tenant_id,
                Message.conversation_id == conversation_id,
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(history_limit)
        )
    ).scalars().all()

    # 图片消息 content 为空，但必须保留：视觉输入全靠 attachments
    ordered = [
        row
        for row in reversed(rows)
        if row.content.strip() or asset_service.find_image_attachment(row) is not None
    ]

    # 只把最近 N 张图送给模型：每张图都按 vision token 计费，
    # 一次塞十张既贵又容易顶到网关的请求体上限
    vision_budget = get_settings().chat_image_vision_limit
    images_by_id: dict[uuid.UUID, tuple[ImagePart, ...]] = {}
    for row in reversed(ordered):
        if vision_budget <= 0:
            break
        if asset_service.find_image_attachment(row) is None:
            continue
        loaded = await _load_image_part(row)
        if loaded:
            images_by_id[row.id] = (loaded,)
            vision_budget -= 1

    history = [
        ChatMessage(
            role=row.role,
            content=row.content,
            images=images_by_id.get(row.id, ()),
        )
        for row in ordered
    ]

    system_content = SYSTEM_PROMPT
    if get_settings().handoff_ai_marker_enabled:
        # 让模型在"答不了"时主动示意转人工（设计文档 5.3 的 AI 判定）
        system_content += HANDOFF_INSTRUCTION
    if references:
        system_content += REFERENCE_INSTRUCTION + "\n\n" + _render_references(references)
    return [ChatMessage(role=ROLE_SYSTEM, content=system_content), *history]


async def _load_image_part(message: Message) -> ImagePart | None:
    """读图片并转 base64；文件丢了就降级为纯文本，不阻断整轮对话。"""
    try:
        data, mime_type = await asset_service.read_image(message)
    except AppError:
        logger.warning("chat_image_unavailable", message_id=str(message.id))
        return None
    except OSError:
        logger.warning("chat_image_read_failed", message_id=str(message.id))
        return None
    return ImagePart(mime_type=mime_type, data_b64=base64.b64encode(data).decode("ascii"))


def _render_references(items: list[RetrievedChunk]) -> str:
    lines = ["参考资料："]
    for index, item in enumerate(items, start=1):
        lines.append(f"[{index}]（来源：{item.document_name}）{item.content}")
    return "\n".join(lines)


def _sources_payload(items: list[RetrievedChunk]) -> list[dict[str, Any]]:
    """SSE 与 messages.sources 共用的引用结构（uuid 转字符串以 JSON 序列化）。"""
    return [
        {
            "chunk_id": str(item.chunk_id),
            "document_id": str(item.document_id),
            "document_name": item.document_name,
            "content": item.content[:SOURCE_SNIPPET_LENGTH],
            "score": round(item.score, 6),
        }
        for item in items
    ]
