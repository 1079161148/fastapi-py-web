"""模型用量记录与成本聚合（设计文档 6 / 10 / 11 M7）。

三个职责，集中在这里避免散落：

1. **记录**：一次调用一行。失败只记日志、绝不向上抛——用量统计是附属能力，
   不能因为写流水失败而让已经生成的回答丢掉（与 pubsub 发布失败同一原则）。
2. **聚合**：固定按 (天, 类型, 模型) 三元组出桶，调用方再按需折叠。
   为什么不让 SQL 支持任意 group by：混合口径求和会出错（ASR 的"秒"和
   TTS 的"字符"相加没有意义），固定三元组是唯一不产生歧义的口径。
3. **配额**：`tenants.quota.max_tokens_per_day` 的校验入口。必须查库而不是
   内存计数——多 uvicorn worker 下内存计数天然不准。

成本金额只存不算：价格来自配置，改价只影响新记录，历史账目不变。
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import Date, String, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.alerts import send_alert
from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.core.tenant import get_current_tenant_id, set_current_tenant
from app.db.models.tenant import Tenant
from app.db.models.usage import (
    KIND_ASR,
    KIND_DIALOGUE,
    KIND_EMBEDDING,
    KIND_RERANK,
    KIND_TTS,
    USAGE_KINDS,
    LLMUsageLog,
)
from app.db.session import async_session_factory

logger = get_logger("usage")

GROUP_BY_DAY = "day"
GROUP_BY_KIND = "kind"
GROUP_BY_MODEL = "model"
GROUP_BY_CHOICES = (GROUP_BY_DAY, GROUP_BY_KIND, GROUP_BY_MODEL)

# 金额精度：单次调用常常远小于 0.01，按 1e-6 存储
_COST_QUANTUM = Decimal("0.000001")
_THOUSAND = Decimal(1000)


@dataclass(frozen=True, slots=True)
class UsageBucket:
    """一个聚合桶。未参与分组的维度是空串，避免调用方把 None 当"未知"。"""

    day: str
    kind: str
    model: str
    requests: int
    prompt_tokens: int
    completion_tokens: int
    units: Decimal
    cost: Decimal

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def _money(amount: Decimal) -> Decimal:
    return amount.quantize(_COST_QUANTUM, rounding=ROUND_HALF_UP)


def estimate_cost(
    *,
    kind: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    units: Decimal | int | float = Decimal(0),
) -> Decimal:
    """按配置价格估算一次调用的成本；价格未配置（0）时返回 0。

    不做"猜一个业内均价"的兜底：看板上出现无来源的数字比显示 0 更危险。
    """
    settings = get_settings()
    amount = Decimal(0)

    if kind == KIND_DIALOGUE:
        amount = (
            Decimal(prompt_tokens) * Decimal(str(settings.usage_price_prompt_per_1k))
            + Decimal(completion_tokens) * Decimal(str(settings.usage_price_completion_per_1k))
        ) / _THOUSAND
    elif kind == KIND_EMBEDDING:
        # units 是输入字符数（见 config 注释：没有 tokenizer，用字符数近似）
        amount = (
            Decimal(str(units)) * Decimal(str(settings.usage_price_embedding_per_1k_chars))
        ) / _THOUSAND
    elif kind == KIND_RERANK:
        amount = (
            Decimal(str(units)) * Decimal(str(settings.usage_price_rerank_per_1k_chars))
        ) / _THOUSAND
    elif kind == KIND_ASR:
        # units 是音频秒数，价格按分钟
        amount = (
            Decimal(str(units)) / Decimal(60) * Decimal(str(settings.usage_price_asr_per_minute))
        )
    elif kind == KIND_TTS:
        amount = (
            Decimal(str(units)) * Decimal(str(settings.usage_price_tts_per_1k_chars))
        ) / _THOUSAND
    else:
        # 未知类型直接报错：静默记 0 会让成本对不上账且查不出原因
        raise ValueError(f"未知的用量类型：{kind}")

    return _money(amount)


async def record(
    *,
    tenant_id: uuid.UUID,
    kind: str,
    model: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    units: Decimal | int | float = Decimal(0),
    conversation_id: uuid.UUID | None = None,
) -> None:
    """写入一条用量流水。

    用独立会话：调用点（流式生成、worker、SSE 音频分支）都可能有未提交的
    业务事务，混在一起会互相拖累；且失败必须能单独吞掉。

    上下文恢复：本函数会临时切到 tenant_id，结束后还原，避免调用方在同一
    协程后续操作里读到被改写的租户。
    """
    if kind not in USAGE_KINDS:
        raise ValueError(f"未知的用量类型：{kind}")

    previous = get_current_tenant_id()
    try:
        cost = estimate_cost(
            kind=kind,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            units=units,
        )
        set_current_tenant(tenant_id)
        async with async_session_factory() as session:
            session.add(
                LLMUsageLog(
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    kind=kind,
                    model=model[:100],
                    prompt_tokens=max(prompt_tokens, 0),
                    completion_tokens=max(completion_tokens, 0),
                    units=Decimal(str(units)),
                    cost=cost,
                )
            )
            await session.commit()
    except Exception:  # noqa: BLE001 - 用量记录失败不能影响业务主流程
        logger.exception("usage_record_failed", kind=kind, tenant_id=str(tenant_id), model=model)
    finally:
        set_current_tenant(previous)


async def aggregate(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    start: datetime,
    end: datetime,
    kind: str | None = None,
) -> list[UsageBucket]:
    """按 (天, 类型, 模型) 出桶，时间区间为 [start, end)。"""
    day_col = func.date_trunc("day", LLMUsageLog.created_at).cast(Date).cast(String)
    stmt = (
        select(
            day_col.label("day"),
            LLMUsageLog.kind.label("kind"),
            LLMUsageLog.model.label("model"),
            func.count().label("requests"),
            func.coalesce(func.sum(LLMUsageLog.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(LLMUsageLog.completion_tokens), 0).label("completion_tokens"),
            func.coalesce(func.sum(LLMUsageLog.units), 0).label("units"),
            func.coalesce(func.sum(LLMUsageLog.cost), 0).label("cost"),
        )
        .where(
            LLMUsageLog.tenant_id == tenant_id,
            LLMUsageLog.created_at >= start,
            LLMUsageLog.created_at < end,
        )
        .group_by(day_col, LLMUsageLog.kind, LLMUsageLog.model)
        .order_by(day_col, LLMUsageLog.kind, LLMUsageLog.model)
    )
    if kind is not None:
        stmt = stmt.where(LLMUsageLog.kind == kind)

    rows = (await session.execute(stmt)).all()
    return [
        UsageBucket(
            day=str(row.day),
            kind=str(row.kind),
            model=str(row.model),
            requests=int(row.requests),
            prompt_tokens=int(row.prompt_tokens),
            completion_tokens=int(row.completion_tokens),
            units=Decimal(str(row.units)),
            cost=Decimal(str(row.cost)),
        )
        for row in rows
    ]


def fold(buckets: Sequence[UsageBucket], *, group_by: str) -> list[UsageBucket]:
    """把明细桶折叠成单一维度（天 / 类型 / 模型），按该维度顺序返回。"""
    if group_by not in GROUP_BY_CHOICES:
        raise ValueError(f"group_by 只支持 {GROUP_BY_CHOICES}，收到 {group_by}")

    merged: dict[str, UsageBucket] = {}
    for bucket in buckets:
        key = {
            GROUP_BY_DAY: bucket.day,
            GROUP_BY_KIND: bucket.kind,
            GROUP_BY_MODEL: bucket.model,
        }[group_by]
        existing = merged.get(key)
        if existing is None:
            merged[key] = UsageBucket(
                day=bucket.day if group_by == GROUP_BY_DAY else "",
                kind=bucket.kind if group_by == GROUP_BY_KIND else "",
                model=bucket.model if group_by == GROUP_BY_MODEL else "",
                requests=bucket.requests,
                prompt_tokens=bucket.prompt_tokens,
                completion_tokens=bucket.completion_tokens,
                units=bucket.units,
                cost=bucket.cost,
            )
            continue
        merged[key] = UsageBucket(
            day=existing.day,
            kind=existing.kind,
            model=existing.model,
            requests=existing.requests + bucket.requests,
            prompt_tokens=existing.prompt_tokens + bucket.prompt_tokens,
            completion_tokens=existing.completion_tokens + bucket.completion_tokens,
            units=existing.units + bucket.units,
            cost=existing.cost + bucket.cost,
        )
    return [merged[key] for key in sorted(merged)]


def total(buckets: Sequence[UsageBucket]) -> UsageBucket:
    """合计一行：看板顶部的总量指标。"""
    return UsageBucket(
        day="",
        kind="",
        model="",
        requests=sum(item.requests for item in buckets),
        prompt_tokens=sum(item.prompt_tokens for item in buckets),
        completion_tokens=sum(item.completion_tokens for item in buckets),
        units=sum((item.units for item in buckets), Decimal(0)),
        cost=sum((item.cost for item in buckets), Decimal(0)),
    )


def day_start(moment: datetime | None = None) -> datetime:
    """当日 0 点（UTC）。

    配额按 UTC 日界结算：跨时区租户的"今天"必然不同，取 UTC 是唯一无需
    额外配置的确定性口径；需要本地日界时应改为按时租户时区换算。
    """
    current = moment or datetime.now(UTC)
    return current.replace(hour=0, minute=0, second=0, microsecond=0)


async def daily_tokens_used(
    session: AsyncSession, *, tenant_id: uuid.UUID, now: datetime | None = None
) -> int:
    """当日已用 token（prompt + completion）。"""
    start = day_start(now)
    tokens = func.coalesce(func.sum(LLMUsageLog.prompt_tokens + LLMUsageLog.completion_tokens), 0)
    used = await session.scalar(
        select(tokens).where(
            LLMUsageLog.tenant_id == tenant_id,
            LLMUsageLog.created_at >= start,
        )
    )
    return int(used or 0)


async def enforce_daily_token_quota(session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
    """超过租户当日 token 预算则拒绝本次调用（429）。

    配额为 0 或未配置表示不限；已用量的查询有 (tenant_id, created_at) 索引，
    单次是一次索引范围扫描，放在对话入口的开销可接受。
    """
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        return
    limit = int(tenant.quota.get("max_tokens_per_day") or 0)
    if limit <= 0:
        return

    used = await daily_tokens_used(session, tenant_id=tenant_id)
    if used < limit:
        return

    logger.warning("token_quota_exceeded", tenant_id=str(tenant_id), used=used, limit=limit)
    # 成本失控是经营风险，必须能推给运维；内部按 key 做 10 分钟去重
    await send_alert(
        "token_quota_exceeded",
        tenant_id=str(tenant_id),
        used=used,
        limit=limit,
        dedupe_key=f"token_quota:{tenant_id}",
    )
    raise AppError(
        code="TOKEN_QUOTA_EXCEEDED",
        message=(
            f"本租户今日 AI 用量已达上限（{used}/{limit} tokens），请稍后再试或联系管理员调整配额"
        ),
        status_code=429,
        detail={"used": used, "limit": limit},
        expose=True,
    )


__all__ = [
    "GROUP_BY_CHOICES",
    "GROUP_BY_DAY",
    "GROUP_BY_KIND",
    "GROUP_BY_MODEL",
    "UsageBucket",
    "aggregate",
    "daily_tokens_used",
    "day_start",
    "enforce_daily_token_quota",
    "estimate_cost",
    "fold",
    "record",
    "total",
]
