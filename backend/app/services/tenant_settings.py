"""租户运营配置的读取与判断（M8）。

配置存在 tenants.settings JSONB，读取时用 TenantSettings 做一次校验与补缺：
历史数据可能是空 dict 或旧结构，直接 .get 取字段会在升级后 KeyError。
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.tenant import Tenant
from app.schemas.tenant import TenantSettings


async def load(session: AsyncSession, tenant_id: uuid.UUID) -> TenantSettings:
    """读取租户配置；不存在或缺字段时返回默认值（即"硬编码时代"的行为）。"""
    tenant = await session.get(Tenant, tenant_id)
    raw = tenant.settings if tenant is not None else {}
    return TenantSettings.model_validate(raw or {})


def is_within_work_hours(settings: TenantSettings, *, now: datetime | None = None) -> bool:
    """当前是否在工作时间内。

    时区统一按 UTC：租户分布在不同时区时应由前端把本地时间换算后
    存成 UTC 的起止时刻；当前版本先服务单时区部署，避免引入时区库。
    """
    moment = now or datetime.now(UTC)
    if moment.isoweekday() not in settings.work_days:
        return False
    start = _parse_hhmm(settings.work_hours_start)
    end = _parse_hhmm(settings.work_hours_end)
    current = moment.hour * 60 + moment.minute
    return start <= current < end


def match_trigger(content: str, triggers: list[str]) -> str | None:
    """命中任意触发词则返回该词（用于工单备注，便于统计误触发）。"""
    for trigger in (item.strip() for item in triggers):
        if trigger and trigger in content:
            return trigger
    return None


def _parse_hhmm(value: str) -> int:
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)
