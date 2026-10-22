"""语音消息闭环测试（M5，设计文档 9.3）。

覆盖：
- 语音消息：上传 → 转写 → 复用文本编排 → 落库（message_type=audio + 附件）
- 输入校验：格式/时长/大小/越权，失败时**不产生消息**
- TTS：按需合成、缓存命中不重复计费、只允许 assistant 消息
- 音频播放：短时令牌认证，令牌与消息不匹配一律拒绝
- 降级：ASR/TTS 未配置时明确 503，不静默返回空文本

ASR/TTS 用假实现：语音网关的真实调用只在人工端到端验证时进行，
测试关注的是"编排与鉴权正确"，不是供应商可用性。
"""

import struct
import uuid
from typing import Any

import pytest
from chat_helpers import FakeLLM, auth_headers, parse_sse, register_user
from httpx import AsyncClient

from app.api.v1 import deps
from app.core.config import get_settings
from app.core.security import create_audio_token
from app.main import app
from app.speech.audio import build_object_key, wav_duration_seconds
from app.speech.base import SynthesisResult, TranscriptionResult


def make_wav(seconds: float, *, sample_rate: int = 8000) -> bytes:
    """构造最小合法 wav（16bit 单声道 PCM），用于验证魔数与时长解析。"""
    byte_rate = sample_rate * 2
    payload = b"\x00\x00" * int(sample_rate * seconds)
    fmt = struct.pack("<HHIIHH", 1, 1, sample_rate, byte_rate, 2, 16)
    body = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body


class FakeTranscriber:
    def __init__(self, text: str = "我想问一下退款什么时候到账") -> None:
        self.text = text
        self.calls = 0
        self.last_mime = ""

    async def transcribe(
        self,
        audio: bytes,
        *,
        filename: str,
        mime_type: str,
        language: str | None = None,
    ) -> TranscriptionResult:
        self.calls += 1
        self.last_mime = mime_type
        return TranscriptionResult(text=self.text)


class FakeSpeech:
    def __init__(self) -> None:
        self.calls = 0
        self.audio = b"ID3\x03\x00\x00\x00fake-mp3-payload"

    async def synthesize(self, text: str, *, voice: str | None = None) -> SynthesisResult:
        self.calls += 1
        return SynthesisResult(audio=self.audio, media_type="audio/mpeg")


@pytest.fixture
def fake_transcriber() -> FakeTranscriber:
    transcriber = FakeTranscriber()
    app.dependency_overrides[deps.provide_transcription_client] = lambda: transcriber
    try:
        yield transcriber
    finally:
        app.dependency_overrides.pop(deps.provide_transcription_client, None)


@pytest.fixture
def fake_speech() -> FakeSpeech:
    speech = FakeSpeech()
    app.dependency_overrides[deps.provide_speech_client] = lambda: speech
    app.dependency_overrides[deps.provide_required_speech_client] = lambda: speech
    try:
        yield speech
    finally:
        app.dependency_overrides.pop(deps.provide_speech_client, None)
        app.dependency_overrides.pop(deps.provide_required_speech_client, None)


async def _setup(client: AsyncClient, email: str) -> tuple[dict[str, str], str]:
    data = await register_user(client, email)
    return auth_headers(data["tokens"]["access_token"]), data["user"]["tenant_id"]


async def _create_conversation(client: AsyncClient, headers: dict[str, str]) -> str:
    resp = await client.post("/api/v1/conversations", json={}, headers=headers)
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _post_audio(
    client: AsyncClient,
    headers: dict[str, str],
    conversation_id: str,
    audio: bytes,
    *,
    filename: str = "voice.wav",
    duration: float = 1.0,
) -> list[tuple[str, dict[str, Any]]]:
    resp = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages/audio",
        files={"file": (filename, audio, "application/octet-stream")},
        data={"duration_seconds": str(duration)},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return parse_sse(resp.text)


