"""语音客户端工厂：缺失即明确报错，不回退到"哑客户端"。

配置策略与嵌入模型一致：**模型必须单独指定**（`AI_TRANSCRIPTION_MODEL` /
`AI_TTS_MODEL`），密钥与端点可回退到 `AI_API_KEY` / `AI_BASE_URL`。
原因：几乎没有模型同时承担对话、转写、合成三种职责，把 AI_MODEL 拿去调
`/audio/speech` 只会得到供应商侧一句晦涩的 400。

可选性差异：
- 转写（ASR）是语音消息的**入口**，缺失时该功能直接不可用 → 抛 503
- 合成（TTS）是**增强**，缺失时只是没有语音回复 → 返回 None 或按需抛错
"""

from app.core.config import get_settings
from app.speech.base import SpeechClient, SpeechNotConfiguredError, TranscriptionClient
from app.speech.openai_compatible import OpenAITranscriptionClient, OpenAITTSClient


def _transcription_settings() -> tuple[str, str, str, list[str]]:
    settings = get_settings()
    api_key = settings.ai_transcription_api_key.strip() or settings.ai_api_key.strip()
    base_url = settings.ai_transcription_base_url.strip() or settings.ai_base_url.strip()
    model = settings.ai_transcription_model.strip()

    missing: list[str] = []
    if not api_key:
        missing.append("AI_TRANSCRIPTION_API_KEY（或 AI_API_KEY）")
    if not base_url:
        missing.append("AI_TRANSCRIPTION_BASE_URL（或 AI_BASE_URL）")
    if not model:
        missing.append("AI_TRANSCRIPTION_MODEL")
    return api_key, base_url, model, missing


def _tts_settings() -> tuple[str, str, str, str, list[str]]:
    settings = get_settings()
    api_key = settings.ai_tts_api_key.strip() or settings.ai_api_key.strip()
    base_url = settings.ai_tts_base_url.strip() or settings.ai_base_url.strip()
    model = settings.ai_tts_model.strip()
    voice = settings.ai_tts_voice.strip() or "alloy"

    missing: list[str] = []
    if not api_key:
        missing.append("AI_TTS_API_KEY（或 AI_API_KEY）")
    if not base_url:
        missing.append("AI_TTS_BASE_URL（或 AI_BASE_URL）")
    if not model:
        missing.append("AI_TTS_MODEL")
    return api_key, base_url, model, voice, missing


def get_transcription_client() -> TranscriptionClient:
    """ASR 客户端；未配置抛 SpeechNotConfiguredError（503，可曝光）。"""
    api_key, base_url, model, missing = _transcription_settings()
    if missing:
        raise SpeechNotConfiguredError(missing, capability="转写")

    return OpenAITranscriptionClient(
        api_key=api_key,
        base_url=base_url,
        model=model,
        timeout_seconds=get_settings().ai_transcription_timeout_seconds,
    )


def get_speech_client() -> SpeechClient | None:
    """TTS 客户端；未配置返回 None（语音回复是可选增强，不影响文字链路）。"""
    api_key, base_url, model, voice, missing = _tts_settings()
    if missing:
        return None

    return OpenAITTSClient(
        api_key=api_key,
        base_url=base_url,
        model=model,
        voice=voice,
        timeout_seconds=get_settings().ai_tts_timeout_seconds,
    )


def get_required_speech_client() -> SpeechClient:
    """显式要求 TTS 的场景（用户点"转语音"）：未配置必须 503 而非静默无响应。"""
    client = get_speech_client()
    if client is not None:
        return client
    raise SpeechNotConfiguredError(_tts_settings()[4], capability="合成")


def transcription_missing_settings() -> list[str]:
    """能力探测用：缺哪些变量（空列表即已就绪）。"""
    return _transcription_settings()[3]


def tts_missing_settings() -> list[str]:
    return _tts_settings()[4]


__all__ = [
    "get_required_speech_client",
    "get_speech_client",
    "get_transcription_client",
    "transcription_missing_settings",
    "tts_missing_settings",
]
