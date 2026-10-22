"""消息级语音接口（M5，设计文档 7 / 9.3）。

两条路径，鉴权方式不同，原因是浏览器行为：

- `POST /messages/{id}/tts`：页面上点"朗读"，走正常 axios 请求（带 Bearer）
- `GET /messages/{id}/audio`：`<audio src>` 由浏览器直接发起，**无法附加
  Authorization 头**，登录用户也没有访客 cookie。因此用 query 里的短时令牌
  认证（令牌只绑定单条消息 + 租户，见 core/security.py）。

音频端点不支持 Range：单条语音通常 <1MB，完整返回即可播放；
拖动进度条需要分片响应，等真的出现长音频需求再加。
"""

import uuid

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    get_chat_principal,
    provide_required_speech_client,
)
from app.core.exceptions import AppError
from app.core.security import decode_token
from app.core.tenant import set_current_tenant
from app.db.models.conversation import Message
from app.db.session import get_session
from app.llm.base import ROLE_ASSISTANT
from app.schemas.voice import SpeechSynthesisResult
from app.services import assets as asset_service
from app.services import conversations as conversation_service
from app.services import voice as voice_service
from app.services.conversations import ConversationOwner
from app.speech import SpeechClient

router = APIRouter(prefix="/messages", tags=["messages"])

# 音频内容不可变，可让浏览器缓存；private 防止中间代理跨用户复用
AUDIO_CACHE_CONTROL = "private, max-age=300"


@router.post("/{message_id}/tts", response_model=SpeechSynthesisResult)
async def synthesize_message(
    message_id: uuid.UUID,
    owner: ConversationOwner = Depends(get_chat_principal),
    session: AsyncSession = Depends(get_session),
    speech: SpeechClient = Depends(provide_required_speech_client),
) -> SpeechSynthesisResult:
    """为指定回复生成语音（幂等：已合成过则直接返回缓存地址）。

    只允许 assistant 消息：对用户自己的录音再合成一遍没有意义，
    且会白白消耗额度。
    """
    message = await conversation_service.get_message_owned(
        session, owner=owner, message_id=message_id
    )
    if message.role != ROLE_ASSISTANT:
        raise AppError(
            code="TTS_ROLE_NOT_ALLOWED",
            message="只能为客服回复生成语音",
            status_code=422,
        )

    cached = voice_service.find_audio_attachment(message) is not None
    attachment = await voice_service.synthesize_for_message(session, message=message, speech=speech)
    return SpeechSynthesisResult(
        message_id=message.id,
        audio_url=voice_service.audio_url(message_id=message.id, tenant_id=message.tenant_id),
        mime_type=str(attachment.get("mime_type") or "audio/mpeg"),
        duration_seconds=_as_float(attachment.get("duration_seconds")),
        cached=cached,
    )


@router.get("/{message_id}/image")
async def download_image(
    message_id: uuid.UUID,
    token: str = Query(min_length=1, description="短时图片令牌"),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """按令牌返回图片字节。

    与音频同一套思路：`<img src>` 由浏览器直接发起，加不了 Authorization 头。
    令牌绑定单条消息 + 租户，伪造 message_id 也拿不到别人的图。
    """
    claims = decode_token(token, expected_type="asset")
    if claims.get("sub") != str(message_id):
        raise AppError(
            code="ASSET_TOKEN_MISMATCH",
            message="图片凭证与消息不匹配，请刷新页面后重试",
            status_code=403,
        )
    try:
        tenant_id = uuid.UUID(str(claims["tid"]))
    except (KeyError, ValueError) as exc:
        raise AppError(
            code="TOKEN_INVALID", message="图片凭证无效，请刷新页面", status_code=401
        ) from exc

    set_current_tenant(tenant_id)
    try:
        message = await session.get(Message, message_id)
        if message is None or message.tenant_id != tenant_id:
            raise AppError(code="IMAGE_NOT_FOUND", message="图片不存在", status_code=404)
        data, media_type = await asset_service.read_image(message)
    finally:
        set_current_tenant(None)

    return Response(
        content=data,
        media_type=media_type,
        headers={"Cache-Control": AUDIO_CACHE_CONTROL},
    )


@router.get("/{message_id}/audio")
async def download_audio(
    message_id: uuid.UUID,
    token: str = Query(min_length=1, description="短时音频令牌"),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """按令牌返回音频字节。

    令牌本身即凭证：解析出 tenant 后设置租户上下文，再按 RLS 取消息，
    因此伪造 message_id 也拿不到别的租户/别人的音频。
    """
    claims = decode_token(token, expected_type="audio")
    if claims.get("sub") != str(message_id):
        raise AppError(
            code="AUDIO_TOKEN_MISMATCH",
            message="音频凭证与消息不匹配，请刷新页面后重试",
            status_code=403,
        )
    try:
        tenant_id = uuid.UUID(str(claims["tid"]))
    except (KeyError, ValueError) as exc:
        raise AppError(
            code="TOKEN_INVALID", message="音频凭证无效，请刷新页面", status_code=401
        ) from exc

    set_current_tenant(tenant_id)
    try:
        message = await session.get(Message, message_id)
        if message is None or message.tenant_id != tenant_id:
            raise AppError(code="AUDIO_NOT_FOUND", message="语音内容不存在", status_code=404)
        data, media_type = await voice_service.read_audio(message)
    finally:
        set_current_tenant(None)

    return Response(
        content=data,
        media_type=media_type,
        headers={"Cache-Control": AUDIO_CACHE_CONTROL},
    )


def _as_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None
