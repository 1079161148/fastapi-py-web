"""API 依赖：当前用户 / 当前租户成员（设计文档 4.2）。"""

import uuid
from collections.abc import Awaitable, Callable

from arq.connections import ArqRedis
from fastapi import Cookie, Depends, Header
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import rate_limit
from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.core.security import decode_token
from app.core.tenant import require_current_tenant_id, set_current_tenant
from app.db.models.conversation import OWNER_TYPE_GUEST, OWNER_TYPE_USER
from app.db.models.tenant import TenantMember
from app.db.models.user import User
from app.db.session import get_session
from app.llm.base import EmbeddingClient, LLMClient, LLMNotConfiguredError, RerankClient
from app.llm.factory import get_embedding_client, get_llm_client, get_rerank_client
from app.services import guest as guest_service
from app.services.conversations import ConversationOwner
from app.speech import SpeechClient, TranscriptionClient
from app.speech.factory import (
    get_required_speech_client,
    get_speech_client,
    get_transcription_client,
)
from app.workers.pool import get_pool

logger = get_logger("deps")
# 知识库写操作允许的租户内角色（设计文档 7：知识库属于租户管理动作）
KB_MANAGER_ROLES = ("owner", "admin", "agent")
# 渠道配置属于租户级设置（涉及外部账号与凭证）：只允许 owner/admin
TENANT_ADMIN_ROLES = ("owner", "admin")
# 坐席工作台：租户内三类角色都能接单（小团队里 owner 亲自回消息是常态）
AGENT_ROLES = ("owner", "admin", "agent")

BEARER_PREFIX = "Bearer "
# 访客标识 cookie（设计文档 5.5：HttpOnly + SameSite=Lax，不用 localStorage）
GUEST_COOKIE_NAME = "guest_id"


_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> User:
    """从 Authorization: Bearer <token> 解析当前用户。

    令牌自带租户（tid 声明），校验通过后写入租户上下文，
    后续查询即被 RLS 限制在该租户内。
    """
    if credentials is None:
        raise AppError(code="UNAUTHENTICATED", message="请先登录", status_code=401)

    claims = decode_token(credentials.credentials, expected_type="access")
    try:
        user_id = uuid.UUID(claims["sub"])
        tenant_id = uuid.UUID(claims["tid"])
    except (KeyError, ValueError) as exc:
        raise AppError(
            code="TOKEN_INVALID", message="登录凭证无效，请重新登录", status_code=401
        ) from exc

    # 令牌租户优先于请求头租户：防止用户持 A 租户令牌越权访问 B 租户
    set_current_tenant(tenant_id)

    user = await session.get(User, user_id)
    if user is None or user.status != "active":
        raise AppError(code="UNAUTHENTICATED", message="账号不存在或已禁用", status_code=401)
    return user


async def get_current_tenant_member(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> TenantMember:
    """当前用户在当前租户内的成员记录（含租户内角色）。"""
    tenant_id = require_current_tenant_id()
    result = await session.execute(
        select(TenantMember).where(
            TenantMember.tenant_id == tenant_id,
            TenantMember.user_id == user.id,
            TenantMember.status == "active",
        )
    )
    member = result.scalar_one_or_none()
    if member is None:
        raise AppError(code="TENANT_ACCESS_DENIED", message="你不属于当前租户", status_code=403)
    return member


def require_roles(*allowed: str) -> Callable[..., Awaitable[User]]:
    """角色门禁：按全局角色放行（租户内角色校验在 service 层按需使用）。"""

    async def checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            raise AppError(code="FORBIDDEN", message="权限不足", status_code=403)
        return user

    return checker


def get_tenant_header(x_tenant_id: str | None = Header(default=None)) -> str | None:
    """供登录等引导接口读取租户提示头（可选）。"""
    return x_tenant_id


def parse_guest_id(raw: str | None) -> uuid.UUID:
    """解析 cookie 中的 guest_id；非法/缺失一律视为会话失效（401）。"""
    if not raw:
        raise guest_service.GuestSessionInvalidError()
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        # 被篡改的 cookie 与过期同处理，不暴露具体原因
        raise guest_service.GuestSessionInvalidError() from exc


async def get_chat_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    guest_id: str | None = Cookie(default=None, alias=GUEST_COOKIE_NAME),
    session: AsyncSession = Depends(get_session),
) -> ConversationOwner:
    """对话主体：优先登录用户，其次访客 cookie（设计文档 5.5）。

    - 带 Bearer：走用户身份；令牌无效直接 401，不静默降级为访客
      （否则过期令牌 + 残留 guest cookie 会把用户数据写进访客会话）
    - 无 Bearer：必须有访客 cookie + 租户上下文（X-Tenant-Id / 生产子域名）
    """
    if credentials is not None:
        user = await get_current_user(credentials=credentials, session=session)
        return ConversationOwner(
            tenant_id=user.tenant_id, owner_type=OWNER_TYPE_USER, user_id=user.id
        )

    tenant_id = require_current_tenant_id()
    guest = await guest_service.get_active_session(
        session, tenant_id=tenant_id, guest_id=parse_guest_id(guest_id)
    )
    return ConversationOwner(
        tenant_id=tenant_id, owner_type=OWNER_TYPE_GUEST, guest_id=guest.guest_id
    )


