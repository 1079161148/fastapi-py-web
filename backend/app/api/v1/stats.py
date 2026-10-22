"""数据报表路由（M8）。

工作台 KPI 与报表页共用同一接口：两处数字必须同源，
分成两个接口必然出现"首页说 41、报表说 39"的分歧。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant_member
from app.db.models.tenant import TenantMember
from app.db.session import get_session
from app.schemas.support import StatsOverview
from app.services import stats as stats_service

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("/overview", response_model=StatsOverview)
async def get_overview(
    session: AsyncSession = Depends(get_session),
    _member: TenantMember = Depends(get_current_tenant_member),
) -> StatsOverview:
    return await stats_service.get_overview(session)
