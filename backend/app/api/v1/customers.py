"""客户管理路由（M8）：按 owner 聚合的客户视图。"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import require_agent
from app.db.models.tenant import TenantMember
from app.db.session import get_session
from app.schemas.support import CustomerListResponse
from app.services import customers as customer_service

router = APIRouter(prefix="/customers", tags=["customers"])


@router.get("", response_model=CustomerListResponse)
async def list_customers(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    _agent: TenantMember = Depends(require_agent),
) -> CustomerListResponse:
    return await customer_service.list_customers(session, limit=limit, offset=offset)
