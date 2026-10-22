"""用量与成本看板 API（M7，设计文档 10 / 11）。

门禁：租户管理员（owner/admin）。成本数据属于租户经营信息，
坐席即便能看会话，也不该看到整租户的模型开销。

聚合口径固定为 (天, 类型, 模型) 明细桶，`group_by` 只做折叠——
这样"按天看趋势"和"按模型看成本构成"用的是同一份数据，不会出现
两个页面对不上账的情况。
"""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import require_tenant_admin
from app.core.exceptions import AppError
from app.core.tenant import require_current_tenant_id
from app.db.models.tenant import Tenant, TenantMember
from app.db.models.usage import USAGE_KINDS
from app.db.session import get_session
from app.schemas.usage import (
    UsageBucketInfo,
    UsageOverview,
    UsageQuotaInfo,
    UsageSummary,
)
from app.services import usage as usage_service

router = APIRouter(prefix="/usage", tags=["usage"])

DEFAULT_RANGE_DAYS = 30
MAX_RANGE_DAYS = 366


def _to_info(bucket: usage_service.UsageBucket) -> UsageBucketInfo:
    return UsageBucketInfo(
        day=bucket.day,
        kind=bucket.kind,
        model=bucket.model,
        requests=bucket.requests,
        prompt_tokens=bucket.prompt_tokens,
        completion_tokens=bucket.completion_tokens,
        total_tokens=bucket.total_tokens,
        units=float(bucket.units),
        cost=float(bucket.cost),
    )


def _ensure_aware(value: datetime) -> datetime:
    """无时区输入按 UTC 解释。

    静默按服务器本地时区解释会让"最近 30 天"随部署机器漂移，
    结果不可复现——统一 UTC，前端负责换算展示时区。
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@router.get("/summary", response_model=UsageSummary)
async def get_summary(
    start: datetime | None = Query(default=None, description="起始时间（含），默认 30 天前"),
    end: datetime | None = Query(default=None, description="结束时间（不含），默认当前"),
    group_by: str = Query(default=usage_service.GROUP_BY_DAY),
    kind: str | None = Query(default=None, description="只看某类调用，如 dialogue"),
    session: AsyncSession = Depends(get_session),
    _admin: TenantMember = Depends(require_tenant_admin),
) -> UsageSummary:
    tenant_id = require_current_tenant_id()
    range_end = _ensure_aware(end) if end is not None else datetime.now(UTC)
    range_start = (
        _ensure_aware(start)
        if start is not None
        else range_end - timedelta(days=DEFAULT_RANGE_DAYS)
    )

    if range_start >= range_end:
        raise AppError(
            code="INVALID_TIME_RANGE",
            message="起始时间必须早于结束时间",
            status_code=400,
        )
    if range_end - range_start > timedelta(days=MAX_RANGE_DAYS):
        raise AppError(
            code="TIME_RANGE_TOO_LARGE",
            message=f"单次查询区间不能超过 {MAX_RANGE_DAYS} 天，请分段查询",
            status_code=400,
        )
    if group_by not in usage_service.GROUP_BY_CHOICES:
        raise AppError(
            code="INVALID_GROUP_BY",
            message=f"group_by 只支持 {'/'.join(usage_service.GROUP_BY_CHOICES)}",
            status_code=400,
        )
    if kind is not None and kind not in USAGE_KINDS:
        raise AppError(
            code="INVALID_USAGE_KIND",
            message=f"kind 只支持 {'/'.join(USAGE_KINDS)}",
            status_code=400,
        )

    buckets = await usage_service.aggregate(
        session, tenant_id=tenant_id, start=range_start, end=range_end, kind=kind
    )
    return UsageSummary(
        start=range_start,
        end=range_end,
        group_by=group_by,
        total=_to_info(usage_service.total(buckets)),
        buckets=[_to_info(item) for item in usage_service.fold(buckets, group_by=group_by)],
    )


@router.get("/overview", response_model=UsageOverview)
async def get_overview(
    session: AsyncSession = Depends(get_session),
    _admin: TenantMember = Depends(require_tenant_admin),
) -> UsageOverview:
    """看板首屏：今日 / 近 7 天 / 近 30 天 + 当日配额。"""
    tenant_id = require_current_tenant_id()
    now = datetime.now(UTC)
    today_start = usage_service.day_start(now)
    # end 取"今日结束"而不是 now：避免当前秒之后的时间边界歧义
    today_end = today_start + timedelta(days=1)

    today = await usage_service.aggregate(
        session, tenant_id=tenant_id, start=today_start, end=today_end
    )
    last_7 = await usage_service.aggregate(
        session, tenant_id=tenant_id, start=today_start - timedelta(days=6), end=today_end
    )
    last_30 = await usage_service.aggregate(
        session, tenant_id=tenant_id, start=today_start - timedelta(days=29), end=today_end
    )

    limit = 0
    tenant = await session.get(Tenant, tenant_id)
    if tenant is not None:
        limit = int(tenant.quota.get("max_tokens_per_day") or 0)
    used_today = usage_service.total(today).total_tokens

    return UsageOverview(
        tenant_id=tenant_id,
        today=_to_info(usage_service.total(today)),
        last_7_days=_to_info(usage_service.total(last_7)),
        last_30_days=_to_info(usage_service.total(last_30)),
        quota=UsageQuotaInfo(
            tokens_limit=limit,
            tokens_used_today=used_today,
            exceeded=limit > 0 and used_today >= limit,
        ),
        daily=[
            _to_info(item)
            for item in usage_service.fold(last_7, group_by=usage_service.GROUP_BY_DAY)
        ],
    )


__all__ = ["router"]
