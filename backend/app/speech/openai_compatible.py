"""OpenAI 兼容协议的 ASR / TTS 客户端（设计文档 3.1 / 9.3）。

端点约定（与 /chat/completions 同一套网关即可）：
- `POST {base}/audio/transcriptions`  multipart(file, model, response_format) → {"text": ...}
- `POST {base}/audio/speech`         json(model, input, voice, response_format) → 音频字节

不引入供应商 SDK：语音端点在各家网关上的形状高度一致，直连 httpx 反而
更少踩版本坑，也不必为一个 HTTP 调用引入新依赖。
"""

import httpx

from app.core.logging import get_logger
from app.speech.base import (
    SpeechUpstreamError,
    SynthesisResult,
    TranscriptionResult,
)

logger = get_logger("speech")

# 上游错误体只截断记录，避免把整页 HTML 塞进日志
ERROR_BODY_LIMIT = 500
# 合成音频的格式：mp3 兼容性最好（Safari/Chrome/Firefox 均可直接 <audio> 播放）
TTS_RESPONSE_FORMAT = "mp3"
TRANSCRIPTION_RESPONSE_FORMAT = "json"


def _auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _log_rejection(kind: str, status: int, body: str) -> None:
    logger.warning(f"speech_{kind}_rejected", status=status, body=body[:ERROR_BODY_LIMIT])


class OpenAITranscriptionClient:
    """语音转写客户端。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds

    async def transcribe(
        self,
        audio: bytes,
        *,
        filename: str,
        mime_type: str,
        language: str | None = None,
    ) -> TranscriptionResult:
        data = {"model": self._model, "response_format": TRANSCRIPTION_RESPONSE_FORMAT}
        if language:
            data["language"] = language
        files = {"file": (filename, audio, mime_type)}
        timeout = httpx.Timeout(self._timeout, connect=10.0)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{self._base_url}/audio/transcriptions",
                    files=files,
                    data=data,
                    headers=_auth_headers(self._api_key),
                )
        except httpx.TimeoutException as exc:
            raise SpeechUpstreamError("语音转写超时，请缩短录音后重试") from exc
        except httpx.HTTPError as exc:
            logger.warning("speech_transcription_transport_error", error=str(exc))
            raise SpeechUpstreamError("无法连接语音转写服务，请稍后重试") from exc

        if response.status_code >= 400:
            _log_rejection("transcription", response.status_code, response.text)
            raise SpeechUpstreamError(
                f"语音转写服务返回错误（HTTP {response.status_code}），"
                "请检查 AI_TRANSCRIPTION_* 配置"
            )

        return _parse_transcription(response)

    @property
    def model(self) -> str:
        return self._model


class OpenAITTSClient:
    """语音合成客户端。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        voice: str = "alloy",
        timeout_seconds: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._voice = voice
        self._timeout = timeout_seconds

    async def synthesize(self, text: str, *, voice: str | None = None) -> SynthesisResult:
        payload = {
            "model": self._model,
            "input": text,
            "voice": voice or self._voice,
            "response_format": TTS_RESPONSE_FORMAT,
        }
        timeout = httpx.Timeout(self._timeout, connect=10.0)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{self._base_url}/audio/speech",
                    json=payload,
                    headers=_auth_headers(self._api_key),
                )
        except httpx.TimeoutException as exc:
            raise SpeechUpstreamError("语音合成超时，请稍后重试") from exc
        except httpx.HTTPError as exc:
            logger.warning("speech_synthesis_transport_error", error=str(exc))
            raise SpeechUpstreamError("无法连接语音合成服务，请稍后重试") from exc

        if response.status_code >= 400:
            _log_rejection("synthesis", response.status_code, response.text)
            raise SpeechUpstreamError(
                f"语音合成服务返回错误（HTTP {response.status_code}），请检查 AI_TTS_* 配置"
            )

        if not response.content:
            # 空音频不能落库：前端会得到一个无法播放的 <audio>
            raise SpeechUpstreamError("语音合成返回空音频，请稍后重试")

        media_type = response.headers.get("content-type", "").split(";")[0].strip()
        return SynthesisResult(
            audio=response.content,
            media_type=media_type or "audio/mpeg",
        )

    @property
    def model(self) -> str:
        return self._model


def _parse_transcription(response: httpx.Response) -> TranscriptionResult:
    """兼容两种网关行为：JSON `{"text": ...}` 与纯文本正文。"""
    content_type = response.headers.get("content-type", "")
    if "json" in content_type:
        try:
            payload = response.json()
        except ValueError as exc:
            logger.warning("speech_transcription_bad_json", body=response.text[:ERROR_BODY_LIMIT])
            raise SpeechUpstreamError("语音转写返回了无法解析的内容") from exc
        if isinstance(payload, dict):
            text = payload.get("text")
            if not isinstance(text, str):
                raise SpeechUpstreamError("语音转写返回缺少 text 字段")
            duration = payload.get("duration")
            language = payload.get("language")
            return TranscriptionResult(
                text=text.strip(),
                language=language if isinstance(language, str) else None,
                duration_seconds=(float(duration) if isinstance(duration, (int, float)) else None),
            )
        if isinstance(payload, str):
            return TranscriptionResult(text=payload.strip())
        raise SpeechUpstreamError("语音转写返回结构不符合预期")

    # 少数网关忽略 response_format，直接返回纯文本
    return TranscriptionResult(text=response.text.strip())


__all__ = ["OpenAITTSClient", "OpenAITranscriptionClient"]
