"""图片附件（M8）：格式校验、对象存储、短时访问地址。

只做"文件这一侧"：落库、广播由调用方（api/v1/conversations.py）负责。
如果把这三件事都塞进来，会和 services/messages.py 形成循环依赖
（messages 需要 asset_url 生成附件 URL，assets 需要 publish 广播消息）。

格式判定基于文件头而不是客户端声明的 Content-Type：
浏览器给的类型可以随意伪造，按魔数判定才拦得住"改名成 .png 的 HTML"。
"""

import uuid
from typing import Any

from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.core.security import create_asset_token
from app.db.models.conversation import Message
from app.storage import get_storage

logger = get_logger("assets")

ATTACHMENT_TYPE_IMAGE = "image"
MESSAGE_TYPE_IMAGE = "image"

# 魔数 → mime。WebP 需要额外看第 8-12 字节，单独判断
IMAGE_SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
)
WEBP_RIFF = b"RIFF"
WEBP_MARK = b"WEBP"


def detect_image(data: bytes) -> tuple[str, str] | None:
    """按文件头判定图片类型，返回 (mime, 后缀)；不支持的格式返回 None。"""
    for signature, mime, suffix in IMAGE_SIGNATURES:
        if data.startswith(signature):
            return mime, suffix
    if len(data) >= 12 and data.startswith(WEBP_RIFF) and data[8:12] == WEBP_MARK:
        return "image/webp", ".webp"
    return None


def build_object_key(
    *, tenant_id: uuid.UUID, conversation_id: uuid.UUID, message_id: uuid.UUID, suffix: str
) -> str:
    """存储 key 由服务端拼装（不接受用户文件名）：
    image/{tenant}/{conversation}/{message}{suffix}
    以 message_id 命名，让"文件 ↔ 消息"一一对应，排障时不用查库。
    """
    return f"image/{tenant_id}/{conversation_id}/{message_id}{suffix}"


def image_url(*, message_id: uuid.UUID, tenant_id: uuid.UUID) -> str:
    """短时访问地址：`<img src>` 带不了 Authorization，只能把凭证放 query。"""
    token = create_asset_token(message_id=message_id, tenant_id=tenant_id)
    return f"/api/v1/messages/{message_id}/image?token={token}"


def find_image_attachment(message: Message) -> dict[str, Any] | None:
    for item in message.attachments or []:
        if isinstance(item, dict) and item.get("type") == ATTACHMENT_TYPE_IMAGE:
            return item
    return None


async def store_image(
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    data: bytes,
) -> dict[str, Any]:
    """校验并落盘，返回可写入 messages.attachments 的结构（含 storage_key，不外发）。"""
    limit_mb = get_settings().chat_image_max_mb
    if not data:
        raise AppError(code="IMAGE_EMPTY", message="图片内容为空", status_code=422)
    if len(data) > limit_mb * 1024 * 1024:
        raise AppError(
            code="IMAGE_TOO_LARGE",
            message=f"图片不能超过 {limit_mb}MB，请压缩后重试",
            status_code=413,
        )

    detected = detect_image(data)
    if detected is None:
        raise AppError(
            code="IMAGE_TYPE_NOT_ALLOWED",
            message="仅支持 PNG / JPEG / WebP 图片",
            status_code=422,
        )
    mime, suffix = detected

    key = build_object_key(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        message_id=message_id,
        suffix=suffix,
    )
    await get_storage().save(key, data)
    return {
        "type": ATTACHMENT_TYPE_IMAGE,
        "mime_type": mime,
        "size_bytes": len(data),
        "storage_key": key,
    }


async def read_image(message: Message) -> tuple[bytes, str]:
    """按消息附件读回图片字节；文件缺失时留痕并给出可读错误。"""
    attachment = find_image_attachment(message)
    if attachment is None:
        raise AppError(code="IMAGE_NOT_FOUND", message="该消息没有图片", status_code=404)

    key = attachment.get("storage_key")
    if not isinstance(key, str) or not key:
        raise AppError(code="IMAGE_NOT_FOUND", message="图片记录不完整", status_code=404)

    try:
        data = await get_storage().read(key)
    except FileNotFoundError as exc:
        # 存储与数据库不一致：留痕而不是静默 500，便于运维定位丢文件
        logger.error("image_object_missing", object_key=key, message_id=str(message.id))
        raise AppError(
            code="IMAGE_NOT_FOUND", message="图片已不存在，请重新发送", status_code=404
        ) from exc
    return data, str(attachment.get("mime_type") or "image/png")


__all__ = [
    "ATTACHMENT_TYPE_IMAGE",
    "MESSAGE_TYPE_IMAGE",
    "build_object_key",
    "detect_image",
    "find_image_attachment",
    "image_url",
    "read_image",
    "store_image",
]
