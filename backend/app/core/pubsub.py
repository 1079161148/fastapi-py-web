"""实时事件通道（设计文档 5.4）：Redis Pub/Sub + SSE 的数据面。

为什么是 Pub/Sub 而不是进程内广播：API 可能跑多个 uvicorn worker，
产生事件的请求（用户发言、坐席认领）与持有 SSE 连接的进程**不一定同一个**。
Pub/Sub 天然跨进程，且不需要持久化——离线期间错过的事件由"重连后拉一次
历史"补齐（前端负责，见 AgentView 的重连逻辑）。

两个频道，订阅方不同：
- `aics:agent:{tenant_id}`：坐席工作台，收全租户的工单与消息
- `aics:conv:{conversation_id}`：单个对话，用户端只收自己这段会话

失效策略：发布失败**不影响主流程**（工单已建、消息已落库，实时推送是附属能力），
但必须记 error 日志——静默吞掉会让"坐席收不到通知"变成一个查不出来的问题。
订阅侧则相反：Redis 不可用时 SSE 端点直接 503，不让前端挂着一个永远没消息的连接。
"""

import json
import uuid
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from redis.asyncio.client import PubSub

from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.logging import get_logger

logger = get_logger("pubsub")

# 事件类型（前端按 type 分发）
EVENT_TICKET_CREATED = "ticket.created"
EVENT_TICKET_CLAIMED = "ticket.claimed"
EVENT_TICKET_RESOLVED = "ticket.resolved"
# 用户主动取消排队：坐席队列要把这张单摘掉，不能留着让人去接
EVENT_TICKET_CANCELLED = "ticket.cancelled"
EVENT_MESSAGE_CREATED = "message.created"
EVENT_CONVERSATION_UPDATED = "conversation.updated"
EVENT_GUEST_MERGED = "guest.merged"

CHANNEL_PREFIX_AGENT = "aics:agent"
CHANNEL_PREFIX_CONVERSATION = "aics:conv"

_redis: Redis | None = None


