"""LLM 适配层（设计文档 4.2）。"""

from app.llm.base import (
    ChatMessage,
    EmbeddingClient,
    LLMClient,
    LLMNotConfiguredError,
    LLMUpstreamError,
    RerankClient,
    RerankHit,
    StreamChunk,
    TokenUsage,
)
from app.llm.factory import get_embedding_client, get_llm_client, get_rerank_client

__all__ = [
    "ChatMessage",
    "EmbeddingClient",
    "LLMClient",
    "LLMNotConfiguredError",
    "LLMUpstreamError",
    "RerankClient",
    "RerankHit",
    "StreamChunk",
    "TokenUsage",
    "get_embedding_client",
    "get_llm_client",
    "get_rerank_client",
]
