"""模型用量流水（设计文档 6 / 10）。

一张**追加式**表：一次模型调用一行，只插入不改写（租户删除时级联删）。
用途有两个，缺一不可：

1. 成本核算：按天/模型/类型聚合（设计文档 10「每次调用记 tokens，看板按天/模型聚合」）
2. 配额校验：`max_tokens_per_day` 依赖当日 token 汇总，不能靠内存计数
   （多 uvicorn worker / 多 API 实例下内存计数必然不准）

为什么额外有 `units`：ASR 按时长计费、TTS 按字符计费，token 字段对它们无意义。
`units` 承载该计费口径的原始量，`cost` 存按当时价格算出的金额——价格后续调整
不影响历史账目（不改写历史，只对新增生效）。

**不存任何原文**：设计文档 8 要求 llm_usage_logs 不含会话内容，
这里只有模型名、计数与金额，连 prompt 摘要都不留。
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# 调用类型：与 services/usage.py 的价格口径一一对应
KIND_DIALOGUE = "dialogue"
KIND_EMBEDDING = "embedding"
KIND_RERANK = "rerank"
KIND_ASR = "asr"
KIND_TTS = "tts"

USAGE_KINDS = (KIND_DIALOGUE, KIND_EMBEDDING, KIND_RERANK, KIND_ASR, KIND_TTS)


class LLMUsageLog(Base):
    __tablename__ = "llm_usage_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 会话被删除时只置空：成本记录属于账目，不随会话清理消失
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    # 模型标识：网关返回的 model 名，未提供时记配置里的模型名
    model: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 非 token 计费口径的原始量：音频秒数 / 合成字符数 / 批处理条数
    units: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False, default=Decimal("0"))
    # 金额精度到 1e-6：单次调用可能远小于 0.01
    cost: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=Decimal("0"))
    # 不继承 TimestampMixin：流水没有"更新时间"，updated_at 是纯粹的噪音列
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = [
    "KIND_ASR",
    "KIND_DIALOGUE",
    "KIND_EMBEDDING",
    "KIND_RERANK",
    "KIND_TTS",
    "USAGE_KINDS",
    "LLMUsageLog",
]