@dataclass(frozen=True, slots=True)
class RealtimeEvent:
    """统一事件信封。

    前端只依赖 type + data 两个字段，新增事件类型不需要改传输层；
    tenant_id / conversation_id 用于订阅方二次过滤（Pub/Sub 频道是粗粒度，
    会话频道里可能混入同租户其他会话的事件）。
    """

    type: str
    tenant_id: uuid.UUID
    data: dict[str, Any] = field(default_factory=dict)
    conversation_id: uuid.UUID | None = None
    ticket_id: uuid.UUID | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_json(self) -> str:
        return json.dumps(
            {
                "type": self.type,
                "tenant_id": str(self.tenant_id),
                "conversation_id": str(self.conversation_id) if self.conversation_id else None,
                "ticket_id": str(self.ticket_id) if self.ticket_id else None,
                "created_at": self.created_at.isoformat(),
                "data": self.data,
            },
            ensure_ascii=False,
            default=str,
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> "RealtimeEvent":
        payload = json.loads(raw)
        return cls(
            type=str(payload["type"]),
            tenant_id=uuid.UUID(payload["tenant_id"]),
            data=payload.get("data") or {},
            conversation_id=(
                uuid.UUID(payload["conversation_id"]) if payload.get("conversation_id") else None
            ),
            ticket_id=uuid.UUID(payload["ticket_id"]) if payload.get("ticket_id") else None,
            created_at=(
                datetime.fromisoformat(payload["created_at"])
                if payload.get("created_at")
                else datetime.now(UTC)
            ),
        )


def agent_channel(tenant_id: uuid.UUID) -> str:
    return f"{CHANNEL_PREFIX_AGENT}:{tenant_id}"


def conversation_channel(conversation_id: uuid.UUID) -> str:
    return f"{CHANNEL_PREFIX_CONVERSATION}:{conversation_id}"


async def get_redis() -> Redis:
    """懒创建单例：Redis 挂掉不应该影响 API 启动（健康检查仍需可用）。"""
    global _redis
    if _redis is None:
        settings = get_settings()
        _redis = Redis.from_url(settings.redis_url, decode_responses=True)
    return _redis


async def close_redis() -> None:
    """关闭连接并清空单例。

    先置 None 再关闭：关闭动作可能因"客户端属于已结束的事件循环"而失败
    （测试每个用例一个新循环），此时必须保证后续请求重建客户端，
    而不是一直拿着一个无法使用的实例。
    """
    global _redis
    if _redis is None:
        return
    client, _redis = _redis, None
    try:
        await client.aclose()
    except Exception:  # noqa: BLE001 - 循环已关闭时关闭失败无补救手段
        logger.debug("redis_close_failed")


async def publish(
    events: Iterable[RealtimeEvent], *, channels: Iterable[str] | None = None
) -> bool:
    """发布事件到指定频道；失败只记日志并返回 False（不影响已完成的业务写入）。

    默认同时发到"租户频道 + 会话频道"：坐席要全量，用户只要自己那段。
    """
    event_list = list(events)
    if not event_list:
        return True

    targets = list(channels) if channels is not None else _default_channels(event_list)
    if not targets:
        return True

    try:
        client = await get_redis()
        async with client.pipeline(transaction=False) as pipe:
            for channel in targets:
                for event in event_list:
                    pipe.publish(channel, event.to_json())
            await pipe.execute()
        return True
    except Exception:  # noqa: BLE001 - Redis 不可用不能把已成功的业务操作变成失败
        logger.exception(
            "realtime_publish_failed",
            channels=targets,
            types=[event.type for event in event_list],
        )
        return False


def _default_channels(events: list[RealtimeEvent]) -> list[str]:
    channels: set[str] = set()
    for event in events:
        channels.add(agent_channel(event.tenant_id))
        if event.conversation_id is not None:
            channels.add(conversation_channel(event.conversation_id))
    return sorted(channels)


class Subscription:
    """一次订阅的生命周期：`open_subscription` 建立 → `events()` 迭代 → `close()` 释放。

    为什么不在生成器里 `async with`：SSE 端点在返回 StreamingResponse 之前
    就必须知道 Redis 能不能连上。若把建连推迟到生成器内部，连接失败时
    响应头已经发出（HTTP 200），前端只能收到一个静默断开的流。
    """

    def __init__(self, pubsub: PubSub, channels: tuple[str, ...]) -> None:
        self._pubsub = pubsub
        self._channels = channels
        self._closed = False

    @property
    def channels(self) -> tuple[str, ...]:
        return self._channels

    async def events(self) -> AsyncIterator[RealtimeEvent | None]:
        """空闲超过 1s 产出 None，调用方据此发心跳注释行。"""
        while not self._closed:
            message = await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message is None:
                yield None
                continue
            try:
                yield RealtimeEvent.from_json(message["data"])
            except (KeyError, ValueError):
                # 坏消息（例如手工往频道里塞数据）跳过，不中断整条订阅
                logger.warning("realtime_event_invalid", channel=str(message.get("channel")))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._pubsub.unsubscribe(*self._channels)
            # redis-py 5.3 的 PubSub.aclose 缺返回标注，strict mypy 判为 untyped call；
            # 上游未修前只能局部抑制（close() 已废弃，不能替代）
            await self._pubsub.aclose()  # type: ignore[no-untyped-call]
        except Exception:  # noqa: BLE001 - 连接已断时关闭失败无补救手段
            logger.debug("realtime_unsubscribe_failed", channels=list(self._channels))


async def open_subscription(*channels: str) -> Subscription:
    if not channels:
        raise ValueError("至少订阅一个频道")

    try:
        client = await get_redis()
        pubsub = client.pubsub()
        await pubsub.subscribe(*channels)
    except Exception as exc:  # noqa: BLE001 - 连接失败要给出可操作提示
        logger.warning("realtime_subscribe_failed", error=str(exc))
        raise AppError(
            code="REALTIME_UNAVAILABLE",
            message="实时通知不可用：请确认 Redis 已启动（docker compose up -d redis）",
            status_code=503,
            expose=True,
        ) from exc
    return Subscription(pubsub, tuple(channels))


__all__ = [
    "EVENT_CONVERSATION_UPDATED",
    "EVENT_GUEST_MERGED",
    "EVENT_MESSAGE_CREATED",
    "EVENT_TICKET_CANCELLED",
    "EVENT_TICKET_CLAIMED",
    "EVENT_TICKET_CREATED",
    "EVENT_TICKET_RESOLVED",
    "RealtimeEvent",
    "Subscription",
    "agent_channel",
    "close_redis",
    "conversation_channel",
    "get_redis",
    "open_subscription",
    "publish",
]
