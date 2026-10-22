"""租户与成员服务（设计文档 9.2：租户管理 API）。

成员模型的现实约束（与设计文档 6 的差异，此处显式记录）：
`users.tenant_id` 是账号的**主租户**，登录令牌里的 `tid` 取自它。
因此跨租户邀请（把一个已有账号挂到第二个租户）在当前登录流程下无法生效——
用户登进来的仍然是主租户。所以新增成员一律是"创建新账号 + 加入当前租户"，
已有邮箱直接 409，而不是假装支持邀请。

不变量（由本模块守护，不依赖 UI）：
- 每个租户至少保留一个 active 的 owner（改角色、移除成员都检查）
- 不能移除自己（避免管理员把自己锁在门外）
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, ConflictError, NotFoundError
from app.core.security import hash_password
from app.core.tenant import require_current_tenant_id
from app.db.models.tenant import Tenant, TenantMember
from app.db.models.user import RefreshToken, User
from app.schemas.tenant import TenantInfo, TenantMemberInfo, TenantQuotaUpdate

ROLE_OWNER = "owner"
# 租户内角色 → 全局角色（users.role 决定能进哪些后台，租户内角色决定管辖范围）
_GLOBAL_ROLE_BY_TENANT_ROLE = {
    "owner": "admin",
    "admin": "admin",
    "agent": "agent",
    "customer": "customer",
}


async def get_current_tenant(session: AsyncSession) -> TenantInfo:
    tenant = await session.get(Tenant, require_current_tenant_id())
    if tenant is None:
        raise NotFoundError("租户")
    return TenantInfo.model_validate(tenant)


async def update_tenant(
    session: AsyncSession,
    *,
    name: str | None = None,
    settings: dict[str, Any] | None = None,
) -> TenantInfo:
    """租户信息局部更新：name 与 settings 都只改提交的字段。

    settings 合并而非替换：配置字段会随版本增加，整体替换会让未提交的旧字段丢失。
    """
    tenant = await session.get(Tenant, require_current_tenant_id())
    if tenant is None:
        raise NotFoundError("租户")
    if name is not None:
        tenant.name = name
    if settings is not None:
        tenant.settings = {**tenant.settings, **settings}
    await session.commit()
    await session.refresh(tenant)
    return TenantInfo.model_validate(tenant)


async def update_quota(session: AsyncSession, payload: TenantQuotaUpdate) -> TenantInfo:
    tenant = await session.get(Tenant, require_current_tenant_id())
    if tenant is None:
        raise NotFoundError("租户")
    # 合并而非替换：调用方只提交要改的键
    merged = {**tenant.quota, **payload.model_dump(exclude_none=True)}
    tenant.quota = merged
    await session.commit()
    await session.refresh(tenant)
    return TenantInfo.model_validate(tenant)


async def list_members(session: AsyncSession) -> list[TenantMemberInfo]:
    tenant_id = require_current_tenant_id()
    result = await session.execute(
        select(TenantMember, User)
        .join(User, User.id == TenantMember.user_id)
        .where(TenantMember.tenant_id == tenant_id)
        .order_by(TenantMember.created_at)
    )
    return [
        TenantMemberInfo(
            user_id=user.id,
            email=user.email,
            username=user.username,
            display_name=user.display_name,
            role=member.role,
            status=member.status,
        )
        for member, user in result.all()
    ]


async def add_member(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    display_name: str,
    role: str,
    username: str | None = None,
) -> TenantMemberInfo:
    """创建账号并加入当前租户。邮箱已被占用时明确拒绝，不做静默接管。"""
    tenant_id = require_current_tenant_id()

    # 邮箱全局唯一，且存在性检查必须绕过 RLS（users 表按租户隔离）
    existing = await session.execute(
        text("SELECT tenant_id FROM auth_lookup_user(:email)"), {"email": email}
    )
    row = existing.mappings().first()
    if row is not None:
        if str(row["tenant_id"]) == str(tenant_id):
            raise ConflictError("该邮箱已是本租户成员")
        raise ConflictError(
            "该邮箱已在其他租户注册：当前版本账号绑定单一主租户，请用其他邮箱创建本租户成员"
        )

    # 用户名只在本租户内唯一：此处租户上下文已由依赖设置，直接查 users 表即可，
    # 无需 SECURITY DEFINER 函数（RLS 允许读本租户的行）
    normalized_username = username.strip().lower() if username else None
    if normalized_username:
        taken = await session.execute(
            select(User.id).where(
                User.tenant_id == tenant_id,
                User.username == normalized_username,
            )
        )
        if taken.first() is not None:
            raise ConflictError("该用户名在本租户内已被占用，请换一个")

    now = datetime.now(UTC)
    user = User(
        id=uuid.uuid4(),  # 显式生成：RLS 下 INSERT...RETURNING 需要上下文先于行存在
        email=email,
        username=normalized_username,
        password_hash=hash_password(password),
        display_name=display_name or email.split("@")[0],
        role=_GLOBAL_ROLE_BY_TENANT_ROLE[role],
        tenant_id=tenant_id,
        email_verified=False,
    )
    session.add(user)
    await session.flush()

    session.add(
        TenantMember(
            tenant_id=tenant_id,
            user_id=user.id,
            role=role,
            status="active",
            joined_at=now,
        )
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if normalized_username and "uq_users_tenant_username" in str(exc.orig):
            raise ConflictError("该用户名在本租户内已被占用，请换一个") from exc
        raise

    return TenantMemberInfo(
        user_id=user.id,
        email=user.email,
        username=user.username,
        display_name=user.display_name,
        role=role,
        status="active",
    )


async def update_member_role(
    session: AsyncSession, *, user_id: uuid.UUID, role: str
) -> TenantMemberInfo:
    tenant_id = require_current_tenant_id()
    member = await _get_member(session, tenant_id=tenant_id, user_id=user_id)

    if member.role == ROLE_OWNER and role != ROLE_OWNER:
        await _require_another_owner(session, tenant_id=tenant_id, exclude_user_id=user_id)

    member.role = role
    user = await session.get(User, user_id)
    if user is not None:
        user.role = _GLOBAL_ROLE_BY_TENANT_ROLE[role]
    await session.commit()

    return TenantMemberInfo(
        user_id=member.user_id,
        email=user.email if user is not None else "",
        username=user.username if user is not None else None,
        display_name=user.display_name if user is not None else "",
        role=member.role,
        status=member.status,
    )


async def remove_member(
    session: AsyncSession, *, user_id: uuid.UUID, current_user_id: uuid.UUID
) -> None:
    """移除成员。

    主租户成员被移除后账号一并禁用（单租户模型下他本来也进不来）；
    同时吊销未过期的 refresh 令牌——只禁用账号而不吊销，已签发的刷新令牌
    还能换出新 access token，等于没禁用。
    """
    tenant_id = require_current_tenant_id()
    if user_id == current_user_id:
        raise AppError(
            code="CANNOT_REMOVE_SELF",
            message="不能移除自己：请让其他管理员操作",
            status_code=409,
            expose=True,
        )

    member = await _get_member(session, tenant_id=tenant_id, user_id=user_id)
    if member.role == ROLE_OWNER:
        await _require_another_owner(session, tenant_id=tenant_id, exclude_user_id=user_id)

    user = await session.get(User, user_id)
    await session.delete(member)

    if user is not None and user.tenant_id == tenant_id:
        user.status = "disabled"
        await session.execute(
            update(RefreshToken)
            .where(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(UTC))
        )
    await session.commit()


async def _get_member(
    session: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> TenantMember:
    member = await session.get(TenantMember, (tenant_id, user_id))
    if member is None:
        raise NotFoundError("租户成员")
    return member


async def _require_another_owner(
    session: AsyncSession, *, tenant_id: uuid.UUID, exclude_user_id: uuid.UUID
) -> None:
    """确认除 exclude_user_id 之外还有 active 的 owner，否则拒绝。"""
    others = await session.scalar(
        select(func.count())
        .select_from(TenantMember)
        .where(
            TenantMember.tenant_id == tenant_id,
            TenantMember.role == ROLE_OWNER,
            TenantMember.status == "active",
            TenantMember.user_id != exclude_user_id,
        )
    )
    if not others:
        raise AppError(
            code="LAST_OWNER_PROTECTED",
            message="租户必须至少保留一个 owner：请先指定其他 owner 再操作",
            status_code=409,
            expose=True,
        )
