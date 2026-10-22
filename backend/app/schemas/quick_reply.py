"""快捷回复（坐席常用话术）相关模型。"""

import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.support import QUICK_REPLY_CATEGORIES

CATEGORY_LABELS = {
    "greeting": "问候语",
    "faq": "常见问题",
    "closing": "结束语",
    "other": "其他",
}


class QuickReplyCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=2000)
    category: str = Field(default="other", pattern=f"^({'|'.join(QUICK_REPLY_CATEGORIES)})$")
    sort_order: int = Field(default=0, ge=0, le=9999)


class QuickReplyUpdate(BaseModel):
    """局部更新：只提交要改的字段。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str | None = Field(default=None, min_length=1, max_length=100)
    content: str | None = Field(default=None, min_length=1, max_length=2000)
    category: str | None = Field(default=None, pattern=f"^({'|'.join(QUICK_REPLY_CATEGORIES)})$")
    sort_order: int | None = Field(default=None, ge=0, le=9999)


class QuickReplyInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    content: str
    category: str
    sort_order: int
    created_at: str
