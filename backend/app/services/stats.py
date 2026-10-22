"""数据报表聚合（M8）。

口径说明（写在这里而不是只写在文档里，避免两处不一致）：
- 统计窗口：近 30 天（与 usage 概览一致）
- ai_resolved：会话状态仍是 ai，即"AI 答完没转人工"
- human_handled：状态为 human 或 closed。closed 可能由 AI 直答后手动关闭，
  本版本无法区分，统一计入"已处理"
- handoff_rate：(human_handled + pending_handoff) / total
- avg_first_reply_ms：每会话首条 assistant 消息 - 首条 user 消息的平均耗时，
  比 messages.latency_ms（纯 LLM 生成耗时）更贴近用户感知
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant import require_current_tenant_id
from app.schemas.support import StatsOverview, TrendPoint

WINDOW_DAYS = 30

_OVERVIEW_SQL = text(
    """
    with scoped as (
        select id, status
        from conversations
        where tenant_id = :tenant_id
          and created_at >= now() - make_interval(days => :days)
    ),
    first_reply as (
        select c.id,
               extract(epoch from (min(a.created_at) - min(u.created_at))) * 1000 as ms
        from scoped c
        join messages u on u.conversation_id = c.id and u.role = 'user'
        join messages a on a.conversation_id = c.id and a.role = 'assistant'
        group by c.id
    )
    select
        (select count(*) from scoped) as total,
        (select count(*) from scoped where status = 'ai') as ai_resolved,
        (select count(*) from scoped where status in ('human', 'closed')) as human_handled,
        (select count(*) from scoped where status = 'pending_handoff') as pending_handoff,
        (select avg(ms) from first_reply) as avg_first_reply_ms,
        (select avg(rating) from messages
          where tenant_id = :tenant_id and rating is not null) as satisfaction_avg,
        (select count(*) from messages
          where tenant_id = :tenant_id and rating is not null) as satisfaction_count,
        (select count(*) from leave_messages
          where tenant_id = :tenant_id and status = 'new') as new_leave_messages
    """
)

_TREND_SQL = text(
    """
    select d::date as date,
        (select count(*) from conversations c
          where c.tenant_id = :tenant_id and c.created_at::date = d) as conversations,
        (select count(*) from messages m
          where m.tenant_id = :tenant_id and m.created_at::date = d) as messages,
        (select count(*) from messages m
          where m.tenant_id = :tenant_id and m.created_at::date = d
            and m.rating is not null) as ratings
    from generate_series(
        (now() at time zone 'utc')::date - make_interval(days => :days - 1),
        (now() at time zone 'utc')::date,
        interval '1 day'
    ) as d
    """
)


async def get_overview(session: AsyncSession) -> StatsOverview:
    tenant_id = require_current_tenant_id()
    row = (
        await session.execute(
            _OVERVIEW_SQL, {"tenant_id": tenant_id, "days": WINDOW_DAYS}
        )
    ).mappings().one()

    trend_rows = await session.execute(
        _TREND_SQL, {"tenant_id": tenant_id, "days": WINDOW_DAYS}
    )
    trend = [
        TrendPoint(
            date=str(item["date"]),
            conversations=item["conversations"] or 0,
            messages=item["messages"] or 0,
            ratings=item["ratings"] or 0,
        )
        for item in trend_rows.mappings()
    ]

    total = row["total"] or 0
    handled = row["human_handled"] or 0
    pending = row["pending_handoff"] or 0
    return StatsOverview(
        total_conversations=total,
        ai_resolved=row["ai_resolved"] or 0,
        human_handled=handled,
        pending_handoff=pending,
        handoff_rate=round((handled + pending) / total, 4) if total else 0.0,
        avg_first_reply_ms=int(row["avg_first_reply_ms"]) if row["avg_first_reply_ms"] else None,
        satisfaction_avg=round(float(row["satisfaction_avg"]), 2)
        if row["satisfaction_avg"]
        else None,
        satisfaction_count=row["satisfaction_count"] or 0,
        new_leave_messages=row["new_leave_messages"] or 0,
        trend=trend,
    )


def window_start() -> datetime:
    """统计窗口起点（供测试或调试复用）。"""
    return datetime.now(UTC) - timedelta(days=WINDOW_DAYS)