async def enforce_chat_rate(
    owner: ConversationOwner = Depends(get_chat_principal),
) -> ConversationOwner:
    """对话限流（M8）：按用户/访客计数。

    为什么在依赖里而不是中间件：中间件阶段还没解析 JWT，只能按 IP；
    而同一 NAT 后面的多个访客共享 IP，按 IP 限流会互相误伤。
    访客没有身份，用 guest_id（cookie 签发，更换成本高于换 IP 头）。

    这里是"每轮对话"的限速：LLM 调用是唯一的按量计费路径，
    被打爆的代价是真实账单，比其他接口更需要严格。
    """
    subject = (
        f"user:{owner.user_id}"
        if owner.owner_type == OWNER_TYPE_USER
        else f"guest:{owner.guest_id}"
    )
    await rate_limit.enforce(
        rate_limit.CATEGORY_CHAT,
        subject=subject,
        limit=get_settings().rate_limit_chat,
    )
    return owner


def provide_llm_client() -> LLMClient:
    """LLM 客户端依赖：未配置时在这里抛 503，早于 SSE 响应头发出。"""
    return get_llm_client()


def provide_optional_llm_client() -> LLMClient | None:
    """文本对话入口用：未配置时返回 None，由路由决定走兜底文案还是 503。

    与 embedder 的降级不同：LLM 是回答的生产者，没有它"回答"本身不存在，
    所以只在租户显式配了兜底回复时才降级，否则仍然明确失败。
    """
    try:
        return get_llm_client()
    except LLMNotConfiguredError as exc:
        logger.warning("llm_not_configured", missing=exc.missing)
        return None


def provide_embedding_client() -> EmbeddingClient | None:
    """聊天链路的嵌入客户端：未配置返回 None，由检索服务降级为纯模型回答。

    设计文档 10 的降级要求是"检索失败 → 纯模型回答并标注无知识库依据"，
    因此这里不能像入队那样直接 503（否则知识库没配好整个聊天都不可用）。
    """
    try:
        return get_embedding_client()
    except LLMNotConfiguredError as exc:
        logger.warning("embedding_not_configured", missing=exc.missing)
        return None


def provide_required_embedding_client() -> EmbeddingClient:
    """知识库检索接口用：缺配置明确 503，不静默返回空结果。"""
    return get_embedding_client()


def provide_reranker() -> RerankClient | None:
    """重排可选：未配置时返回 None，检索退化为 RRF 顺序。"""
    return get_rerank_client()


def provide_transcription_client() -> TranscriptionClient:
    """语音消息入口：ASR 未配置必须 503（附缺失变量），不能静默降级成空文本。"""
    return get_transcription_client()


def provide_speech_client() -> SpeechClient | None:
    """TTS 是可选增强：未配置返回 None，流式链路跳过语音回复只发文字。"""
    return get_speech_client()


def provide_required_speech_client() -> SpeechClient:
    """用户显式点"转语音"时使用：未配置必须 503，不能静默不返回音频。"""
    return get_required_speech_client()


async def provide_job_queue() -> ArqRedis:
    """文档入库队列依赖：Redis 不可用时在请求早期报 503（不落半截数据）。"""
    return await get_pool()


async def require_kb_manager(
    member: TenantMember = Depends(get_current_tenant_member),
) -> TenantMember:
    if member.role not in KB_MANAGER_ROLES:
        raise AppError(
            code="FORBIDDEN",
            message="当前角色无权管理知识库（需要 owner/admin/agent）",
            status_code=403,
        )
    return member


async def require_agent(
    member: TenantMember = Depends(get_current_tenant_member),
) -> TenantMember:
    """坐席门禁：租户内成员均可接单，非成员在 get_current_tenant_member 就已 403。"""
    if member.role not in AGENT_ROLES:
        raise AppError(
            code="FORBIDDEN",
            message="当前角色无权使用坐席工作台（需要 owner/admin/agent）",
            status_code=403,
        )
    return member


async def require_tenant_admin(
    member: TenantMember = Depends(get_current_tenant_member),
) -> TenantMember:
    """渠道配置门禁：外部账号与回调地址是租户级设置，坐席不可改。"""
    if member.role not in TENANT_ADMIN_ROLES:
        raise AppError(
            code="FORBIDDEN",
            message="当前角色无权管理渠道配置（需要 owner/admin）",
            status_code=403,
        )
    return member
