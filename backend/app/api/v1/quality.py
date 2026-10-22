"""质检抽检与敏感词命中路由（M9）。

权限：坐席及以上。质检是"复核别人的服务过程"，
与数据报表、AI 优化同一层可见性，不必收窄到 admin。
"""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import require_agent
from app.db.models.tenant import TenantMember
from app.db.session import get_session
from app.schemas.quality import (
    QualityOverview,
    QualityReviewInfo,
    QualityReviewSubmit,
    SensitiveHitInfo,
)
from app.services import quality as quality_service

router = APIRouter(prefix="/quality", tags=["quality"])


@router.get("/overview", response_model=QualityOverview)
async def get_overview(
    session: AsyncSession = Depends(get_session),
    _agent: TenantMember = Depends(require_agent),
) -> QualityOverview:
    return await quality_service.get_overview(session)


@router.post("/reviews/sample", response_model=int, status_code=201)
async def sample_reviews(
    limit: int = Query(
        default=quality_service.DEFAULT_SAMPLE_LIMIT,
        ge=1,
        le=quality_service.MAX_SAMPLE_LIMIT,
    ),
    member: TenantMember = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
) -> int:
    """随机抽若干条未抽检会话进队列；返回实际新建条数（可能少于 limit）。"""
    return await quality_service.sample(session, limit=limit, sampled_by=member.user_id)


@router.get("/reviews", response_model=list[QualityReviewInfo])
async def list_reviews(
    status: str | None = Query(default=None, description="pending / done"),
    mine: bool = Query(default=False, description="只看我评过的"),
    limit: int = Query(default=quality_service.DEFAULT_LIMIT, ge=1, le=quality_service.MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    member: TenantMember = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
) -> list[QualityReviewInfo]:
    items, _total = await quality_service.list_reviews(
        session,
        status=status,
        reviewer_id=member.user_id if mine else None,
        limit=limit,
        offset=offset,
    )
    return items


@router.post("/reviews/{review_id}", response_model=QualityReviewInfo)
async def submit_review(
    review_id: uuid.UUID,
    payload: QualityReviewSubmit,
    member: TenantMember = Depends(require_agent),
    session: AsyncSession = Depends(get_session),
) -> QualityReviewInfo:
    """提交评分（重复提交即改分）。"""
    return await quality_service.submit_review(
        session,
        review_id=review_id,
        reviewer_id=member.user_id,
        score=payload.score,
        tags=payload.tags,
        comment=payload.comment,
    )


@router.get("/sensitive-hits", response_model=list[SensitiveHitInfo])
async def list_sensitive_hits(
    limit: int = Query(default=quality_service.DEFAULT_LIMIT, ge=1, le=quality_service.MAX_LIMIT),
    session: AsyncSession = Depends(get_session),
    _agent: TenantMember = Depends(require_agent),
) -> list[SensitiveHitInfo]:
    """敏感词命中记录：命中词、处置方式、片段与时间。"""
    return await quality_service.list_sensitive_hits(session, limit=limit)
