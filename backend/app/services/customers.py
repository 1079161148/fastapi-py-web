"""客户管理（M8）：按 owner 聚合会话视图。

数据来源只有 conversations / messages / users 三张表，不建客户档案表——
当前版本的"客户"就是"这段会话属于谁"，引入独立档案表会产生同步成本，
而客服场景关心的（历史会话、最近状态、评价）都能从会话聚合出来。
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant import require_current_tenant_id
from app.schemas.support import CustomerInfo, CustomerListResponse

_LIST_SQL = text(
    """
    with conv as (
        select c.id, c.owner_type, c.status, c.updated_at,
               coalesce(c.user_id::text, c.guest_id::text) as owner_id
        from conversations c
        where c.tenant_id = :tenant_id
    ),
    agg as (
        select owner_type, owner_id,
               count(*) as conversation_count,
               max(updated_at) as last_active_at
        from conv
        group by owner_type, owner_id
    ),
    -- 每个客户最近一条会话的状态（按 updated_at 取最新）
    latest as (
        select distinct on (owner_type, owner_id)
               owner_type, owner_id, status as last_conversation_status
        from conv
        order by owner_type, owner_id, updated_at desc
    ),
    rated as (
        select coalesce(c.user_id::text, c.guest_id::text) as owner_id,
               count(m.rating) as total_ratings,
               avg(m.rating) as avg_rating
        from messages m
        join conversations c on c.id = m.conversation_id
        where c.tenant_id = :tenant_id and m.rating is not null
        group by 1
    )
    select a.owner_type, a.owner_id, a.conversation_count, a.last_active_at,
           l.last_conversation_status,
           coalesce(r.total_ratings, 0) as total_ratings,
           r.avg_rating,
           u.email, u.display_name
    from agg a
    join latest l using (owner_type, owner_id)
    left join rated r using (owner_id)
    left join users u on a.owner_type = 'user' and u.id = a.owner_id::uuid
    order by a.last_active_at desc
    limit :limit offset :offset
    """
)

_COUNT_SQL = text(
    """
    select count(*) from (
        select 1 from conversations c
        where c.tenant_id = :tenant_id
        group by c.owner_type, coalesce(c.user_id::text, c.guest_id::text)
    ) s
    """
)


async def list_customers(
    session: AsyncSession, *, limit: int = 50, offset: int = 0
) -> CustomerListResponse:
    tenant_id = require_current_tenant_id()
    rows = await session.execute(
        _LIST_SQL, {"tenant_id": tenant_id, "limit": min(limit, 200), "offset": offset}
    )
    total = await session.scalar(_COUNT_SQL, {"tenant_id": tenant_id})

    items = []
    for row in rows.mappings():
        is_user = row["owner_type"] == "user"
        fallback_name = row["display_name"] if is_user else f"访客-{str(row['owner_id'])[:8]}"
        items.append(
            CustomerInfo(
                owner_type=row["owner_type"],
                owner_id=row["owner_id"],
                display_name=row["display_name"] or fallback_name,
                email=row["email"],
                conversation_count=row["conversation_count"],
                last_active_at=row["last_active_at"].isoformat() if row["last_active_at"] else None,
                last_conversation_status=row["last_conversation_status"] or "",
                total_ratings=row["total_ratings"] or 0,
                avg_rating=round(float(row["avg_rating"]), 2) if row["avg_rating"] else None,
            )
        )
    return CustomerListResponse(items=items, total=total or 0)
