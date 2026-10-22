"""用量看板响应模型（M7，设计文档 10）。

金额与计量单位用 float 暴露：看板是展示用途，数据库里的 Numeric 才是账目。
若将来要做账单导出，应另出 CSV/对账接口直接读原始精度，而不是放宽这里。
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class UsageBucketInfo(BaseModel):
    """一个聚合桶；未参与分组的维度为空串。"""

    day: str = ""
    kind: str = ""
    model: str = ""
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    units: float = 0.0
    cost: float = 0.0


class UsageSummary(BaseModel):
    start: datetime
    end: datetime
    group_by: str
    total: UsageBucketInfo
    buckets: list[UsageBucketInfo]


class UsageQuotaInfo(BaseModel):
    """当日 token 配额使用情况：limit 为 0 表示未设置上限。"""

    tokens_limit: int = 0
    tokens_used_today: int = 0
    exceeded: bool = False


class UsageOverview(BaseModel):
    tenant_id: uuid.UUID
    today: UsageBucketInfo
    last_7_days: UsageBucketInfo
    last_30_days: UsageBucketInfo
    quota: UsageQuotaInfo
    # 近 7 天按天明细（看板折线图直接消费）
    daily: list[UsageBucketInfo] = Field(default_factory=list)


__all__ = ["UsageBucketInfo", "UsageOverview", "UsageQuotaInfo", "UsageSummary"]
