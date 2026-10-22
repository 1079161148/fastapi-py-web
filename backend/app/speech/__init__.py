"""语音能力（ASR / TTS，设计文档 9.3）。

目录边界（设计文档 4.2）：语音 SDK 与音频处理只允许出现在本目录，
业务层只见 `TranscriptionClient` / `SpeechClient` 两个 Protocol。
"""

from app.speech.base import (
    SpeechClient,
    SpeechNotConfiguredError,
    SpeechUpstreamError,
    SynthesisResult,
    TranscriptionClient,
    TranscriptionResult,
)
from app.speech.factory import get_speech_client, get_transcription_client

__all__ = [
    "SpeechClient",
    "SpeechNotConfiguredError",
    "SpeechUpstreamError",
    "SynthesisResult",
    "TranscriptionClient",
    "TranscriptionResult",
    "get_speech_client",
    "get_transcription_client",
]
