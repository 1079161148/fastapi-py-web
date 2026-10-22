"""语音能力与合成结果（M5，设计文档 9.3）。"""

import uuid

from pydantic import BaseModel, Field


class VoiceCapabilities(BaseModel):
    """前端据此决定是否显示麦克风按钮，避免"录完才告知不支持"。

    missing_* 直接给变量名：部署方看到就能补配置，不必翻代码。
    """

    transcription: bool
    tts: bool
    max_seconds: int
    max_mb: int
    missing_transcription: list[str] = Field(default_factory=list)
    missing_tts: list[str] = Field(default_factory=list)


class SpeechSynthesisResult(BaseModel):
    """单条消息的语音合成结果（幂等：已合成过的直接返回缓存）。"""

    message_id: uuid.UUID
    audio_url: str
    mime_type: str
    duration_seconds: float | None = None
    # true = 命中缓存，未重新调用上游（省费用，也保证同一句话音色一致）
    cached: bool = False
