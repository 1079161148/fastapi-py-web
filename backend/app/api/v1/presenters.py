"""ORM → 响应模型的组装（含依赖请求才能生成的派生字段）。

存在的理由：同一个消息结构在四个地方返回（用户拉历史、坐席拉历史、坐席回复、
流式 message 事件），而音频播放地址需要现场签发。集中一处，避免某条路径
漏签导致"历史能放、新消息不能放"这种只在特定入口复现的问题。
"""

import time
from collections.abc import AsyncIterator, Callable

from app.core.pubsub import RealtimeEvent, Subscription
from app.db.models.conversation import Message
from app.schemas.chat import MessageAttachment, MessageInfo
from app.services import messages as message_service


def message_info(message: Message) -> MessageInfo:
    info = MessageInfo.model_validate(message)
    info.audio_url = message_service.audio_url_for(message)
    # 附件要走同一套对外形态：直接 model_validate 会把 storage_key 带出去
    info.attachments = [
        MessageAttachment.model_validate(item) for item in message_service.attachments_for(message)
    ]
    return info


def realtime_frame(event: RealtimeEvent) -> str:
    """实时事件 → SSE 帧：事件名即 type，data 是完整信封（含 conversation_id）。"""
    return f"event: {event.type}\ndata: {event.to_json()}\n\n"


def sse_ping_frame() -> str:
    """SSE 注释行心跳：防止 Nginx/企业代理按空闲连接掐断。"""
    return ": ping\n\n"


async def realtime_frames(
    subscription: Subscription,
    *,
    heartbeat_seconds: int,
    accept: Callable[[RealtimeEvent], bool] | None = None,
) -> AsyncIterator[str]:
    """订阅 → SSE 帧，带心跳节流。

    订阅迭代器每秒都会产出 None（便于及时发现断开），但心跳不能每秒发一条：
    按 `heartbeat_seconds` 节流，既维持连接又不刷无意义字节。
    """
    last_ping = time.monotonic()
    async for event in subscription.events():
        if event is None:
            now = time.monotonic()
            if now - last_ping >= heartbeat_seconds:
                last_ping = now
                yield sse_ping_frame()
            continue
        last_ping = time.monotonic()
        if accept is not None and not accept(event):
            continue
        yield realtime_frame(event)
