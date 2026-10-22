"""访客会话路由（设计文档 5.5 / 第 7 章）。

租户来源：X-Tenant-Id 请求头（本地/内部）或生产环境子域名，
由 RequestContextMiddleware 注入；访客会话必须落在某个租户下。
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Cookie, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import GUEST_COOKIE_NAME
from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.tenant import require_current_tenant_id
from app.db.models.conversation import GuestSession
from app.db.session import get_session
from app.schemas.chat import GuestSessionCurrent, GuestSessionInfo
from app.services import guest as guest_service
from app.services import tenant_settings

router = APIRouter(prefix="/guest", tags=["guest"])

settings = get_settings()


def _set_guest_cookie(response: Response, guest_id: uuid.UUID, expires_at: datetime) -> None:
    """写入访客 cookie：HttpOnly 防 XSS 窃取，SameSite=Lax 防跨站携带。

    Secure 仅在 production 打开：本地 http://localhost 下带 Secure 的 cookie
    会被浏览器直接丢弃，表现为"每次刷新都换新访客"。
    """
    remaining = max(int((expires_at - datetime.now(UTC)).total_seconds()), 0)
    response.set_cookie(
        key=GUEST_COOKIE_NAME,
        value=str(guest_id),
        max_age=remaining,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        path="/",
    )


async def _resolve_existing(
    session: AsyncSession, *, tenant_id: uuid.UUID, raw_guest_id: str | None
) -> GuestSession | None:
    """老 cookie 能用就复用，不能用就当没有。

    cookie 可能过期、被篡改，或对应的 DB 行已被清理——这些都属于
    "访客回来时已经没有会话"，重新签发即可，不该报错打断用户。
    """
    if not raw_guest_id:
        return None
    try:
        return await guest_service.get_active_session(
            session, tenant_id=tenant_id, guest_id=uuid.UUID(raw_guest_id)
        )
    except (AppError, ValueError):
        return None


@router.post("/sessions", response_model=GuestSessionInfo)
async def create_or_resume_session(
    response: Response,
    guest_id: str | None = Cookie(default=None, alias=GUEST_COOKIE_NAME),
    session: AsyncSession = Depends(get_session),
) -> GuestSessionInfo:
    """签发访客标识；已有可用会话则复用（幂等，刷新页面不丢历史）。"""
    tenant_id = require_current_tenant_id()
    await guest_service.ensure_tenant(session, tenant_id)
    # 欢迎语随访客会话下发：访客没有令牌，读不到需要登录的 /tenants/current
    welcome = (await tenant_settings.load(session, tenant_id)).welcome_message

    existing = await _resolve_existing(session, tenant_id=tenant_id, raw_guest_id=guest_id)
    if existing is not None:
        _set_guest_cookie(response, existing.guest_id, existing.expires_at)
        return GuestSessionInfo(
            guest_id=existing.guest_id,
            expires_at=existing.expires_at,
            status=existing.status,
            welcome_message=welcome,
        )

    guest = await guest_service.start_session(session, tenant_id=tenant_id)
    _set_guest_cookie(response, guest.guest_id, guest.expires_at)
    return GuestSessionInfo(
        guest_id=guest.guest_id,
        expires_at=guest.expires_at,
        status=guest.status,
        welcome_message=welcome,
    )


@router.get("/sessions/current", response_model=GuestSessionCurrent)
async def get_current_session(
    guest_id: str | None = Cookie(default=None, alias=GUEST_COOKIE_NAME),
    session: AsyncSession = Depends(get_session),
) -> GuestSessionCurrent:
    """当前访客会话（无则字段为空，不返回 404）。

    登录后前端用它判断"是否存在未合并的访客对话"，决定是否提示合并。
    """
    tenant_id = require_current_tenant_id()
    existing = await _resolve_existing(session, tenant_id=tenant_id, raw_guest_id=guest_id)
    if existing is None:
        return GuestSessionCurrent()

    count = await guest_service.count_conversations(
        session, tenant_id=tenant_id, guest_id=existing.guest_id
    )
    return GuestSessionCurrent(
        guest_id=existing.guest_id, expires_at=existing.expires_at, conversation_count=count
    )
