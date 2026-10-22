"""LLM 适配层接口（设计文档 4.2：唯一允许触碰模型 SDK 的目录）。

上层（services/chat.py）只依赖本文件的 Protocol 与数据结构，
换供应商 / 换协议只改本目录实现，编排与业务不受影响。

与设计文档 4.2 的签名差异：`chat_stream` 产出 StreamChunk 而非裸 str。
原因是成本核算需要 token 用量（设计文档 5.1/10），而裸 str 无处承载
usage；用不可变 dataclass 显式传递，避免在客户端上挂可变状态。
"""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Protocol

from app.core.exceptions import AppError

# 对话角色：与 OpenAI 兼容协议一致
ROLE_SYSTEM = "system"
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class ImagePart:
    """随消息发送的一张图片。

    用 base64 直传而不是给 URL：模型网关在云端，够不到本机 127.0.0.1 的
    签名地址；把图塞进请求体是唯一不依赖网络可达性的做法。
    """

    mime_type: str
    data_b64: str


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: str
    content: str
    # 非空表示这是多模态消息（content 会转成 text + image_url 数组）
    images: tuple[ImagePart, ...] = ()


@dataclass(frozen=True, slots=True)
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True, slots=True)
class StreamChunk:
    """一个流式增量：delta 为文本增量，usage 仅在末帧出现（若供应商支持）。"""

    delta: str = ""
    usage: TokenUsage | None = None


class LLMNotConfiguredError(AppError):
    """模型未配置：必须显式失败（设计文档 10 降级要求：不静默失败）。

    expose=True：这条信息对使用者是可操作的（该去补哪个环境变量），
    被统一 5xx 兜底替换成"服务内部错误"反而无从排查。
    """

    def __init__(self, missing: Sequence[str]) -> None:
        super().__init__(
            code="LLM_NOT_CONFIGURED",
            message=(
                "AI 模型未配置：请在 backend/.env 中设置 "
                + " / ".join(missing)
                + " 后重启服务"
            ),
            status_code=503,
            expose=True,
        )
        self.missing = list(missing)


class LLMUpstreamError(AppError):
    """上游模型服务错误（含超时、非 2xx、协议异常）。

    expose=True 但 detail 传 None：上游原始报文可能含网关内部信息，
    只写日志，不回给前端。
    """

    def __init__(self, message: str) -> None:
        super().__init__(
            code="LLM_UPSTREAM_ERROR",
            message=message,
            status_code=502,
            expose=True,
        )


class LLMClient(Protocol):
    def chat_stream(
        self, messages: Sequence[ChatMessage], *, temperature: float = 0.7
    ) -> AsyncIterator[StreamChunk]:
        """流式生成回复。实现必须是异步生成器函数或返回异步迭代器。"""
        ...


@dataclass(frozen=True, slots=True)
class RerankHit:
    """重排结果：index 为原始文档下标，score 为该模型给出的相关性分数。"""

    index: int
    score: float


class EmbeddingClient(Protocol):
    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """批量向量化，返回顺序与输入一致。"""
        ...


class RerankClient(Protocol):
    async def rerank(
        self, query: str, documents: Sequence[str], *, top_n: int
    ) -> list[RerankHit]:
        """按相关性重排候选文档，返回降序结果（至少含 top_n 条，不足则全给）。"""
        ...
