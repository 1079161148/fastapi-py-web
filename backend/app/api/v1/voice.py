"""语音能力探测（M5）。

前端进页面先问一次：ASR/TTS 是否可用、时长与大小上限多少。
这样"没配置就禁用麦克风按钮"，而不是让用户录完 30 秒才收到 503。
"""

from fastapi import APIRouter

from app.core.config import get_settings
from app.schemas.voice import VoiceCapabilities
from app.speech.factory import transcription_missing_settings, tts_missing_settings

router = APIRouter(prefix="/voice", tags=["voice"])


@router.get("/capabilities", response_model=VoiceCapabilities)
async def get_capabilities() -> VoiceCapabilities:
    """无需登录：访客也要知道能不能用语音（能力是租户级部署配置，不含敏感信息）。"""
    settings = get_settings()
    missing_transcription = transcription_missing_settings()
    missing_tts = tts_missing_settings()
    return VoiceCapabilities(
        transcription=not missing_transcription,
        tts=not missing_tts,
        max_seconds=settings.voice_max_seconds,
        max_mb=settings.voice_max_mb,
        missing_transcription=missing_transcription,
        missing_tts=missing_tts,
    )
