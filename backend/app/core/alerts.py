"""告警出口（M7，设计文档 10「可观测」）。

默认行为：写一条 `level=warning` 的结构化日志——运维只要有日志采集就能告警。
配置 `ALERT_WEBHOOK_URL` 后额外推送一条 JSON 到 webhook（钉钉/飞书/Slack
的自定义机器人都是这个形态）。

为什么不接 Prometheus/Sentry：当前没有指标存储与采集端，接了也只是空转；
webhook 是"出事有人知道"的最低成本通道，且不锁定具体厂商。

两条硬约束：
1. **不反噬业务**：发送失败只记日志，绝不向上抛
2. **必须节流**：配额超限这类事件会连续触发，不节流会变成告警风暴
   （Redis SET NX EX 做去重；Redis 不可用时宁可多发也不漏发）
"""

import time

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.pubsub import get_redis

logger = get_logger("alerts")

# 告警推送超时：告警链路本身不能拖住调用方
WEBHOOK_TIMEOUT_SECONDS = 3.0
DEFAULT_DEDUPE_WINDOW_SECONDS = 600


async def _should_send(key: str, window_seconds: int) -> bool:
    """窗口内同 key 只发一次。Redis 异常时返回 True（不漏报优先）。"""
    try:
        redis = await get_redis()
        return bool(await redis.set(f"alert:dedupe:{key}", "1", ex=window_seconds, nx=True))
    except Exception:  # noqa: BLE001 - 去重失败不能导致告警丢失
        logger.warning("alert_dedupe_unavailable", key=key)
        return True


async def send_alert(
    event: str,
    *,
    dedupe_key: str = "",
    window_seconds: int = DEFAULT_DEDUPE_WINDOW_SECONDS,
    **fields: object,
) -> None:
    """发一条告警：始终记日志，配置了 webhook 时再推送。

    日志字段用 `alert` 而不是 `event`：structlog 的 `event` 是保留参数
    （日志事件名本身），同名会直接抛 TypeError。
    """
    logger.warning("alert_raised", alert=event, **fields)

    url = get_settings().alert_webhook_url.strip()
    if not url:
        return

    key = dedupe_key or event
    if not await _should_send(key, window_seconds):
        return

    payload = {
        "event": event,
        "fields": {name: _stringify(value) for name, value in fields.items()},
        "timestamp": time.time(),
    }
    try:
        async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_SECONDS) as client:
            await client.post(url, json=payload)
    except Exception as exc:  # noqa: BLE001 - 告警失败只留痕，不影响调用方
        logger.warning("alert_webhook_failed", alert=event, error=str(exc))


def _stringify(value: object) -> object:
    """UUID/Decimal 等类型统一转字符串，避免 webhook 侧解析失败。"""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


__all__ = ["send_alert"]
