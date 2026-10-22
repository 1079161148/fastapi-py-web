"""接口限流（M8，设计文档 8「限流」）。

为什么用 Redis 计数而不是进程内计数器：uvicorn 多 worker + 多 API 实例下，
进程内计数等于把限流阈值按实例数放大，防不住刷量。

算法：固定窗口 + 原子 INCR/EXPIRE（Lua 脚本一次往返）。
选固定窗口而不是滑动窗口/令牌桶：客服系统的流量是突发型，窗口边界的
双倍通过（最坏 2x）对保护 LLM 额度完全够用，而滑动窗口每个请求要多几次
Redis 操作。真要精确再换，不提前付这个复杂度。

失败策略：**fail-open**。Redis 不可用时放行并记 warning——限流是保护措施，
不是安全边界；Redis 挂掉时实时通道已经降级，再把全站请求拒掉只会放大故障。
（登录接口本身有 bcrypt 成本，暴力破解的代价依然存在。）
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.core.pubsub import get_redis
from app.core.tenant import parse_tenant_id

logger = get_logger("rate_limit")

CATEGORY_AUTH = "auth"
CATEGORY_CHAT = "chat"
CATEGORY_WEBHOOK = "webhook"
CATEGORY_DEFAULT = "default"

# 不计数的路径：健康检查会被探针高频调用，限流只会让探针误报
SKIP_PREFIXES = ("/api/v1/health", "/health", "/docs", "/redoc", "/openapi.json")

# INCR 与 EXPIRE 必须原子：分两条命令会在进程崩溃时留下永不过期的计数
_COUNTER_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return current
"""


@dataclass(frozen=True, slots=True)
class RateLimitRule:
    category: str
    limit: int
    window_seconds: int = 60


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    retry_after: int = 0
    remaining: int = 0


def _rule(category: str, limit: int) -> RateLimitRule:
    return RateLimitRule(
        category=category,
        limit=limit,
        window_seconds=max(get_settings().rate_limit_window_seconds, 1),
    )


async def check(rule: RateLimitRule, *, subject: str) -> RateLimitResult:
    """记一次请求并判断是否放行。subject 是限流主体（用户/访客/IP/租户）。"""
    settings = get_settings()
    if not settings.rate_limit_enabled or rule.limit <= 0:
        return RateLimitResult(allowed=True)

    key = f"rl:{rule.category}:{subject}"
    try:
        redis = await get_redis()
        # redis-py 的 eval 标注是 Awaitable[str] | str（同步/异步共用签名），
        # strict mypy 下需要显式 cast 才能 await
        pending = cast(
            Awaitable[str],
            redis.eval(_COUNTER_SCRIPT, 1, key, str(rule.window_seconds)),
        )
        current = int(await pending)
    except Exception as exc:  # noqa: BLE001 - fail-open 见模块文档
        logger.warning("rate_limit_unavailable", category=rule.category, error=str(exc))
        return RateLimitResult(allowed=True)

    if current > rule.limit:
        ttl = int(await redis.ttl(key))
        retry_after = ttl if ttl > 0 else rule.window_seconds
        logger.warning(
            "rate_limit_exceeded",
            category=rule.category,
            subject=subject,
            limit=rule.limit,
            window_seconds=rule.window_seconds,
        )
        return RateLimitResult(allowed=False, retry_after=retry_after)

    return RateLimitResult(allowed=True, remaining=max(rule.limit - current, 0))


async def enforce(category: str, *, subject: str, limit: int) -> None:
    """依赖层调用入口：超限直接抛 429（统一错误体）。"""
    result = await check(_rule(category, limit), subject=subject)
    if result.allowed:
        return
    raise AppError(
        code="RATE_LIMITED",
        message=f"请求过于频繁，请 {result.retry_after} 秒后再试",
        status_code=429,
        detail={"retry_after": result.retry_after},
        expose=True,
    )


def rule_for_path(path: str) -> RateLimitRule | None:
    """按路径前缀归类。返回 None 表示不限流。"""
    settings = get_settings()
    if any(path.startswith(prefix) for prefix in SKIP_PREFIXES):
        return None
    if path.startswith("/api/v1/auth/"):
        return _rule(CATEGORY_AUTH, settings.rate_limit_auth)
    if path.startswith("/api/v1/channels/") and path.endswith("/webhook"):
        return _rule(CATEGORY_WEBHOOK, settings.rate_limit_webhook)
    return _rule(CATEGORY_DEFAULT, settings.rate_limit_default)


def client_ip(request: Request) -> str:
    """客户端 IP。

    trust_proxy_headers 默认关闭：X-Forwarded-For 可被客户端伪造，
    开着它等于让限流可被绕过。生产在 Nginx 后面时应打开，并确保
    只有反代能连到应用端口（否则请保持关闭）。
    """
    if get_settings().trust_proxy_headers:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    client = request.client
    return client.host if client is not None else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """粗粒度兜底：按 IP 对 auth / webhook / 其他接口分别计数。

    精确到"用户/访客"的限制由端点依赖做（见 api/v1/deps.enforce_chat_rate）——
    中间件阶段还没解析 JWT，拿不到可靠身份。
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        rule = rule_for_path(request.url.path)
        if rule is None:
            return await call_next(request)

        # 伪造的租户头在这里只影响限流归属，不进入业务上下文；
        # parse_tenant_id 对非法值抛错，限流阶段直接退回 IP 计数
        try:
            tenant_id = parse_tenant_id(request.headers.get("X-Tenant-Id"))
        except Exception:  # noqa: BLE001 - 非法租户头交给后续中间件返回 400
            tenant_id = None

        if rule.category == CATEGORY_WEBHOOK and tenant_id is not None:
            subject = f"tenant:{tenant_id}"
        else:
            subject = f"ip:{client_ip(request)}"

        result = await check(rule, subject=subject)
        if not result.allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "code": "RATE_LIMITED",
                    "message": f"请求过于频繁，请 {result.retry_after} 秒后再试",
                    "detail": {"retry_after": result.retry_after},
                },
                headers={"Retry-After": str(result.retry_after)},
            )

        response: Response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(rule.limit)
        response.headers["X-RateLimit-Remaining"] = str(result.remaining)
        return response


__all__ = [
    "CATEGORY_AUTH",
    "CATEGORY_CHAT",
    "CATEGORY_DEFAULT",
    "CATEGORY_WEBHOOK",
    "RateLimitMiddleware",
    "RateLimitRule",
    "RateLimitResult",
    "check",
    "client_ip",
    "enforce",
    "rule_for_path",
]
