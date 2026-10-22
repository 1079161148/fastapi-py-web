"""认证服务（设计文档 M1 / 5.5 / 8）。

登录引导查询走 SECURITY DEFINER 函数 auth_lookup_user：
users 表启用 RLS 后，未设租户上下文时按邮箱查不到用户，
而放开 users 策略会破坏隔离——用函数把"按邮箱查用户"这一个能力
显式授权给应用角色，是教科书式的解法（见首个迁移）。
"""

import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AppError, ConflictError
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.core.tenant import set_current_tenant
from app.db.models.tenant import Tenant, TenantMember
from app.db.models.user import RefreshToken, User
from app.schemas.auth import AuthResponse, TokenPair, UserInfo

settings = get_settings()


def _issue_tokens(user: User) -> tuple[TokenPair, str]:
    """签发令牌对，返回 (tokens, refresh_jti)——jti 需落库以便吊销。"""
    jti = secrets.token_hex(16)
    access = create_access_token(user_id=user.id, tenant_id=user.tenant_id, role=user.role)
    refresh = create_refresh_token(
        user_id=user.id, tenant_id=user.tenant_id, role=user.role, jti=jti
    )
    return TokenPair(access_token=access, refresh_token=refresh), jti


async def build_user_info(session: AsyncSession, user: User) -> UserInfo:
    """UserInfo 的唯一构造点。

    租户内角色必须查成员表：平台级 user.role 与租户角色是两回事，
    知识库/坐席台/渠道配置的权限判定用的是后者。成员被移除或停用时
    留空字符串，前端按"无权限"处理，而不是退回 user.role 蒙混过去。
    """
    result = await session.execute(
        select(TenantMember.role).where(
            TenantMember.tenant_id == user.tenant_id,
            TenantMember.user_id == user.id,
            TenantMember.status == "active",
        )
    )
    tenant_role = result.scalar_one_or_none() or ""
    return UserInfo(
        id=user.id,
        email=user.email,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        tenant_role=tenant_role,
        tenant_id=user.tenant_id,
    )


async def _to_auth_response(
    session: AsyncSession, user: User, tokens: TokenPair
) -> AuthResponse:
    return AuthResponse(tokens=tokens, user=await build_user_info(session, user))


def _slugify_tenant_name(name: str) -> str:
    """租户 slug：取自名称，附加随机后缀保证全局唯一（子域名解析用）。"""
    base = "".join(ch.lower() if ch.isalnum() else "-" for ch in name).strip("-")[:40]
    return f"{base or 'tenant'}-{secrets.token_hex(3)}"


