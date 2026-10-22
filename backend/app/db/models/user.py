"""用户与刷新令牌（设计文档 6/8）。

users 为全局身份表，tenant_id 记录其主租户；
跨租户成员关系由 tenant_members 表达（设计文档 9.2）。

登录查询不走 RLS：users 表启用行级安全，认证引导查询
通过 SECURITY DEFINER 函数 auth_lookup_user 执行（见首个迁移），
避免"未设租户上下文就查不到用户"与"放开策略破坏隔离"的两难。
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.db.models.tenant import TenantMember


class User(Base, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (
        # 用户名**租户内唯一**：不同租户可以有同名的张三。
        # 部分索引：空用户名不占坑（存量账号与自动创建的账号都是 NULL）。
        # 与迁移 0008 的定义保持一致，避免 autogenerate 反复提出删除重建
        Index(
            "uq_users_tenant_username",
            "tenant_id",
            "username",
            unique=True,
            postgresql_where=text("username IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    # 可选登录标识：租户内唯一、存储为小写。为空表示该账号只能用邮箱登录。
    # 因为只在租户内唯一，用户名登录必须同时提供租户标识（slug）才能定位到人
    username: Mapped[str | None] = mapped_column(String(32), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    # 全局角色：customer / agent / admin（超管）；租户内角色看 tenant_members.role
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="customer")
    # 主租户：注册时创建/加入的第一个租户
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    memberships: Mapped[list["TenantMember"]] = relationship(back_populates="user")
    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class RefreshToken(Base, TimestampMixin):
    """刷新令牌：可吊销（设计文档 8：refresh 长但可吊销）。

    只存 token 标识（jti）与哈希，不存明文；吊销即改 status。
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    # JWT ID：与刷新令牌的 jti 声明对应
    jti: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="refresh_tokens")
