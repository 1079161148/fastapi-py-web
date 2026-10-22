"""知识库路由（设计文档 7 / 9.4）。

权限：知识库是租户管理动作，写操作要求 owner/admin/agent（`require_kb_manager`）；
读操作（知识库/文档列表、检索调试）对租户内成员开放，坐席也要能查资料。

上传语义（设计文档 5.2）：接口只做校验 + 落盘 + 入队，秒级返回 202；
解析/分块/向量化在 arq worker 里完成，前端轮询文档状态。
"""

import uuid
from typing import Annotated

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    get_current_tenant_member,
    provide_job_queue,
    provide_required_embedding_client,
    provide_reranker,
    require_kb_manager,
)
from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.core.tenant import require_current_tenant_id
from app.db.models.tenant import TenantMember
from app.db.session import get_session
from app.llm.base import EmbeddingClient, RerankClient
from app.schemas.knowledge import (
    DocumentInfo,
    DocumentListResponse,
    KnowledgeBaseCreate,
    KnowledgeBaseInfo,
    SearchHit,
    SearchResponse,
)
from app.services import knowledge as knowledge_service
from app.services import retrieval as retrieval_service
from app.storage import get_storage

router = APIRouter(prefix="/knowledge", tags=["knowledge"])

settings = get_settings()
logger = get_logger("api.knowledge")


@router.post("/bases", response_model=KnowledgeBaseInfo, status_code=201)
async def create_base(
    payload: KnowledgeBaseCreate,
    member: TenantMember = Depends(require_kb_manager),
    session: AsyncSession = Depends(get_session),
) -> KnowledgeBaseInfo:
    tenant_id = require_current_tenant_id()
    base = await knowledge_service.create_base(
        session,
        tenant_id=tenant_id,
        name=payload.name,
        description=payload.description,
        chunk_strategy=payload.chunk_strategy,
    )
    return KnowledgeBaseInfo(
        id=base.id,
        name=base.name,
        description=base.description,
        status=base.status,
        chunk_strategy=base.chunk_strategy,
        document_count=0,
        created_at=base.created_at,
    )


@router.get("/bases", response_model=list[KnowledgeBaseInfo])
async def list_bases(
    member: TenantMember = Depends(get_current_tenant_member),
    session: AsyncSession = Depends(get_session),
) -> list[KnowledgeBaseInfo]:
    tenant_id = require_current_tenant_id()
    rows = await knowledge_service.list_bases(session, tenant_id=tenant_id)
    return [
        KnowledgeBaseInfo(
            id=row.base.id,
            name=row.base.name,
            description=row.base.description,
            status=row.base.status,
            chunk_strategy=row.base.chunk_strategy,
            document_count=row.document_count,
            created_at=row.base.created_at,
        )
        for row in rows
    ]


@router.get("/documents", response_model=DocumentListResponse)
async def list_documents(
    kb_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=knowledge_service.DEFAULT_DOCUMENT_LIMIT, ge=1, le=200),
    member: TenantMember = Depends(get_current_tenant_member),
    session: AsyncSession = Depends(get_session),
) -> DocumentListResponse:
    tenant_id = require_current_tenant_id()
    documents = await knowledge_service.list_documents(
        session, tenant_id=tenant_id, kb_id=kb_id, limit=limit
    )
    return DocumentListResponse(
        items=[DocumentInfo.model_validate(document) for document in documents]
    )


@router.get("/documents/{document_id}", response_model=DocumentInfo)
async def get_document(
    document_id: uuid.UUID,
    member: TenantMember = Depends(get_current_tenant_member),
    session: AsyncSession = Depends(get_session),
) -> DocumentInfo:
    tenant_id = require_current_tenant_id()
    document = await knowledge_service.get_document(
        session, tenant_id=tenant_id, document_id=document_id
    )
    return DocumentInfo.model_validate(document)


