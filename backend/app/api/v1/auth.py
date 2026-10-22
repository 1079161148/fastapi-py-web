"""认证路由（设计文档第 7 章）。"""

import uuid

from fastapi import APIRouter, Cookie, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import GUEST_COOKIE_NAME, get_current_user
from app.db.models.user import User
from app.db.session import get_session
from app.schemas.auth import (
    AuthResponse,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    UserInfo,
)
from app.schemas.chat import MergeGuestResult
from app.services import auth as auth_service
from app.services import guest as guest_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=AuthResponse, status_code=201)
async def register(
    payload: RegisterRequest, session: AsyncSession = Depends(get_session)
) -> AuthResponse:
    """注册即开户：创建租户 + 管理员账号，返回令牌对。"""
    return await auth_service.register(
        session,
        email=payload.email,
        password=payload.password,
        display_name=payload.display_name,
        tenant_name=payload.tenant_name,
        username=payload.username,
    )


@router.post("/login", response_model=AuthResponse)
async def login(
    payload: LoginRequest, session: AsyncSession = Depends(get_session)
) -> AuthResponse:
    """`identifier` 同时接受邮箱与用户名（旧客户端的 `email` 字段仍然有效）。

    用户名登录需同时提供 `tenant`（租户 slug）：用户名仅租户内唯一。
    """
    return await auth_service.login(
        session,
        identifier=payload.login_id,
        password=payload.password,
        tenant=payload.tenant,
    )


@router.post("/refresh", response_model=AuthResponse)
async def refresh(
    payload: RefreshRequest, session: AsyncSession = Depends(get_session)
) -> AuthResponse:
    """刷新令牌对；旧 refresh 立即吊销（轮换防重放）。"""
    return await auth_service.refresh(session, refresh_token=payload.refresh_token)


@router.post("/logout", status_code=204)
async def logout(payload: LogoutRequest, session: AsyncSession = Depends(get_session)) -> None:
    await auth_service.logout(session, refresh_token=payload.refresh_token)


@router.get("/me", response_model=UserInfo)
async def me(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> UserInfo:
    """当前登录用户信息（前端启动时拉取，用于恢复登录态）。

    带上租户内角色：前端导航按 tenant_role 收口，避免"平台 admin 在别人
    租户里看得见菜单、点进去 403"这种错位。
    """
    return await auth_service.build_user_info(session, user)


@router.post("/merge-guest", response_model=MergeGuestResult)
async def merge_guest(
    response: Response,
    guest_id: str | None = Cookie(default=None, alias=GUEST_COOKIE_NAME),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MergeGuestResult:
    """访客数据合并（设计文档 5.5 第③步）：双凭证校验。

    凭证一 = Authorization: Bearer <access token>（证明"他是谁"）
    凭证二 = HttpOnly cookie guest_id（关联"他之前聊了什么"）
    两者缺一不可：只信 token 会合并别人的访客数据，只信 guest_id 可被劫持。

    幂等：重复/并发提交、guest 已作废、没有访客会话都返回 200，
    不向用户抛错。合并成功后立即清除 cookie。
    """
    result = await guest_service.merge_guest(
        session,
        user=user,
        guest_id=_parse_optional_guest_id(guest_id),
    )
    # 只在确实处理过访客会话时清 cookie：跨租户/不存在的 guest 保持不变，
    # 否则访客在别的租户登录一次就会丢掉自己的匿名历史
    if result.status != guest_service.MERGE_STATUS_NO_SESSION:
        response.delete_cookie(GUEST_COOKIE_NAME, path="/")
    return MergeGuestResult(
        merged_conversations=result.merged_conversations, status=result.status
    )


def _parse_optional_guest_id(raw: str | None) -> uuid.UUID | None:
    """cookie 缺失或格式非法都不算错误：语义等同"没有可合并的访客会话"。"""
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None
