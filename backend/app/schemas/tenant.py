"""租户相关模型。"""

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.auth import USERNAME_PATTERN

# 租户内角色（设计文档 6：owner/admin/agent/customer）
TENANT_ROLES = ("owner", "admin", "agent", "customer")
# 新建成员不允许直接给 owner：owner 唯一性由"最后一个 owner 不可降级/移除"守护，
# 允许直接创建 owner 会让这条约束在没有警告的情况下被绕过
CREATABLE_ROLES = ("admin", "agent", "customer")


class TenantSettings(BaseModel):
    """租户级运营配置（存 tenants.settings JSONB）。

    缺省值即"当前硬编码行为"：不填任何字段时系统与改造前一致。
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    # 访客打开会话时展示的欢迎语；空串 = 不展示
    welcome_message: str = Field(default="", max_length=500)
    # 命中其中任意词即自动转人工（用户输入包含即算命中）
    handoff_triggers: list[str] = Field(default_factory=list, max_length=20)
    # 非工作时间策略：leave_message=引导留言，continue_ai=继续由 AI 回答
    after_hours_action: str = Field(
        default="leave_message", pattern="^(leave_message|continue_ai)$"
    )
    # 工作时间（按天判断）：不在区间内视为非工作时间
    work_hours_start: str = Field(default="09:00", pattern=r"^\d{2}:\d{2}$")
    work_hours_end: str = Field(default="18:00", pattern=r"^\d{2}:\d{2}$")
    # 周一至周日，1=周一 …… 7=周日（与 datetime.isoweekday 对齐）
    work_days: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5], max_length=7)
    # AI 未配置时的兜底回复；空串 = 保持原有"服务内部错误"行为
    fallback_reply: str = Field(default="", max_length=500)
    # 敏感词词库（M9）：命中后按 sensitive_action 处置；空词库 = 不过滤
    sensitive_words: list[str] = Field(default_factory=list, max_length=200)
    # warn=仅记录 / handoff=记录并转人工 / block=直接拒绝本次输入
    sensitive_action: str = Field(default="warn", pattern="^(warn|handoff|block)$")
    # 质检抽检比例（M9）：0.1 = 每 10 个会话抽 1 个；0 表示不自动抽样
    quality_sample_ratio: float = Field(default=0.0, ge=0, le=1)


class TenantInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    status: str
    quota: dict[str, Any]
    settings: dict[str, Any] = Field(default_factory=dict)


class TenantUpdate(BaseModel):
    """租户基础信息更新。slug 不改：它是子域名与既有链接的锚点。

    name 与 settings 均可选：只提交要改的字段（局部更新）。
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=100)
    settings: TenantSettings | None = None


class TenantQuotaUpdate(BaseModel):
    """配额局部更新：只提交要改的键，未提交的保持不变。"""

    max_messages_per_day: int | None = Field(default=None, ge=0)
    max_concurrent: int | None = Field(default=None, ge=0)
    max_storage_gb: int | None = Field(default=None, ge=0)
    max_tokens_per_day: int | None = Field(default=None, ge=0)


class TenantMemberInfo(BaseModel):
    user_id: uuid.UUID
    email: str
    # 未设置时为 None：该账号只能用邮箱登录
    username: str | None = None
    display_name: str
    role: str
    status: str


class TenantMemberCreate(BaseModel):
    """新增成员。

    为什么必须带密码：本系统 users.tenant_id 是"主租户"，账号由主租户创建，
    暂不支持把已有账号挂到第二个租户（跨租户身份需要额外的租户切换流程）。
    因此这里走"创建新账号并加入当前租户"，而不是发邀请链接。
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    email: str = Field(min_length=3, max_length=255, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    # 可选登录名：留空则该成员只能用邮箱登录
    username: str | None = Field(default=None, pattern=USERNAME_PATTERN)
    # 上限 72 与 bcrypt 的输入截断行为对齐（与注册接口一致）
    password: str = Field(min_length=8, max_length=72)
    display_name: str = Field(default="", max_length=100)
    role: str = Field(default="agent", pattern=f"^({'|'.join(CREATABLE_ROLES)})$")


class TenantMemberUpdate(BaseModel):
    role: str = Field(pattern=f"^({'|'.join(TENANT_ROLES)})$")
