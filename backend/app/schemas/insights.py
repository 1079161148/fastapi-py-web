"""AI 效果洞察（未命中问题看板）相关模型。"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class UnansweredQuestion(BaseModel):
    """一条未命中问题（按提问原文聚合）。"""

    question: str
    ask_count: int
    last_asked_at: datetime
    # 最近一次提问所在会话：坐席可以点进去看上下文
    conversation_id: uuid.UUID


class InsightOverview(BaseModel):
    """AI 效果概览 + top 未命中问题。

    口径与数据报表一致：近 30 天窗口。
    """

    window_days: int
    # AI 生成回答的条数（不含坐席回复与转人工固定文案）
    total_answers: int
    # 其中引用了知识库的条数
    cited_answers: int
    # cited / total；total=0 时为 None（"还没有数据"比 0% 更不容易误读）
    hit_rate: float | None
    # 窗口内未命中（AI 回答未引用知识）的提问总次数
    unanswered_total: int
    # 按提问原文聚合后的 top N
    unanswered: list[UnansweredQuestion] = Field(default_factory=list)
    # 已完成解析的知识文档数：0 时前端要提示"先上传知识库"
    knowledge_documents: int
