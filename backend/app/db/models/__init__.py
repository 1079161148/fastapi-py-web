"""ORM 模型。

每个模型文件对应一个领域
（tenant / user / conversation / knowledge / channel / handoff / usage / leave_message）。
模型必须在此导入，Alembic autogenerate 与 metadata 才看得到。
"""

from app.db.models.channel import ChannelConfig, ChannelIdentity, ChannelMessage
from app.db.models.conversation import Conversation, GuestSession, Message
from app.db.models.handoff import HandoffTicket
from app.db.models.knowledge import Chunk, Document, KnowledgeBase
from app.db.models.leave_message import LeaveMessage
from app.db.models.tenant import Tenant, TenantMember
from app.db.models.usage import LLMUsageLog
from app.db.models.user import User

__all__ = [
    "ChannelConfig",
    "ChannelIdentity",
    "ChannelMessage",
    "Chunk",
    "Conversation",
    "Document",
    "GuestSession",
    "HandoffTicket",
    "KnowledgeBase",
    "LLMUsageLog",
    "LeaveMessage",
    "Message",
    "Tenant",
    "TenantMember",
    "User",
]
