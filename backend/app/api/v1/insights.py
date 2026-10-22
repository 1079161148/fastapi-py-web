"""AI 效果洞察路由（未命中问题看板）。

读接口、坐席与管理员可见：知识库运营是坐席日常工作的一部分，
不必收窄到 admin（与数据报表口径一致）。
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import require_agent
from app.db.models.tenant import TenantMember
from app.db.session import get_session
from app.schemas.insights import InsightOverview
from app.services import insights as insights_service

router = APIRouter(prefix="/insights", tags=["insights"])


@router.get("/overview", response_model=InsightOverview)
async def get_overview(
    limit: int = Query(default=insights_service.DEFAULT_LIMIT, ge=1, le=insights_service.MAX_LIMIT),
    session: AsyncSession = Depends(get_session),
    _agent: TenantMember = Depends(require_agent),
) -> InsightOverview:
    """AI 命中率 + 未命中问题 top 列表（近 30 天）。"""
    return await insights_service.get_overview(session, limit=limit)
