"""知识库与文档服务（设计文档 5.2 / 9.4）。

职责边界：
- 接口层只做上传落盘与入队（秒级返回），解析/分块/向量化全在 worker；
- 本模块只碰数据库与校验，不碰 AI 客户端与队列（可测试性好）；
- 删除文档时返回 object_key，由接口层删文件（事务与外部 IO 分离）。
"""

import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AppError, NotFoundError
from app.db.models.knowledge import (
    CHUNK_STRATEGY_BY_HEADING,
    CHUNK_STRATEGY_FIXED_WINDOW,
    DOC_STATUS_UPLOADED,
    Document,
    KnowledgeBase,
)
from app.services.ingestion import SUPPORTED_SUFFIXES

MAX_FILENAME_LENGTH = 255
DEFAULT_DOCUMENT_LIMIT = 50


@dataclass(frozen=True, slots=True)
class BaseWithCount:
    base: KnowledgeBase
    document_count: int


async def create_base(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    name: str,
    description: str = "",
    chunk_strategy: str = CHUNK_STRATEGY_BY_HEADING,
) -> KnowledgeBase:
    if chunk_strategy not in {CHUNK_STRATEGY_BY_HEADING, CHUNK_STRATEGY_FIXED_WINDOW}:
        raise AppError(
            code="INVALID_CHUNK_STRATEGY",
            message="分块策略只支持 by_heading / fixed_window",
            status_code=400,
        )
    base = KnowledgeBase(
        tenant_id=tenant_id,
        name=name,
        description=description,
        chunk_strategy=chunk_strategy,
    )
    session.add(base)
    await session.commit()
    await session.refresh(base)
    return base


async def list_bases(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[BaseWithCount]:
    # 用左连接聚合一次取回文档数，避免每个知识库再查一次（N+1）
    stmt = (
        select(KnowledgeBase, func.count(Document.id))
        .outerjoin(
            Document,
            (Document.kb_id == KnowledgeBase.id) & (Document.tenant_id == tenant_id),
        )
        .where(KnowledgeBase.tenant_id == tenant_id)
        .group_by(KnowledgeBase.id)
        .order_by(KnowledgeBase.created_at.desc())
    )
    rows = (await session.execute(stmt)).all()
    return [BaseWithCount(base=base, document_count=int(count)) for base, count in rows]


async def get_base(
    session: AsyncSession, *, tenant_id: uuid.UUID, kb_id: uuid.UUID
) -> KnowledgeBase:
    stmt = select(KnowledgeBase).where(
        KnowledgeBase.id == kb_id, KnowledgeBase.tenant_id == tenant_id
    )
    base = (await session.execute(stmt)).scalar_one_or_none()
    if base is None:
        raise NotFoundError("知识库")
    return base


async def list_documents(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    kb_id: uuid.UUID | None = None,
    limit: int = DEFAULT_DOCUMENT_LIMIT,
) -> list[Document]:
    stmt = select(Document).where(Document.tenant_id == tenant_id)
    if kb_id is not None:
        stmt = stmt.where(Document.kb_id == kb_id)
    stmt = stmt.order_by(Document.created_at.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def get_document(
    session: AsyncSession, *, tenant_id: uuid.UUID, document_id: uuid.UUID
) -> Document:
    stmt = select(Document).where(
        Document.id == document_id, Document.tenant_id == tenant_id
    )
    document = (await session.execute(stmt)).scalar_one_or_none()
    if document is None:
        raise NotFoundError("文档")
    return document


async def create_document(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    tenant_id: uuid.UUID,
    kb_id: uuid.UUID,
    filename: str,
    object_key: str,
    content_type: str,
    size_bytes: int,
) -> Document:
    """document_id 由调用方生成：对象存储 key 里要用同一个 id，先落盘再写库。"""
    document = Document(
        id=document_id,
        tenant_id=tenant_id,
        kb_id=kb_id,
        filename=filename,
        object_key=object_key,
        content_type=content_type,
        size_bytes=size_bytes,
        status=DOC_STATUS_UPLOADED,
    )
    session.add(document)
    await session.commit()
    await session.refresh(document)
    return document


async def mark_failed(
    session: AsyncSession, *, tenant_id: uuid.UUID, document_id: uuid.UUID, message: str
) -> None:
    document = await get_document(session, tenant_id=tenant_id, document_id=document_id)
    document.error_msg = message[:500]
    # 状态由 worker 写；接口层入队失败时也要落 failed，避免永远停在 uploaded
    document.status = "failed"
    await session.commit()


async def reset_for_reprocess(
    session: AsyncSession, *, tenant_id: uuid.UUID, document_id: uuid.UUID
) -> Document:
    document = await get_document(session, tenant_id=tenant_id, document_id=document_id)
    document.status = DOC_STATUS_UPLOADED
    document.error_msg = ""
    await session.commit()
    await session.refresh(document)
    return document


async def delete_document(
    session: AsyncSession, *, tenant_id: uuid.UUID, document_id: uuid.UUID
) -> str:
    """删除文档（chunks 由外键 CASCADE 清理），返回待删除的对象存储 key。"""
    document = await get_document(session, tenant_id=tenant_id, document_id=document_id)
    object_key = document.object_key
    await session.delete(document)
    await session.commit()
    return object_key


def validate_upload(filename: str | None, size_bytes: int) -> str:
    """后缀白名单 + 大小限制，返回小写后缀。"""
    settings = get_settings()
    name = (filename or "").strip()
    if not name:
        raise AppError(code="INVALID_FILENAME", message="文件名不能为空", status_code=400)
    if len(name) > MAX_FILENAME_LENGTH:
        raise AppError(
            code="INVALID_FILENAME",
            message=f"文件名过长（上限 {MAX_FILENAME_LENGTH} 字符）",
            status_code=400,
        )

    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        supported = " / ".join(sorted(SUPPORTED_SUFFIXES))
        raise AppError(
            code="UNSUPPORTED_FILE_TYPE",
            message=f"不支持的文件类型 {suffix or '(无扩展名)'}，支持：{supported}",
            status_code=400,
        )

    if size_bytes <= 0:
        raise AppError(code="EMPTY_FILE", message="文件内容为空", status_code=400)

    limit_bytes = settings.upload_max_mb * 1024 * 1024
    if size_bytes > limit_bytes:
        raise AppError(
            code="FILE_TOO_LARGE",
            message=f"文件超过 {settings.upload_max_mb}MB 上限，请拆分后再上传",
            status_code=413,
        )
    return suffix


def build_object_key(
    *, tenant_id: uuid.UUID, kb_id: uuid.UUID, document_id: uuid.UUID, suffix: str
) -> str:
    """服务端生成 key（不含用户文件名），天然规避路径穿越与重名覆盖。"""
    return f"tenant/{tenant_id}/kb/{kb_id}/doc/{document_id}{suffix}"
