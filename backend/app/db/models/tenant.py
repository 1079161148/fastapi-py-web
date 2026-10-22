"""租户与租户成员（设计文档 6/9.2）。

RLS 说明：tenants / tenant_members 均启用行级安全，
未设置 app.tenant_id 时查询返回空行（fail-closed）；
tenants 的 WITH CHECK 放开 INSERT，否则注册流程无法创建首个租户。
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.db.models.user import User


class Tenant(Base, TimestampMixin):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # slug 用于子域名租户解析（设计文档 7 章），全局唯一
    slug: Mapped[str] = mapped_column(String(63), nullable=False, unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    # 配额：消息数 / 并发 / 存储 / Token 预算（设计文档 9.2）
    quota: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    members: Mapped[list["TenantMember"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )


class TenantMember(Base, TimestampMixin):
    __tablename__ = "tenant_members"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    # owner / admin / agent / customer
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="customer")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=None)

    tenant: Mapped[Tenant] = relationship(back_populates="members")
    user: Mapped["User"] = relationship(back_populates="memberships")
