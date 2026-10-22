"""异步入库任务（设计文档 5.2）。

流程：读原件 → 解析 → 分块 → 向量化 → 重建 chunks → 回写状态。

为什么必须异步：解析 PDF/DOCX 与批量 embedding 是秒级到分钟级操作，
同步放在 HTTP 请求里会超时并占满 worker 进程（设计文档 5.2 的原始动机）。

幂等性：每次处理先删除该文档的旧 chunks 再写入，因此"失败后重试"
不会产生重复片段；重试入口见 POST /knowledge/documents/{id}/reprocess。
"""

import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import delete

from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.core.tenant import set_current_tenant
from app.db.models.knowledge import (
    DOC_STATUS_DONE,
    DOC_STATUS_FAILED,
    DOC_STATUS_PROCESSING,
    Chunk,
    Document,
    KnowledgeBase,
)
from app.db.models.usage import KIND_EMBEDDING
from app.db.session import async_session_factory
from app.llm.factory import get_embedding_client
from app.services import usage as usage_service
from app.services.ingestion import build_chunks, tokenize_for_search
from app.storage import get_storage

logger = get_logger("worker")


async def process_document(ctx: dict[str, Any], document_id: str, tenant_id: str) -> None:
    """解析并向量化一份文档。入参用字符串：arq 的 job 参数必须可 JSON 序列化。"""
    tenant_uuid = uuid.UUID(tenant_id)
    doc_uuid = uuid.UUID(document_id)
    set_current_tenant(tenant_uuid)

    try:
        async with async_session_factory() as session:
            document = await session.get(Document, doc_uuid)
            if document is None or document.tenant_id != tenant_uuid:
                logger.warning("document_missing", document_id=document_id)
                return

            settings = get_settings()
            base = await session.get(KnowledgeBase, document.kb_id)
            strategy = base.chunk_strategy if base is not None else settings.chunk_strategy

            document.status = DOC_STATUS_PROCESSING
            document.error_msg = ""
            await session.commit()

            data = await get_storage().read(document.object_key)
            suffix = Path(document.filename).suffix.lower()
            chunks = build_chunks(data, suffix, strategy=strategy)

            # embedding 放在删除旧数据之前：模型不可用时保留上一版可用索引
            embedder = get_embedding_client()
            vectors = await embedder.embed([chunk.content for chunk in chunks])
            # 入库的嵌入是成本大头（一次几千条），必须单独记账
            await usage_service.record(
                tenant_id=tenant_uuid,
                kind=KIND_EMBEDDING,
                model=settings.ai_embedding_model,
                units=sum(len(chunk.content) for chunk in chunks),
            )

            await session.execute(
                delete(Chunk).where(Chunk.tenant_id == tenant_uuid, Chunk.document_id == doc_uuid)
            )
            session.add_all(
                [
                    Chunk(
                        tenant_id=tenant_uuid,
                        kb_id=document.kb_id,
                        document_id=doc_uuid,
                        chunk_index=chunk.index,
                        content=chunk.content,
                        search_text=tokenize_for_search(chunk.content),
                        embedding=vector,
                        chunk_strategy=strategy,
                        chunk_metadata=chunk.metadata,
                    )
                    for chunk, vector in zip(chunks, vectors, strict=True)
                ]
            )
            document.chunk_count = len(chunks)
            document.status = DOC_STATUS_DONE
            await session.commit()

        logger.info("document_processed", document_id=document_id, chunks=len(chunks))
    except AppError as exc:
        await _record_failure(doc_uuid, tenant_uuid, exc.message)
        logger.warning(
            "document_processing_failed",
            document_id=document_id,
            code=exc.code,
            message=exc.message,
        )
        raise
    except Exception as exc:  # noqa: BLE001 - worker 必须记录任何异常并回写状态
        await _record_failure(doc_uuid, tenant_uuid, f"解析失败：{type(exc).__name__}: {exc}")
        logger.exception("document_processing_error", document_id=document_id)
        raise


async def _record_failure(document_id: uuid.UUID, tenant_id: uuid.UUID, message: str) -> None:
    """用新会话回写失败状态：原会话可能因异常处于不可用事务状态。"""
    set_current_tenant(tenant_id)
    try:
        async with async_session_factory() as session:
            document = await session.get(Document, document_id)
            if document is None:
                return
            document.status = DOC_STATUS_FAILED
            document.error_msg = message[:500]
            await session.commit()
    except Exception:  # noqa: BLE001 - 回写失败不能掩盖原始异常，但必须留痕
        logger.exception("document_failure_record_failed", document_id=str(document_id))
