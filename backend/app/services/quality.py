"""质检抽检（M9）：抽样 → 评分 → 汇总。

设计取舍：
- 抽检对象是"整段会话"而不是单条消息：质检看的是一次服务过程
  （有没有答上来、语气如何），拆到消息粒度既没有意义也评不完。
- 同一会话只抽检一次（conversation_id 唯一索引）：重复抽检只会在报表里
  把同一条问题算两遍，让平均分失真。
- 随机抽样而不是"最近 N 条"：固定取最近会让最早的会话永远抽不到，
  而问题往往集中在冷启动阶段。
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.core.tenant import require_current_tenant_id
from app.db.models.conversation import Conversation
from app.db.models.support import (
    QUALITY_TAGS,
    REVIEW_STATUS_DONE,
    REVIEW_STATUS_PENDING,
    QualityReview,
    SensitiveHit,
)
from app.schemas.quality import QualityOverview, QualityReviewInfo, SensitiveHitInfo

logger = get_logger("quality")

WINDOW_DAYS = 30
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
DEFAULT_SAMPLE_LIMIT = 10
MAX_SAMPLE_LIMIT = 100


def _window_start(days: int = WINDOW_DAYS) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


async def sample(
    session: AsyncSession,
    *,
    limit: int = DEFAULT_SAMPLE_LIMIT,
    sampled_by: uuid.UUID | None = None,
    window_days: int = WINDOW_DAYS,
) -> int:
    """从未抽检的会话里随机抽若干条建 pending 记录；返回新建条数。

    并发安全靠 unique(conversation_id)：两个质检员同时点"抽样"，
    后提交的一方撞唯一约束而不是建出两条重复单。
    """
    tenant_id = require_current_tenant_id()
    limit = min(limit, MAX_SAMPLE_LIMIT)
    reviewed = select(QualityReview.conversation_id).where(QualityReview.tenant_id == tenant_id)
    rows = await session.execute(
        select(Conversation.id)
        .where(
            Conversation.tenant_id == tenant_id,
            Conversation.created_at >= _window_start(window_days),
            ~Conversation.id.in_(reviewed),
        )
        .order_by(func.random())
        .limit(limit)
    )
    picked = list(rows.scalars().all())
    now = datetime.now(UTC)
    for conversation_id in picked:
        session.add(
            QualityReview(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                sampled_by=sampled_by,
                status=REVIEW_STATUS_PENDING,
                sampled_at=now,
            )
        )
    if picked:
        try:
            await session.commit()
        except Exception:  # noqa: BLE001 - 撞唯一约束时已建的也要回滚，交由重试
            await session.rollback()
            logger.exception("quality_sample_conflict", tenant_id=str(tenant_id))
            return 0
    return len(picked)


async def list_reviews(
    session: AsyncSession,
    *,
    status: str | None = None,
    reviewer_id: uuid.UUID | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> tuple[list[QualityReviewInfo], int]:
    tenant_id = require_current_tenant_id()
    conditions = [QualityReview.tenant_id == tenant_id]
    if status:
        conditions.append(QualityReview.status == status)
    if reviewer_id is not None:
        conditions.append(QualityReview.reviewer_id == reviewer_id)

    total = await session.scalar(select(func.count()).select_from(QualityReview).where(*conditions))
    rows = await session.execute(
        select(QualityReview, Conversation)
        .join(Conversation, Conversation.id == QualityReview.conversation_id)
        .where(*conditions)
        .order_by(QualityReview.created_at.desc())
        .limit(min(limit, MAX_LIMIT))
        .offset(offset)
    )
    items = [_to_info(review, conversation) for review, conversation in rows.all()]
    return items, total or 0


async def submit_review(
    session: AsyncSession,
    *,
    review_id: uuid.UUID,
    reviewer_id: uuid.UUID,
    score: int,
    tags: list[str],
    comment: str,
) -> QualityReviewInfo:
    """提交评分（可重复提交：改分比"改不了"更贴近质检的实际用法）。"""
    tenant_id = require_current_tenant_id()
    unknown = [tag for tag in tags if tag not in QUALITY_TAGS]
    if unknown:
        raise AppError(
            code="QUALITY_TAG_INVALID",
            message=f"未知的问题标签：{', '.join(unknown)}",
            status_code=422,
            expose=True,
        )
    review = await session.get(QualityReview, review_id)
    if review is None or review.tenant_id != tenant_id:
        raise AppError(code="NOT_FOUND", message="质检记录不存在", status_code=404)

    review.reviewer_id = reviewer_id
    review.score = score
    review.tags = tags
    review.comment = comment
    review.status = REVIEW_STATUS_DONE
    review.reviewed_at = datetime.now(UTC)
    await session.commit()
    conversation = await session.get(Conversation, review.conversation_id)
    return _to_info(review, conversation)


async def get_overview(session: AsyncSession) -> QualityOverview:
    tenant_id = require_current_tenant_id()
    reviewed = await session.scalar(
        select(func.count())
        .select_from(QualityReview)
        .where(
            QualityReview.tenant_id == tenant_id,
            QualityReview.status == REVIEW_STATUS_DONE,
        )
    )
    pending = await session.scalar(
        select(func.count())
        .select_from(QualityReview)
        .where(
            QualityReview.tenant_id == tenant_id,
            QualityReview.status == REVIEW_STATUS_PENDING,
        )
    )
    avg_score = await session.scalar(
        select(func.avg(QualityReview.score)).where(
            QualityReview.tenant_id == tenant_id,
            QualityReview.status == REVIEW_STATUS_DONE,
        )
    )
    conversations_total = await session.scalar(
        select(func.count())
        .select_from(Conversation)
        .where(
            Conversation.tenant_id == tenant_id,
            Conversation.created_at >= _window_start(),
        )
    )
    sensitive_hits = await session.scalar(
        select(func.count())
        .select_from(SensitiveHit)
        .where(
            SensitiveHit.tenant_id == tenant_id,
            SensitiveHit.created_at >= _window_start(),
        )
    )
    tag_rows = await session.execute(
        select(QualityReview.tags).where(
            QualityReview.tenant_id == tenant_id,
            QualityReview.status == REVIEW_STATUS_DONE,
        )
    )
    tag_counts: dict[str, int] = {}
    for (tags,) in tag_rows:
        for tag in tags or []:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

    reviewed_count = reviewed or 0
    total = conversations_total or 0
    return QualityOverview(
        window_days=WINDOW_DAYS,
        reviewed_count=reviewed_count,
        pending_count=pending or 0,
        avg_score=round(float(avg_score), 2) if avg_score is not None else None,
        conversations_total=total,
        coverage=round(reviewed_count / total, 4) if total else 0.0,
        sensitive_hits=sensitive_hits or 0,
        tag_counts=tag_counts,
    )


async def list_sensitive_hits(
    session: AsyncSession, *, limit: int = DEFAULT_LIMIT
) -> list[SensitiveHitInfo]:
    tenant_id = require_current_tenant_id()
    rows = await session.execute(
        select(SensitiveHit, Conversation)
        .outerjoin(Conversation, Conversation.id == SensitiveHit.conversation_id)
        .where(SensitiveHit.tenant_id == tenant_id)
        .order_by(SensitiveHit.created_at.desc())
        .limit(min(limit, MAX_LIMIT))
    )
    return [
        SensitiveHitInfo(
            id=hit.id,
            conversation_id=hit.conversation_id,
            conversation_title=conversation.title if conversation else "",
            message_id=hit.message_id,
            words=list(hit.words or []),
            action=hit.action,
            snippet=hit.snippet,
            created_at=hit.created_at,
        )
        for hit, conversation in rows.all()
    ]


def _to_info(review: QualityReview, conversation: Conversation | None) -> QualityReviewInfo:
    return QualityReviewInfo(
        id=review.id,
        conversation_id=review.conversation_id,
        conversation_title=conversation.title if conversation else "",
        conversation_status=conversation.status if conversation else "",
        channel=conversation.channel if conversation else "",
        conversation_updated_at=conversation.updated_at if conversation else None,
        status=review.status,
        score=review.score,
        tags=list(review.tags or []),
        comment=review.comment,
        reviewer_id=review.reviewer_id,
        sampled_at=review.sampled_at,
        reviewed_at=review.reviewed_at,
        created_at=review.created_at,
    )


__all__ = [
    "DEFAULT_LIMIT",
    "DEFAULT_SAMPLE_LIMIT",
    "MAX_LIMIT",
    "MAX_SAMPLE_LIMIT",
    "WINDOW_DAYS",
    "get_overview",
    "list_reviews",
    "list_sensitive_hits",
    "sample",
    "submit_review",
]
