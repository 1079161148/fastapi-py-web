"""离线留言的请求/响应模型（M8）。"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class LeaveMessageCreate(BaseModel):
    """用户提交留言。

    contact 必填且给下限：留个"1"或者空串，坐席根本没法回，等于没留。
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(default="", max_length=100)
    contact: str = Field(min_length=3, max_length=200)
    content: str = Field(min_length=1, max_length=2000)


class LeaveMessageInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    conversation_id: uuid.UUID | None
    owner_type: str
    name: str
    contact: str
    content: str
    status: str
    handled_by: uuid.UUID | None
    handled_at: datetime | None
    created_at: datetime


class LeaveMessageListResponse(BaseModel):
    items: list[LeaveMessageInfo]
    next_cursor: str | None = None
