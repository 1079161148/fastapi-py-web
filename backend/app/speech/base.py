"""语音能力接口（设计文档 3.1：语音 SDK 只允许出现在 `speech/` 内）。

上层（chat / voice service）只依赖这里的 Protocol 与数据结构：
换供应商、换协议（OpenAI 兼容 → 本地 faster-whisper）只改本目录实现。

两条硬约束：
1. **不静默失败**：未配置抛 SpeechNotConfiguredError（503 expose），
   上游异常抛 SpeechUpstreamError（502），绝不返回空字符串让上层以为"没说话"
2. **时长与格式由调用方先校验**：这里只负责传输，不做"能否接受这个文件"的判断
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from app.core.exceptions import AppError


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    """转写结果：text 必填，其余为网关能提供时的附加信息。"""

    text: str
    language: str | None = None
    duration_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class SynthesisResult:
    """合成结果：音频字节 + MIME。

    duration_seconds 常为空（mp3 帧头解析不值得为此引入依赖），
    前端由 <audio> 元数据自行获得时长。
    """

    audio: bytes
    media_type: str
    duration_seconds: float | None = None


class SpeechNotConfiguredError(AppError):
    """语音能力未配置：必须显式 503，并在消息里指出缺哪个变量。

    设计文档 10 的降级要求是"ASR/TTS 失败 → 降级为纯文字并提示"，
    因此这条错误必须 expose：前端要能明确告诉用户"语音不可用，请打字"。
    """

    def __init__(self, missing: Sequence[str], *, capability: str) -> None:
        super().__init__(
            code="SPEECH_NOT_CONFIGURED",
            message=(
                f"语音{capability}未配置：请在 backend/.env 中设置 "
                + " / ".join(missing)
                + " 后重启服务；未配置时请改用文字输入"
            ),
            status_code=503,
            expose=True,
        )
        self.missing = list(missing)
        self.capability = capability


class SpeechUpstreamError(AppError):
    """语音上游错误（超时 / 非 2xx / 协议异常）。

    detail 一律 None：上游原始报文可能含网关内部信息，只进日志。
    """

    def __init__(self, message: str) -> None:
        super().__init__(
            code="SPEECH_UPSTREAM_ERROR",
            message=message,
            status_code=502,
            expose=True,
        )


class TranscriptionClient(Protocol):
    async def transcribe(
        self,
        audio: bytes,
        *,
        filename: str,
        mime_type: str,
        language: str | None = None,
    ) -> TranscriptionResult:
        """把音频转写成文本。空结果由调用方判断为失败，本层不臆造内容。"""
        ...


class SpeechClient(Protocol):
    async def synthesize(self, text: str, *, voice: str | None = None) -> SynthesisResult:
        """把文本合成为音频。空音频同样抛错，不返回 0 字节。"""
        ...


__all__ = [
    "SpeechClient",
    "SpeechNotConfiguredError",
    "SpeechUpstreamError",
    "SynthesisResult",
    "TranscriptionClient",
    "TranscriptionResult",
]
