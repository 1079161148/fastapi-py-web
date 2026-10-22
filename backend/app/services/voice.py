"""语音消息编排（设计文档 9.3）：音频落库、TTS 生成与缓存、播放地址签发。

与 `services/chat.py` 的分工：
- 本模块只管"声音这一侧"——存储、附件结构、合成缓存、URL 签发
- 文本生成、检索、落库顺序由 `chat.py` 决定，且只由它调用本模块（单向依赖）

缓存策略：TTS 结果写回 `messages.attachments`，同一条消息第二次请求
直接命中缓存（`synthesize_for_message` 里判断）。语音合成按字符计费，
重复合成纯属浪费，而且同一句话两次合成的音色/停顿也会有细微差异。
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.core.security import create_audio_token
from app.db.models.conversation import Message
from app.db.models.usage import KIND_TTS
from app.services import usage as usage_service
from app.speech import SpeechClient
from app.speech.audio import AudioInfo, build_object_key
from app.storage import get_storage

logger = get_logger("voice")

# messages.attachments 里的音频元素靠 type 区分（未来还有 image/file）
ATTACHMENT_TYPE_AUDIO = "audio"
# 合成失败的提示文案：明确告诉用户"可以继续用文字"，符合不过度打扰的降级要求
TTS_TEXT_TOO_LONG = "回答较长，已跳过语音合成（{limit} 字以内支持语音）"


def find_audio_attachment(message: Message) -> dict[str, Any] | None:
    """取消息的音频附件（没有则 None）。"""
    for item in message.attachments or []:
        if isinstance(item, dict) and item.get("type") == ATTACHMENT_TYPE_AUDIO:
            return item
    return None


def audio_url(*, message_id: uuid.UUID, tenant_id: uuid.UUID) -> str:
    """音频播放地址：短时令牌放在 query 里（<audio> 无法带 Authorization）。"""
    token = create_audio_token(message_id=message_id, tenant_id=tenant_id)
    return f"/api/v1/messages/{message_id}/audio?token={token}"


async def store_user_audio(
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    data: bytes,
    info: AudioInfo,
) -> dict[str, Any]:
    """把用户原始录音写入对象存储，返回可落库的附件结构。

    保留原始音频（设计文档 9.3）：既支持复听，也是人工质检与"转写错了"
    时的唯一证据。因此落库失败不应删除文件——但落库在同一个事务里完成，
    这里先存文件再写消息，顺序由调用方保证。
    """
    object_key = build_object_key(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        message_id=message_id,
        suffix=info.suffix,
    )
    await get_storage().save(object_key, data)
    return {
        "type": ATTACHMENT_TYPE_AUDIO,
        "object_key": object_key,
        "mime_type": info.mime_type,
        "duration_seconds": info.duration_seconds,
        "size_bytes": info.size_bytes,
    }


async def synthesize_for_message(
    session: AsyncSession,
    *,
    message: Message,
    speech: SpeechClient,
    voice: str | None = None,
) -> dict[str, Any]:
    """为 assistant 消息合成语音（带缓存），返回 audio 附件。

    调用方需保证：会话归属已校验、租户上下文已设置、消息已落库。
    抛 AppError 时调用方决定是"提示并降级"还是"整体失败"——站内对话是前者。
    """
    settings = get_settings()
    cached = find_audio_attachment(message)
    if cached is not None:
        return cached

    text = message.content.strip()
    if not text:
        raise AppError(
            code="TTS_EMPTY_TEXT", message="该条回复没有内容，无法合成语音", status_code=422
        )
    if len(text) > settings.ai_tts_max_chars:
        # 截断会得到"说到一半"的语音，比没有更糟；直接跳过并说明原因
        raise AppError(
            code="TTS_TEXT_TOO_LONG",
            message=TTS_TEXT_TOO_LONG.format(limit=settings.ai_tts_max_chars),
            status_code=422,
        )

    result = await speech.synthesize(text, voice=voice)
    object_key = build_object_key(
        tenant_id=message.tenant_id,
        conversation_id=message.conversation_id,
        message_id=message.id,
        suffix=_suffix_for_media_type(result.media_type),
    )
    await get_storage().save(object_key, result.audio)

    attachment: dict[str, Any] = {
        "type": ATTACHMENT_TYPE_AUDIO,
        "object_key": object_key,
        "mime_type": result.media_type,
        "duration_seconds": result.duration_seconds,
        "size_bytes": len(result.audio),
        "synthesized": True,
    }
    # 重新赋值整个列表而不是 append：SQLAlchemy 需要感知 JSONB 列的变更
    message.attachments = [*(message.attachments or []), attachment]
    await session.commit()

    # 只在真正调用上游后记录：命中缓存（上面 early return）不产生费用
    await usage_service.record(
        tenant_id=message.tenant_id,
        conversation_id=message.conversation_id,
        kind=KIND_TTS,
        model=get_settings().ai_tts_model,
        units=len(text),
    )

    logger.info(
        "tts_synthesized",
        message_id=str(message.id),
        chars=len(text),
        bytes=len(result.audio),
    )
    return attachment


async def read_audio(message: Message) -> tuple[bytes, str]:
    """读取消息音频内容（播放端点用）。缺失即 404，不返回空 body。"""
    attachment = find_audio_attachment(message)
    if attachment is None:
        raise AppError(code="AUDIO_NOT_FOUND", message="该消息没有语音内容", status_code=404)

    object_key = str(attachment.get("object_key") or "")
    if not object_key:
        raise AppError(code="AUDIO_NOT_FOUND", message="语音内容已失效", status_code=404)

    try:
        data = await get_storage().read(object_key)
    except FileNotFoundError as exc:
        # 存储与数据库不一致：留痕而不是静默 500，便于运维定位丢文件
        logger.error("audio_object_missing", object_key=object_key, message_id=str(message.id))
        raise AppError(
            code="AUDIO_NOT_FOUND", message="语音文件已不存在，请重新生成", status_code=404
        ) from exc

    mime_type = str(attachment.get("mime_type") or "audio/mpeg")
    return data, mime_type


def _suffix_for_media_type(media_type: str) -> str:
    """由上游返回的 MIME 推导后缀；未知一律按 mp3 处理（我们请求的就是 mp3）。"""
    normalized = media_type.split(";")[0].strip().lower()
    return {
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/ogg": ".ogg",
        "audio/opus": ".opus",
        "audio/webm": ".webm",
        "audio/mp4": ".m4a",
        "audio/aac": ".aac",
    }.get(normalized, ".mp3")


__all__ = [
    "ATTACHMENT_TYPE_AUDIO",
    "audio_url",
    "find_audio_attachment",
    "read_audio",
    "store_user_audio",
    "synthesize_for_message",
]
