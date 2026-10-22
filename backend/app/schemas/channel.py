"""渠道请求/响应模型（设计文档 7 / 9.1）。"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# 与 db/models/channel.py 的 ALL_CHANNELS 保持一致
CHANNEL_PATTERN = "^(web|email|wechat|dingtalk)$"


class ChannelConfigUpdate(BaseModel):
    """渠道配置更新：只改显式传入的字段，None 表示保持不变。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    enabled: bool | None = None
    # 平台账号标识：微信 gh_xxx / 钉钉 robotCode / 邮件收件地址
    external_account_id: str | None = Field(default=None, max_length=200)
    credentials: dict[str, Any] | None = None
    rate_limit: int | None = Field(default=None, ge=1, le=6000)
    webhook_secret: str | None = Field(default=None, max_length=200)


class ChannelConfigInfo(BaseModel):
    channel: str
    enabled: bool
    external_account_id: str
    credentials: dict[str, Any]
    rate_limit: int
    # 环境变量凭证是否齐备：false 时该渠道实际不可用（配置了也没用）
    configured: bool
    missing: list[str] = Field(default_factory=list)
    webhook_path: str

    @classmethod
    def empty(cls, channel: str) -> "ChannelConfigInfo":
        """尚未落库的渠道也返回占位行：前端能一次看到全部渠道的可用状态。"""
        return cls(
            channel=channel,
            enabled=False,
            external_account_id="",
            credentials={},
            rate_limit=60,
            configured=False,
            missing=[],
            webhook_path=f"/api/v1/channels/{channel}/webhook",
        )


class ChannelMessageInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel: str
    external_msg_id: str
    conversation_id: uuid.UUID | None
    direction: str
    status: str
    external_user_id: str
    content: str
    error_msg: str
    created_at: datetime


class ChannelMessageListResponse(BaseModel):
    items: list[ChannelMessageInfo]


class InboundResult(BaseModel):
    """webhook 的 JSON 响应（微信/钉钉使用各自平台的 ack 格式，不走此模型）。"""

    status: str
    conversation_id: uuid.UUID | None = None
    duplicate: bool = False
    reason: str = ""
