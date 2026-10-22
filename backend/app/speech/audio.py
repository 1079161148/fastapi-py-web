"""音频校验与元数据处理（设计文档 9.3 成本与体验控制）。

只做三件事：**认格式、算时长、拼存储 key**。解码/重采样一律交给 ASR 网关，
本地不做转码——ffmpeg 依赖重，且转码只会增加失败面。

格式判定基于文件头（魔数）而不是客户端声明的 Content-Type：
后者可伪造，直接决定 ASR 网关能否解析。MIME 也一律由魔数反推，
保证"库里存的 MIME"与"实际字节"一致。

时长：wav 头部可精确解析；ogg/webm/mp4 的时长需要解析容器索引，
为此引入依赖不划算。其余格式采用客户端上报时长（MediaRecorder 计时），
并以**文件大小 + ASR 侧超时**兜底成本。这条取舍写在 docs 里，不藏着。
"""

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from app.core.exceptions import AppError

# 白名单：覆盖浏览器 MediaRecorder 与常见手机录音
SUPPORTED_FORMATS: Final[dict[str, tuple[str, str]]] = {
    "wav": ("audio/wav", ".wav"),
    "ogg": ("audio/ogg", ".ogg"),
    "webm": ("audio/webm", ".webm"),
    "mp3": ("audio/mpeg", ".mp3"),
    "m4a": ("audio/mp4", ".m4a"),
}

# 录音时长与上报值之间允许的误差（秒）：MediaRecorder 计时与真实时长常有抖动
DURATION_TOLERANCE_SECONDS: Final = 0.5


@dataclass(frozen=True, slots=True)
class AudioInfo:
    format: str
    mime_type: str
    suffix: str
    duration_seconds: float | None
    size_bytes: int


def sniff_format(data: bytes) -> str | None:
    """按文件头判断容器格式；不认识返回 None。"""
    if len(data) < 12:
        return None
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[4:8] == b"ftyp":
        # ISO BMFF：Safari 的 MediaRecorder 产出 audio/mp4（.m4a）
        return "m4a"
    if data[:4] == b"OggS":
        return "ogg"
    if data[:4] == b"\x1aE\xdf\xa3":
        # EBML：Chrome/Firefox 的 MediaRecorder 默认 audio/webm;codecs=opus
        return "webm"
    if data[:3] == b"ID3":
        return "mp3"
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        # MPEG 音频帧同步字（0xFFEx / 0xFFFx）
        return "mp3"
    return None


def wav_duration_seconds(data: bytes) -> float | None:
    """解析 RIFF/WAVE 的 fmt + data 块得到精确时长。解析失败返回 None。"""
    if len(data) < 12:
        return None

    byte_rate = 0
    offset = 12
    while offset + 8 <= len(data):
        chunk_id = data[offset : offset + 4]
        chunk_size = int.from_bytes(data[offset + 4 : offset + 8], "little")
        body = offset + 8

        if chunk_id == b"fmt " and chunk_size >= 16 and body + 12 <= len(data):
            byte_rate = int.from_bytes(data[body + 8 : body + 12], "little")
        elif chunk_id == b"data":
            if byte_rate <= 0:
                return None
            available = min(chunk_size, len(data) - body)
            return available / byte_rate if available > 0 else None

        # RIFF 块按偶数字节对齐，奇数长度后有一个填充字节
        offset = body + chunk_size + (chunk_size & 1)

    return None


def inspect_audio(
    data: bytes,
    *,
    declared_duration: float | None,
    max_seconds: int,
    max_bytes: int,
) -> AudioInfo:
    """校验并返回音频元信息；任何不合规都抛 AppError（带可操作提示）。"""
    if not data:
        raise AppError(code="AUDIO_EMPTY", message="录音内容为空，请重新录制", status_code=422)
    if len(data) > max_bytes:
        raise AppError(
            code="AUDIO_TOO_LARGE",
            message=f"音频超过 {max_bytes // (1024 * 1024)}MB 上限，请缩短录音",
            status_code=413,
        )

    detected = sniff_format(data)
    if detected is None:
        raise AppError(
            code="AUDIO_FORMAT_UNSUPPORTED",
            message="不支持的音频格式，请使用浏览器录音（webm/ogg/mp4）或 wav/mp3",
            status_code=415,
        )

    mime_type, suffix = SUPPORTED_FORMATS[detected]
    declared = declared_duration if declared_duration and declared_duration > 0 else None
    duration = wav_duration_seconds(data) if detected == "wav" else None
    duration = duration or declared

    if duration is not None and duration > max_seconds + DURATION_TOLERANCE_SECONDS:
        raise AppError(
            code="AUDIO_TOO_LONG",
            message=f"录音超过 {max_seconds} 秒上限，请缩短后重新录制",
            status_code=422,
            detail={"max_seconds": max_seconds, "duration_seconds": round(duration, 2)},
        )

    return AudioInfo(
        format=detected,
        mime_type=mime_type,
        suffix=suffix,
        duration_seconds=round(duration, 2) if duration is not None else None,
        size_bytes=len(data),
    )


def build_object_key(
    *, tenant_id: UUID, conversation_id: UUID, message_id: UUID, suffix: str
) -> str:
    """存储 key 由服务端拼装（不接受用户输入）：

    voice/{tenant}/{conversation}/{message}{suffix}
    以 message_id 命名，使"音频文件 ↔ 消息"一一对应，排障时无需查库。
    """
    return f"voice/{tenant_id}/{conversation_id}/{message_id}{suffix}"


def audio_attachment(info: AudioInfo, object_key: str) -> dict[str, object]:
    """messages.attachments 的单个元素（设计文档 6：存 key、时长、大小）。"""
    return {
        "type": "audio",
        "object_key": object_key,
        "mime_type": info.mime_type,
        "duration_seconds": info.duration_seconds,
        "size_bytes": info.size_bytes,
    }


__all__ = [
    "AudioInfo",
    "SUPPORTED_FORMATS",
    "audio_attachment",
    "build_object_key",
    "inspect_audio",
    "sniff_format",
    "wav_duration_seconds",
]
