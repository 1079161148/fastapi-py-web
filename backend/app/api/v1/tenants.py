"""租户路由（设计文档 9.2）。

所有接口要求已登录，且令牌租户即上下文租户（deps 层保证）。
写操作（改名、配额、成员、运营配置）要求租户 owner/admin：这些是租户级设置，
坐席与终端用户不应改动。写操作同时落审计日志（M8）。
"""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    get_current_tenant_member,
    get_current_user,
    require_tenant_admin,
)
from app.db.models.tenant import TenantMember
from app.db.models.user import User
from app.db.session import get_session
from app.schemas.tenant import (
    TenantInfo,
    TenantMemberCreate,
    TenantMemberInfo,
    TenantMemberUpdate,
    TenantQuotaUpdate,
    TenantUpdate,
)
from app.services import audit as audit_service
from app.services import tenants as tenant_service

router = APIRouter(prefix="/tenants", tags=["tenants"])


@router.get("/current", response_model=TenantInfo)
async def get_current(
    session: AsyncSession = Depends(get_session),
    _member: TenantMember = Depends(get_current_tenant_member),
) -> TenantInfo:
    return await tenant_service.get_current_tenant(session)


@router.patch("/current", response_model=TenantInfo)
async def update_current(
    payload: TenantUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    _admin: TenantMember = Depends(require_tenant_admin),
) -> TenantInfo:
    result = await tenant_service.update_tenant(
        session,
        name=payload.name,
        settings=payload.settings.model_dump(exclude_unset=True) if payload.settings else None,
    )
    changed = [
        key
        for key, value in (("name", payload.name), ("settings", payload.settings))
        if value is not None
    ]
    if changed:
        await audit_service.record(
            session,
            action=audit_service.ACTION_TENANT_UPDATE,
            actor_id=current_user.id,
            actor_name=current_user.display_name,
            target_type="tenant",
            detail={"changed": changed},
        )
        await session.commit()
    return result


@router.patch("/current/quota", response_model=TenantInfo)
async def update_quota(
    payload: TenantQuotaUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    _admin: TenantMember = Depends(require_tenant_admin),
) -> TenantInfo:
    result = await tenant_service.update_quota(session, payload)
    await audit_service.record(
        session,
        action=audit_service.ACTION_TENANT_QUOTA,
        actor_id=current_user.id,
        actor_name=current_user.display_name,
        target_type="tenant",
        detail={"quota": payload.model_dump(exclude_none=True)},
    )
    await session.commit()
    return result


@router.get("/current/members", response_model=list[TenantMemberInfo])
async def list_members(
    session: AsyncSession = Depends(get_session),
    _member: TenantMember = Depends(get_current_tenant_member),
) -> list[TenantMemberInfo]:
    return await tenant_service.list_members(session)


@router.post("/current/members", response_model=TenantMemberInfo, status_code=201)
async def add_member(
    payload: TenantMemberCreate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    _admin: TenantMember = Depends(require_tenant_admin),
) -> TenantMemberInfo:
    result = await tenant_service.add_member(
        session,
        email=payload.email,
        password=payload.password,
        display_name=payload.display_name,
        role=payload.role,
        username=payload.username,
    )
    await audit_service.record(
        session,
        action=audit_service.ACTION_MEMBER_ADD,
        actor_id=current_user.id,
        actor_name=current_user.display_name,
        target_type="member",
        target_id=str(result.user_id),
        detail={"email": result.email, "role": result.role},
    )
    await session.commit()
    return result


@router.patch("/current/members/{user_id}", response_model=TenantMemberInfo)
async def update_member_role(
    user_id: uuid.UUID,
    payload: TenantMemberUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    _admin: TenantMember = Depends(require_tenant_admin),
) -> TenantMemberInfo:
    result = await tenant_service.update_member_role(
        session, user_id=user_id, role=payload.role
    )
    await audit_service.record(
        session,
        action=audit_service.ACTION_MEMBER_ROLE,
        actor_id=current_user.id,
        actor_name=current_user.display_name,
        target_type="member",
        target_id=str(user_id),
        detail={"role": payload.role},
    )
    await session.commit()
    return result


@router.delete("/current/members/{user_id}", status_code=204)
async def remove_member(
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    _admin: TenantMember = Depends(require_tenant_admin),
) -> None:
    await tenant_service.remove_member(
        session, user_id=user_id, current_user_id=current_user.id
    )
    await audit_service.record(
        session,
        action=audit_service.ACTION_MEMBER_REMOVE,
        actor_id=current_user.id,
        actor_name=current_user.display_name,
        target_type="member",
        target_id=str(user_id),
    )
    await session.commit()