async def _apply_tenant_context(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """在事务内显式设置租户上下文。

    RLS 钩子只在事务开启时执行一次；注册/登录等引导流程先写 tenants
    后写 users，中途租户才确定，必须补一次 SET LOCAL 才能通过
    users / refresh_tokens 的 WITH CHECK 策略。UUID 由本服务生成，
    不存在注入面，故直接内联字面量（PG 的 SET 不支持绑定参数）。
    """
    set_current_tenant(tenant_id)
    await session.execute(text(f"SET LOCAL app.tenant_id = '{tenant_id}'"))


async def register(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    display_name: str,
    tenant_name: str,
    username: str | None = None,
) -> AuthResponse:
    """注册即开户：创建租户 + 属主用户 + 成员关系，单事务提交。"""
    # 存在性检查走 SECURITY DEFINER 函数：无租户上下文时 RLS 会屏蔽 users 表
    existing = await session.execute(
        text("SELECT 1 FROM auth_lookup_user(:email)"), {"email": email}
    )
    if existing.first() is not None:
        raise ConflictError("该邮箱已注册，请直接登录")

    # 用户名统一小写存储。这里**不需要**做占用检查：注册总是新建租户，
    # 而用户名只在本租户内唯一，新租户里不可能有同名用户
    normalized_username = username.strip().lower() if username else None

    tenant = Tenant(
        id=uuid.uuid4(),  # 显式生成：RLS 下 INSERT...RETURNING 需要上下文先于行存在
        name=tenant_name,
        slug=_slugify_tenant_name(tenant_name),
        quota={
            "max_messages_per_day": 1000,
            "max_concurrent": 10,
            "max_storage_gb": 10,
            "max_tokens_per_day": 500_000,
        },
    )
    # 上下文必须先于 INSERT：RLS 的 USING 同样作用于 INSERT...RETURNING，
    # 而 ORM 为读取 created_at/updated_at 必然使用 RETURNING
    await _apply_tenant_context(session, tenant.id)
    session.add(tenant)
    await session.flush()

    user = User(
        email=email,
        username=normalized_username,
        password_hash=hash_password(password),
        display_name=display_name or email.split("@")[0],
        role="admin",  # 注册者是其租户的管理员
        tenant_id=tenant.id,
        email_verified=False,
    )
    session.add(user)
    await session.flush()

    session.add(TenantMember(tenant_id=tenant.id, user_id=user.id, role="owner"))

    tokens, jti = _issue_tokens(user)
    session.add(
        RefreshToken(
            user_id=user.id,
            tenant_id=tenant.id,
            jti=jti,
            expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_ttl_days),
        )
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        # 唯一索引是最后一道防线：转成可读的 409 而不是 500
        if normalized_username and "uq_users_tenant_username" in str(exc.orig):
            raise ConflictError("该用户名已被占用，请换一个") from exc
        raise
    return await _to_auth_response(session, user, tokens)


async def login(
    session: AsyncSession, *, identifier: str, password: str, tenant: str | None = None
) -> AuthResponse:
    """支持邮箱或用户名登录。

    分流规则：含 `@` 视为邮箱，否则视为用户名。邮箱格式在注册时已强制含 @，
    用户名允许的字符集 `[A-Za-z0-9_]` 不含 @，两者必然不歧义。

    **用户名登录必须提供租户标识（slug）**：用户名只在本租户内唯一，
    没有租户就无法确定要查谁。邮箱是全局唯一，所以不需要。
    两种查询都走 SECURITY DEFINER 函数（无租户上下文时 RLS 会屏蔽 users/tenants）。
    """
    if "@" in identifier:
        result = await session.execute(
            text("SELECT * FROM auth_lookup_user(:email)"), {"email": identifier}
        )
    else:
        if not tenant:
            raise AppError(
                code="TENANT_REQUIRED",
                message="使用用户名登录时需要填写组织标识（租户 slug）；"
                "或直接使用邮箱登录",
                status_code=400,
                expose=True,
            )
        result = await session.execute(
            text("SELECT * FROM auth_lookup_user_by_name(:tenant, :username)"),
            {"tenant": tenant.strip().lower(), "username": identifier},
        )
    row = result.mappings().first()
    if (
        row is None
        or row["status"] != "active"
        # 统一报错：不区分"标识不存在"与"密码错误"，避免账号枚举
        or not verify_password(password, row["password_hash"])
    ):
        raise AppError(
            code="INVALID_CREDENTIALS", message="邮箱/用户名或密码错误", status_code=401
        )

    await _apply_tenant_context(session, uuid.UUID(str(row["tenant_id"])))

    user = await session.get(User, uuid.UUID(str(row["id"])))
    if user is None:  # 理论不可达：函数刚返回该行
        raise AppError(
            code="INVALID_CREDENTIALS", message="邮箱/用户名或密码错误", status_code=401
        )

    user.last_login_at = datetime.now(UTC)
    tokens, jti = _issue_tokens(user)
    session.add(
        RefreshToken(
            user_id=user.id,
            tenant_id=user.tenant_id,
            jti=jti,
            expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_ttl_days),
        )
    )
    await session.commit()
    return await _to_auth_response(session, user, tokens)


async def refresh(session: AsyncSession, *, refresh_token: str) -> AuthResponse:
    claims = decode_token(refresh_token, expected_type="refresh")
    jti = claims.get("jti")
    if not jti:
        raise AppError(code="TOKEN_INVALID", message="刷新凭证无效，请重新登录", status_code=401)

    try:
        tenant_id = uuid.UUID(claims["tid"])
    except (KeyError, ValueError) as exc:
        raise AppError(
            code="TOKEN_INVALID", message="刷新凭证无效，请重新登录", status_code=401
        ) from exc

    await _apply_tenant_context(session, tenant_id)

    stored = await session.execute(select(RefreshToken).where(RefreshToken.jti == jti))
    record = stored.scalar_one_or_none()
    if record is None or record.revoked_at is not None:
        raise AppError(code="TOKEN_REVOKED", message="登录态已失效，请重新登录", status_code=401)
    if record.expires_at <= datetime.now(UTC):
        raise AppError(code="TOKEN_EXPIRED", message="登录已过期，请重新登录", status_code=401)

    user = await session.get(User, uuid.UUID(claims["sub"]))
    if user is None or user.status != "active":
        raise AppError(code="UNAUTHENTICATED", message="账号不存在或已禁用", status_code=401)

    # 轮换：旧 refresh 立即吊销，签发新对（防重放）
    record.revoked_at = datetime.now(UTC)
    tokens, new_jti = _issue_tokens(user)
    session.add(
        RefreshToken(
            user_id=user.id,
            tenant_id=user.tenant_id,
            jti=new_jti,
            expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_ttl_days),
        )
    )
    await session.commit()
    return await _to_auth_response(session, user, tokens)


async def logout(session: AsyncSession, *, refresh_token: str) -> None:
    """吊销 refresh 令牌；access 短时效自然过期。"""
    try:
        claims = decode_token(refresh_token, expected_type="refresh")
    except AppError:
        return  # 已失效的令牌无需重复吊销
    jti = claims.get("jti")
    if not jti:
        return
    result = await session.execute(select(RefreshToken).where(RefreshToken.jti == jti))
    record = result.scalar_one_or_none()
    if record is not None and record.revoked_at is None:
        record.revoked_at = datetime.now(UTC)
        await session.commit()
