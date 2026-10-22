"""消息事件出口（M6）。

chat（AI 回复）、agent（坐席回复）、channels（渠道入站）都会产生消息，
坐席端与用户端通过同一个事件信封收到它。广播逻辑集中在这里，
避免每个写消息的地方各拼一份 payload——字段不一致的问题往往就是这样来的。
"""

from typing import Any

from app.core.pubsub import EVENT_MESSAGE_CREATED, RealtimeEvent, publish
from app.db.models.conversation import Message
from app.services import assets as asset_service
from app.services import voice as voice_service


def audio_url_for(message: Message) -> str | None:
    """带语音的消息返回短时播放地址，否则 None（坐席要能听用户语音）。"""
    if voice_service.find_audio_attachment(message) is None:
        return None
    return voice_service.audio_url(message_id=message.id, tenant_id=message.tenant_id)


def attachments_for(message: Message) -> list[dict[str, Any]]:
    """附件对外形态：图片补短时 URL，storage_key 一律不外发。

    库里存的是存储路径，直接下发等于告诉前端"文件在哪儿"，
    既没意义也方便不了别人，只是平白多一处信息泄露面。
    """
    result: list[dict[str, Any]] = []
    for item in message.attachments or []:
        if not isinstance(item, dict):
            continue
        clean = {key: value for key, value in item.items() if key != "storage_key"}
        if clean.get("type") == asset_service.ATTACHMENT_TYPE_IMAGE:
            clean["url"] = asset_service.image_url(
                message_id=message.id, tenant_id=message.tenant_id
            )
        result.append(clean)
    return result


def message_payload(message: Message) -> dict[str, Any]:
    """SSE / 实时事件共用的消息结构（与 chat 的 message 事件保持一致）。"""
    return {
        "id": str(message.id),
        "conversation_id": str(message.conversation_id),
        "role": message.role,
        "content": message.content,
        "message_type": message.message_type,
        "source": message.source,
        "attachments": attachments_for(message),
        "audio_url": audio_url_for(message),
        "tokens": message.tokens,
        "latency_ms": message.latency_ms,
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }


async def publish_message_event(message: Message) -> None:
    """广播到"租户频道 + 会话频道"；失败只记日志，不影响已落库的消息。"""
    await publish(
        [
            RealtimeEvent(
                type=EVENT_MESSAGE_CREATED,
                tenant_id=message.tenant_id,
                conversation_id=message.conversation_id,
                data=message_payload(message),
            )
        ]
    )


__all__ = ["attachments_for", "audio_url_for", "message_payload", "publish_message_event"]
