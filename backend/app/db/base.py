"""SQLAlchemy 声明式基座与命名约定。

命名约定让 Alembic 自动生成的约束名稳定可读，避免升级时反复重命名。
"""

from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 约束命名约定：%(table_name)s_%(column_0_name)s_%(constraint_name)s 等
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """创建/更新时间。所有业务表统一携带，便于审计与数据清理。

    必须声明 timezone=True：迁移里建的是 TIMESTAMP WITH TIME ZONE，
    模型若映射为 naive，显式赋值（如 message.created_at = datetime.now(UTC)）
    会被 asyncpg 以"aware 与 naive 不能相减"拒绝。
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
