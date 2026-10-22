"""知识库请求/响应模型（设计文档第 7 章）。"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeBaseCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    # 只允许已实现的两种策略：暴露不存在的选项比不暴露更糟
    chunk_strategy: str = Field(default="by_heading", pattern="^(by_heading|fixed_window)$")


class KnowledgeBaseInfo(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    status: str
    chunk_strategy: str
    document_count: int = 0
    created_at: datetime


class DocumentInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kb_id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    status: str
    error_msg: str
    chunk_count: int
    created_at: datetime
    updated_at: datetime


class DocumentListResponse(BaseModel):
    items: list[DocumentInfo]


class SourceRef(BaseModel):
    """回答引用的知识片段：SSE sources 事件与 messages.sources 共用同一结构。"""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_name: str
    content: str
    score: float


class SearchHit(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    kb_id: uuid.UUID
    document_name: str
    content: str
    score: float
    vector_score: float | None = None
    keyword_score: float | None = None
    # 是否经过 rerank 重排（便于判断"配了模型但没生效"）
    reranked: bool = False


class SearchResponse(BaseModel):
    items: list[SearchHit]
    # degraded=true 表示"本该检索但失败了"（例如嵌入模型未配置）
    degraded: bool = False
    reason: str = ""
