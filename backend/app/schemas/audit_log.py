"""审计日志相关模型。"""

import uuid

from pydantic import BaseModel, ConfigDict


class AuditLogInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    actor_id: uuid.UUID | None
    actor_name: str
    action: str
    target_type: str
    target_id: str
    detail: dict
    created_at: str
