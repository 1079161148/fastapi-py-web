"""认证与令牌（设计文档 3.1 / 8：PyJWT + bcrypt，access 短 + refresh 长可吊销）。

令牌声明约定：
- type: "access" | "refresh"
- sub:  用户 ID
- tid:  租户 ID（登录时确定，令牌即绑定租户，换租户需重新登录或显式切换）
- role: 全局角色
- jti:  仅 refresh 携带，与 refresh_tokens 表对应，用于吊销
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt

from app.core.config import get_settings
from app.core.exceptions import AppError

settings = get_settings()

ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    """bcrypt 哈希。截断到 72 字节是 bcrypt 算法上限，超长部分无安全性增益。"""
    password_bytes = password.encode("utf-8")[:72]
    return bcrypt.hashpw(password_bytes, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8")[:72], password_hash.encode("utf-8"))


def _create_token(
    *,
    token_type: str,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    role: str,
    ttl: timedelta,
    jti: str | None = None,
) -> str:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "type": token_type,
        "sub": str(user_id),
        "tid": str(tenant_id),
        "role": role,
        "iat": now,
        "exp": now + ttl,
    }
    if jti is not None:
        claims["jti"] = jti
    return jwt.encode(claims, settings.jwt_secret, algorithm=ALGORITHM)


def create_access_token(*, user_id: uuid.UUID, tenant_id: uuid.UUID, role: str) -> str:
    return _create_token(
        token_type="access",
        user_id=user_id,
        tenant_id=tenant_id,
        role=role,
        ttl=timedelta(minutes=settings.access_token_ttl_minutes),
    )


def create_refresh_token(*, user_id: uuid.UUID, tenant_id: uuid.UUID, role: str, jti: str) -> str:
    return _create_token(
        token_type="refresh",
        user_id=user_id,
        tenant_id=tenant_id,
        role=role,
        ttl=timedelta(days=settings.refresh_token_ttl_days),
        jti=jti,
    )


def create_audio_token(*, message_id: uuid.UUID, tenant_id: uuid.UUID) -> str:
    """签发音频访问令牌（M5）。

    为什么需要它：`<audio src>` 由浏览器发起，无法附加 Authorization 头；
    登录用户又没有访客 cookie。用短时令牌把"凭证"放进 URL，是私有存储
    "下载签发短时效 URL"（设计文档第 8 章）在本地存储上的等价实现。

    令牌只绑定单条消息 + 租户，不能用于其他接口（type=audio 会被
    access/refresh 校验拒绝，反之亦然）。
    """
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "type": "audio",
        "sub": str(message_id),
        "tid": str(tenant_id),
        "iat": now,
        "exp": now + timedelta(seconds=settings.voice_audio_url_ttl_seconds),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=ALGORITHM)


def create_asset_token(*, message_id: uuid.UUID, tenant_id: uuid.UUID) -> str:
    """签发图片等静态附件的访问令牌（M8）。

    与 audio 令牌同理：`<img src>` 由浏览器直接发起，加不了 Authorization 头。
    用独立的 type 隔离，音频令牌读不了图片，图片令牌也读不了音频。
    """
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "type": "asset",
        "sub": str(message_id),
        "tid": str(tenant_id),
        "iat": now,
        "exp": now + timedelta(seconds=settings.asset_url_ttl_seconds),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=ALGORITHM)


def decode_token(token: str, *, expected_type: str) -> dict[str, Any]:
    """解码并校验令牌类型；任何失败都转为 401，不泄露具体原因。"""
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise AppError(
            code="TOKEN_EXPIRED", message="登录已过期，请重新登录", status_code=401
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise AppError(
            code="TOKEN_INVALID", message="登录凭证无效，请重新登录", status_code=401
        ) from exc

    if claims.get("type") != expected_type:
        raise AppError(
            code="TOKEN_TYPE_MISMATCH",
            message="令牌类型不匹配，请使用正确的凭证",
            status_code=401,
        )
    return claims