def _event_names(events: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [name for name, _ in events]


def _first(events: list[tuple[str, dict[str, Any]]], name: str) -> dict[str, Any]:
    for event_name, data in events:
        if event_name == name:
            return data
    raise AssertionError(f"缺少 {name} 事件：{_event_names(events)}")


async def test_audio_message_transcribes_and_persists(
    client: AsyncClient,
    unique_email: str,
    fake_llm: FakeLLM,
    fake_transcriber: FakeTranscriber,
) -> None:
    headers, _ = await _setup(client, unique_email)
    conversation_id = await _create_conversation(client, headers)
    wav = make_wav(1.0)

    events = await _post_audio(client, headers, conversation_id, wav)

    # 事件顺序：先告知进入转写，再出用户消息，最后才是生成与收尾
    names = _event_names(events)
    assert names[0] == "transcribing"
    assert names[1] == "message"
    assert names[-1] == "done"
    assert "delta" in names

    user_message = _first(events, "message")
    assert user_message["message_type"] == "audio"
    assert user_message["content"] == fake_transcriber.text
    assert user_message["audio_url"], "用户语音必须可回放"
    assert user_message["attachments"][0]["mime_type"] == "audio/wav"
    assert user_message["attachments"][0]["duration_seconds"] == pytest.approx(1.0, abs=0.05)
    # 服务端按魔数判定 MIME，不采信客户端声明
    assert fake_transcriber.last_mime == "audio/wav"

    history = await client.get(f"/api/v1/conversations/{conversation_id}/messages", headers=headers)
    items = history.json()["items"]
    assert [item["message_type"] for item in items] == ["audio", "text"]
    assert items[0]["content"] == fake_transcriber.text
    assert items[0]["audio_url"]
    assert items[1]["role"] == "assistant"

    audio_resp = await client.get(items[0]["audio_url"])
    assert audio_resp.status_code == 200
    assert audio_resp.headers["content-type"].startswith("audio/wav")
    assert audio_resp.content == wav, "回放必须是原始录音，不能二次编码"


async def test_audio_message_rejects_unsupported_format(
    client: AsyncClient,
    unique_email: str,
    fake_llm: FakeLLM,
    fake_transcriber: FakeTranscriber,
) -> None:
    headers, _ = await _setup(client, unique_email)
    conversation_id = await _create_conversation(client, headers)

    events = await _post_audio(client, headers, conversation_id, b"not-an-audio-file" * 100)
    error = _first(events, "error")
    assert error["code"] == "AUDIO_FORMAT_UNSUPPORTED"
    assert fake_transcriber.calls == 0, "格式非法时不应浪费一次转写调用"

    history = await client.get(f"/api/v1/conversations/{conversation_id}/messages", headers=headers)
    assert history.json()["items"] == [], "失败请求不能留下空消息"


async def test_audio_message_rejects_too_long_recording(
    client: AsyncClient,
    unique_email: str,
    fake_llm: FakeLLM,
    fake_transcriber: FakeTranscriber,
) -> None:
    headers, _ = await _setup(client, unique_email)
    conversation_id = await _create_conversation(client, headers)
    settings = get_settings()

    # wav 时长由服务端精确解析，与客户端上报值无关
    too_long = make_wav(settings.voice_max_seconds + 5, sample_rate=1000)
    events = await _post_audio(client, headers, conversation_id, too_long, duration=1.0)
    error = _first(events, "error")
    assert error["code"] == "AUDIO_TOO_LONG"
    assert error["retryable"] is False
    assert fake_transcriber.calls == 0


async def test_audio_message_rejects_oversized_file(
    client: AsyncClient,
    unique_email: str,
    fake_llm: FakeLLM,
    fake_transcriber: FakeTranscriber,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers, _ = await _setup(client, unique_email)
    conversation_id = await _create_conversation(client, headers)
    monkeypatch.setattr(get_settings(), "voice_max_mb", 1)

    wav = make_wav(1.0)
    oversized = wav + b"\x00" * (1024 * 1024 + 1)
    resp = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages/audio",
        files={"file": ("voice.wav", oversized, "audio/wav")},
        data={"duration_seconds": "1.0"},
        headers=headers,
    )
    # 超限在响应头之前就被拒绝：HTTP 状态码可用，不必等流内 error 事件
    assert resp.status_code == 413
    assert resp.json()["code"] == "AUDIO_TOO_LARGE"


async def test_audio_message_requires_transcription_config(
    client: AsyncClient, unique_email: str, fake_llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ASR 未配置必须 503 并指出缺哪个变量，不能静默当成"没说话"。"""
    headers, _ = await _setup(client, unique_email)
    conversation_id = await _create_conversation(client, headers)
    settings = get_settings()
    monkeypatch.setattr(settings, "ai_transcription_model", "")
    monkeypatch.setattr(settings, "ai_transcription_api_key", "")
    monkeypatch.setattr(settings, "ai_api_key", "")

    resp = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages/audio",
        files={"file": ("voice.wav", make_wav(1.0), "audio/wav")},
        data={"duration_seconds": "1.0"},
        headers=headers,
    )
    assert resp.status_code == 503
    body = resp.json()
    assert body["code"] == "SPEECH_NOT_CONFIGURED"
    assert "AI_TRANSCRIPTION_MODEL" in body["message"]


async def test_audio_message_respects_conversation_ownership(
    client: AsyncClient,
    unique_email: str,
    fake_llm: FakeLLM,
    fake_transcriber: FakeTranscriber,
) -> None:
    headers_a, _ = await _setup(client, unique_email)
    conversation_id = await _create_conversation(client, headers_a)

    other = await register_user(client, f"voice-b-{uuid.uuid4().hex[:8]}@example.com")
    headers_b = auth_headers(other["tokens"]["access_token"])

    resp = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages/audio",
        files={"file": ("voice.wav", make_wav(1.0), "audio/wav")},
        data={"duration_seconds": "1.0"},
        headers=headers_b,
    )
    assert resp.status_code == 404, "越权必须 404，不暴露会话是否存在"
    assert fake_transcriber.calls == 0


async def test_tts_is_cached_and_only_for_assistant(
    client: AsyncClient,
    unique_email: str,
    fake_llm: FakeLLM,
    fake_speech: FakeSpeech,
) -> None:
    headers, _ = await _setup(client, unique_email)
    conversation_id = await _create_conversation(client, headers)

    stream = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages/stream",
        json={"content": "退款多久到账"},
        headers=headers,
    )
    events = parse_sse(stream.text)
    assistant_id = _first(events, "done")["id"]
    user_id = _first(events, "message")["id"]

    first = await client.post(f"/api/v1/messages/{assistant_id}/tts", headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["cached"] is False
    assert fake_speech.calls == 1

    second = await client.post(f"/api/v1/messages/{assistant_id}/tts", headers=headers)
    assert second.status_code == 200
    assert second.json()["cached"] is True
    assert fake_speech.calls == 1, "重复点击不能重复计费"

    audio = await client.get(second.json()["audio_url"])
    assert audio.status_code == 200
    assert audio.content == fake_speech.audio

    user_tts = await client.post(f"/api/v1/messages/{user_id}/tts", headers=headers)
    assert user_tts.status_code == 422
    assert user_tts.json()["code"] == "TTS_ROLE_NOT_ALLOWED"


async def test_voice_reply_enabled_appends_audio_event(
    client: AsyncClient,
    unique_email: str,
    fake_llm: FakeLLM,
    fake_speech: FakeSpeech,
) -> None:
    headers, _ = await _setup(client, unique_email)
    conversation_id = await _create_conversation(client, headers)

    patched = await client.patch(
        f"/api/v1/conversations/{conversation_id}",
        json={"voice_reply_enabled": True},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["voice_reply_enabled"] is True

    stream = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages/stream",
        json={"content": "营业时间"},
        headers=headers,
    )
    events = parse_sse(stream.text)
    names = _event_names(events)
    assert "audio" in names
    # 音频必须晚于 done：先让用户看到文字，合成慢不该阻塞阅读
    assert names.index("audio") > names.index("done")
    audio_event = _first(events, "audio")
    assert audio_event["message_id"] == _first(events, "done")["id"]

    history = await client.get(f"/api/v1/conversations/{conversation_id}/messages", headers=headers)
    assistant = history.json()["items"][-1]
    assert assistant["audio_url"], "语音回复要能回看"
    assert (await client.get(assistant["audio_url"])).status_code == 200


async def test_voice_reply_without_tts_config_reports_failure(
    client: AsyncClient, unique_email: str, fake_llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """开了语音回复但服务端没配 TTS：明确告知，而不是让用户等一段永远不来的音频。"""
    headers, _ = await _setup(client, unique_email)
    conversation_id = await _create_conversation(client, headers)
    settings = get_settings()
    monkeypatch.setattr(settings, "ai_tts_model", "")

    await client.patch(
        f"/api/v1/conversations/{conversation_id}",
        json={"voice_reply_enabled": True},
        headers=headers,
    )
    stream = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages/stream",
        json={"content": "营业时间"},
        headers=headers,
    )
    events = parse_sse(stream.text)
    failed = _first(events, "audio_failed")
    assert failed["code"] == "SPEECH_NOT_CONFIGURED"
    # 文字回答不受影响
    assert _first(events, "done")["content"]


async def test_audio_token_is_bound_to_message(
    client: AsyncClient,
    unique_email: str,
    fake_llm: FakeLLM,
    fake_speech: FakeSpeech,
) -> None:
    headers, tenant_id = await _setup(client, unique_email)
    conversation_id = await _create_conversation(client, headers)

    stream = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages/stream",
        json={"content": "你好"},
        headers=headers,
    )
    message_id = _first(parse_sse(stream.text), "done")["id"]
    await client.post(f"/api/v1/messages/{message_id}/tts", headers=headers)

    other_id = uuid.uuid4()
    stolen = create_audio_token(message_id=other_id, tenant_id=uuid.UUID(tenant_id))
    resp = await client.get(f"/api/v1/messages/{message_id}/audio?token={stolen}")
    assert resp.status_code == 403
    assert resp.json()["code"] == "AUDIO_TOKEN_MISMATCH"

    forged = await client.get(f"/api/v1/messages/{message_id}/audio?token=not-a-token")
    assert forged.status_code == 401


async def test_voice_capabilities_reflects_configuration(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "ai_transcription_model", "")
    monkeypatch.setattr(settings, "ai_tts_model", "")

    disabled = await client.get("/api/v1/voice/capabilities")
    assert disabled.status_code == 200
    body = disabled.json()
    assert body["transcription"] is False and body["tts"] is False
    assert "AI_TRANSCRIPTION_MODEL" in body["missing_transcription"]
    assert "AI_TTS_MODEL" in body["missing_tts"]
    assert body["max_seconds"] == settings.voice_max_seconds

    monkeypatch.setattr(settings, "ai_transcription_model", "whisper-1")
    monkeypatch.setattr(settings, "ai_tts_model", "tts-1")
    monkeypatch.setattr(settings, "ai_api_key", "test-key")
    monkeypatch.setattr(settings, "ai_base_url", "http://127.0.0.1:9/v1")

    enabled = await client.get("/api/v1/voice/capabilities")
    assert enabled.json()["transcription"] is True
    assert enabled.json()["tts"] is True


def test_wav_duration_parser_handles_truncated_input() -> None:
    """坏文件不能让上传接口 500：解析不出来就退回客户端上报值。"""
    assert wav_duration_seconds(b"RIFF\x00\x00\x00\x00WAVE") is None
    assert wav_duration_seconds(b"") is None
    assert wav_duration_seconds(make_wav(1.0)[:30]) is None


def test_audio_object_key_is_tenant_scoped() -> None:
    """存储 key 以租户打头：便于按租户清理与配额统计。"""
    tenant_id, conversation_id, message_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    key = build_object_key(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        message_id=message_id,
        suffix=".wav",
    )
    assert key == f"voice/{tenant_id}/{conversation_id}/{message_id}.wav"