@router.post("/documents", response_model=DocumentInfo, status_code=202)
async def upload_document(
    kb_id: Annotated[uuid.UUID, Form()],
    file: Annotated[UploadFile, File()],
    member: TenantMember = Depends(require_kb_manager),
    session: AsyncSession = Depends(get_session),
    queue: ArqRedis = Depends(provide_job_queue),
) -> DocumentInfo:
    """上传原件并入队解析。

    Redis 不可用时依赖层已提前 503，不会产生"文件落盘但永不处理"的孤儿记录。
    """
    tenant_id = require_current_tenant_id()
    await knowledge_service.get_base(session, tenant_id=tenant_id, kb_id=kb_id)

    max_bytes = settings.upload_max_mb * 1024 * 1024
    data = await _read_upload(file, max_bytes)
    filename = (file.filename or "").strip()
    suffix = knowledge_service.validate_upload(filename, len(data))

    document_id = uuid.uuid4()
    object_key = knowledge_service.build_object_key(
        tenant_id=tenant_id, kb_id=kb_id, document_id=document_id, suffix=suffix
    )
    await get_storage().save(object_key, data)

    document = await knowledge_service.create_document(
        session,
        document_id=document_id,
        tenant_id=tenant_id,
        kb_id=kb_id,
        filename=filename,
        object_key=object_key,
        content_type=file.content_type or "",
        size_bytes=len(data),
    )

    try:
        await queue.enqueue_job(
            "process_document",
            str(document.id),
            str(tenant_id),
            # 随机后缀避免 arq 以 job_id 去重把重复上传/重试挡掉
            _job_id=f"doc-{document.id}-{uuid.uuid4().hex[:8]}",
        )
    except Exception as exc:  # noqa: BLE001 - 入队失败要落 failed 并给出可操作错误
        logger.warning("document_enqueue_failed", document_id=str(document.id), error=str(exc))
        await knowledge_service.mark_failed(
            session,
            tenant_id=tenant_id,
            document_id=document.id,
            message="任务入队失败：请检查 Redis 与 worker 是否运行",
        )
        raise AppError(
            code="QUEUE_UNAVAILABLE",
            message="任务入队失败：请确认 Redis 已启动、arq worker 正在运行",
            status_code=503,
            expose=True,
        ) from exc

    return DocumentInfo.model_validate(document)


@router.post("/documents/{document_id}/reprocess", response_model=DocumentInfo, status_code=202)
async def reprocess_document(
    document_id: uuid.UUID,
    member: TenantMember = Depends(require_kb_manager),
    session: AsyncSession = Depends(get_session),
    queue: ArqRedis = Depends(provide_job_queue),
) -> DocumentInfo:
    tenant_id = require_current_tenant_id()
    document = await knowledge_service.reset_for_reprocess(
        session, tenant_id=tenant_id, document_id=document_id
    )
    await queue.enqueue_job(
        "process_document",
        str(document.id),
        str(tenant_id),
        _job_id=f"doc-{document.id}-{uuid.uuid4().hex[:8]}",
    )
    return DocumentInfo.model_validate(document)


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(
    document_id: uuid.UUID,
    member: TenantMember = Depends(require_kb_manager),
    session: AsyncSession = Depends(get_session),
) -> None:
    tenant_id = require_current_tenant_id()
    object_key = await knowledge_service.delete_document(
        session, tenant_id=tenant_id, document_id=document_id
    )
    try:
        await get_storage().delete(object_key)
    except Exception:  # noqa: BLE001 - 数据已删，文件残留不应让接口失败；必须留痕
        logger.warning("document_file_delete_failed", object_key=object_key)


@router.get("/search", response_model=SearchResponse)
async def search(
    q: str = Query(min_length=1, max_length=500),
    kb_id: uuid.UUID | None = Query(default=None),
    top_k: int = Query(default=settings.retrieval_top_k, ge=1, le=20),
    member: TenantMember = Depends(get_current_tenant_member),
    session: AsyncSession = Depends(get_session),
    embedder: EmbeddingClient = Depends(provide_required_embedding_client),
    reranker: RerankClient | None = Depends(provide_reranker),
) -> SearchResponse:
    """检索调试接口：用于验证分块与召回质量（也是评估脚本的底层入口）。"""
    tenant_id = require_current_tenant_id()
    kb_ids = await retrieval_service.active_kb_ids(session, tenant_id)
    if kb_id is not None:
        await knowledge_service.get_base(session, tenant_id=tenant_id, kb_id=kb_id)
        kb_ids = [kb_id]

    result = await retrieval_service.search(
        session,
        tenant_id=tenant_id,
        kb_ids=kb_ids,
        query=q,
        embedder=embedder,
        reranker=reranker,
        top_k=top_k,
    )
    return SearchResponse(
        degraded=result.degraded,
        reason=result.reason,
        items=[
            SearchHit(
                chunk_id=item.chunk_id,
                document_id=item.document_id,
                kb_id=item.kb_id,
                document_name=item.document_name,
                content=item.content,
                score=item.score,
                vector_score=item.vector_score,
                keyword_score=item.keyword_score,
                reranked=item.reranked,
            )
            for item in result.items
        ],
    )


async def _read_upload(file: UploadFile, max_bytes: int) -> bytes:
    """多读 1 字节用于判断超限：不用 content-length（客户端可伪造）。"""
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise AppError(
            code="FILE_TOO_LARGE",
            message=f"文件超过 {settings.upload_max_mb}MB 上限，请拆分后再上传",
            status_code=413,
        )
    return data
