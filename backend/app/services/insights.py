"""AI 效果洞察聚合（未命中问题看板）。

口径（与 stats.py 一样写在头部，避免两处不一致）：
- 统计窗口：近 30 天
- 一次"AI 回答"= role=assistant 且 source != 'agent' 的消息
  （坐席从后台回复也是 assistant 消息，但它不代表 AI 能力）
- "引用命中"= 回答携带至少一条知识引用（sources 非空数组）
- "未命中问题"= 用户提问后，紧跟的 AI 回答没有引用任何知识。
  转人工时的固定文案回复（"已转接人工…"）不算 AI 回答，
  否则每一次成功转人工都会被误报成知识缺口
- 聚合按提问原文（去首尾空白）：同文重复提问合并，按次数找 top 缺口

为什么用 LAG/LEAD 而不是在应用层逐会话扫描：消息表按
(tenant_id, conversation_id, created_at) 有索引，窗口函数一次扫描即可，
应用层扫描会在会话数增长后变成 N 次往返。
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant import require_current_tenant_id
from app.schemas.insights import InsightOverview, UnansweredQuestion

WINDOW_DAYS = 30
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
QUESTION_MAX_LENGTH = 200

# 与 chat.py 的固定文案保持一致：这些"回答"不是 AI 生成，也不该进命中率口径
FIXED_REPLY_1 = "已转接人工坐席，坐席会在这里回复你，请稍候。"
FIXED_REPLY_2 = "已为你转接人工坐席，请稍候；你也可以继续补充信息。"

_METRICS_SQL = text(
    """
    select
        count(*) as total_answers,
        count(*) filter (where jsonb_array_length(sources) > 0) as cited_answers
    from messages
    where tenant_id = :tenant_id
      and created_at >= now() - make_interval(days => :days)
      and role = 'assistant'
      and source <> 'agent'
      and content <> :fixed_reply_1
      and content <> :fixed_reply_2
    """
)

# 未命中明细：user 消息的"下一条"若是无引用的 AI 回答，则这次提问未命中
_UNANSWERED_SQL = text(
    """
    with ordered as (
        select m.role as role,
               btrim(m.content) as question,
               m.conversation_id,
               m.created_at,
               lead(m.role) over w as next_role,
               lead(m.source) over w as next_source,
               lead(m.sources) over w as next_sources,
               lead(m.content) over w as next_content
        from messages m
        where m.tenant_id = :tenant_id
          and m.created_at >= now() - make_interval(days => :days)
          and m.role in ('user', 'assistant')
        window w as (partition by m.conversation_id order by m.created_at, m.id)
    )
    select question,
           count(*) as ask_count,
           max(created_at) as last_asked_at,
           (array_agg(conversation_id order by created_at desc))[1] as conversation_id
    from ordered
    where role = 'user'
      and next_role = 'assistant'
      and next_source <> 'agent'
      and jsonb_array_length(coalesce(next_sources, '[]'::jsonb)) = 0
      and next_content <> :fixed_reply_1
      and next_content <> :fixed_reply_2
    group by question
    order by ask_count desc, last_asked_at desc
    limit :limit
    """
)

# 未命中总次数（不分组）：看板 KPI 用"次数"而不是"问题数"，
# 与转人工率、满意度的口径一致（都是按发生次数）
_UNANSWERED_TOTAL_SQL = text(
    """
    select count(*) as total
    from (
        select m.role as role,
               lead(m.role) over w as next_role,
               lead(m.source) over w as next_source,
               lead(m.sources) over w as next_sources,
               lead(m.content) over w as next_content
        from messages m
        where m.tenant_id = :tenant_id
          and m.created_at >= now() - make_interval(days => :days)
          and m.role in ('user', 'assistant')
        window w as (partition by m.conversation_id order by m.created_at, m.id)
    ) ordered
    where role = 'user'
      and next_role = 'assistant'
      and next_source <> 'agent'
      and jsonb_array_length(coalesce(next_sources, '[]'::jsonb)) = 0
      and next_content <> :fixed_reply_1
      and next_content <> :fixed_reply_2
    """
)

_DOC_COUNT_SQL = text(
    "select count(*) as total from documents "
    "where tenant_id = :tenant_id and status = 'done'"
)


async def get_overview(
    session: AsyncSession, *, limit: int = DEFAULT_LIMIT
) -> InsightOverview:
    tenant_id = require_current_tenant_id()
    limit = min(limit, MAX_LIMIT)
    params = {
        "tenant_id": tenant_id,
        "days": WINDOW_DAYS,
        "fixed_reply_1": FIXED_REPLY_1,
        "fixed_reply_2": FIXED_REPLY_2,
    }

    metrics = (
        await session.execute(_METRICS_SQL, params)
    ).mappings().one()
    unanswered_rows = (
        await session.execute(_UNANSWERED_SQL, {**params, "limit": limit})
    ).mappings().all()
    unanswered_total = (
        await session.execute(_UNANSWERED_TOTAL_SQL, params)
    ).scalar_one()
    knowledge_documents = (await session.execute(_DOC_COUNT_SQL, params)).scalar_one()

    total = metrics["total_answers"] or 0
    cited = metrics["cited_answers"] or 0
    return InsightOverview(
        window_days=WINDOW_DAYS,
        total_answers=total,
        cited_answers=cited,
        hit_rate=round(cited / total, 4) if total else None,
        unanswered_total=unanswered_total,
        unanswered=[
            UnansweredQuestion(
                question=(row["question"] or "")[:QUESTION_MAX_LENGTH],
                ask_count=row["ask_count"],
                last_asked_at=row["last_asked_at"],
                conversation_id=row["conversation_id"],
            )
            for row in unanswered_rows
        ],
        knowledge_documents=knowledge_documents,
    )


__all__ = ["get_overview", "WINDOW_DAYS", "DEFAULT_LIMIT", "MAX_LIMIT"]
