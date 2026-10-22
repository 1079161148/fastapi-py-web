"""认证相关请求/响应模型（设计文档第 7 章统一错误体配套）。

邮箱用字符串 + 正则校验而非 pydantic 的 EmailStr：后者强依赖 email-validator，
而本项目的邮箱仅作登录标识，不需要 DNS/规范完备性校验。
"""

import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

# 用户名：字母数字下划线，3-32 位。允许大写输入，服务层统一转小写存储
USERNAME_PATTERN = r"^[A-Za-z0-9_]{3,32}$"


class RegisterRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    email: str = Field(min_length=3, max_length=255, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    # 可选登录名：不填则该账号只能用邮箱登录（与存量行为一致）
    username: str | None = Field(default=None, pattern=USERNAME_PATTERN)
    password: str = Field(min_length=8, max_length=72)
    display_name: str = Field(default="", max_length=100)
    # 租户名：注册即创建新租户（个人/团队自助开户）
    tenant_name: str = Field(min_length=1, max_length=100)


class LoginRequest(BaseModel):
    """登录请求：`identifier` 同时接受邮箱与用户名。

    保留 `email` 字段是为了不破坏已发布的客户端与脚本（旧调用方只传 email）。
    两者都给时以 `identifier` 为准。
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    identifier: str | None = Field(default=None, min_length=3, max_length=255)
    email: str | None = Field(default=None, min_length=3, max_length=255)
    # 租户标识（slug）：用户名只在本租户内唯一，因此用户名登录必须带上它；
    # 邮箱登录忽略此字段
    tenant: str | None = Field(default=None, max_length=100)
    password: str = Field(min_length=1, max_length=72)

    @model_validator(mode="after")
    def _require_login_id(self) -> "LoginRequest":
        if not (self.identifier or self.email):
            raise ValueError("identifier 不能为空")
        return self

    @property
    def login_id(self) -> str:
        return (self.identifier or self.email or "").strip()


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"


class UserInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    # 未设置时为 None：前端据此决定是否提示"该账号只能用邮箱登录"
    username: str | None = None
    display_name: str
    # role 是平台级角色（admin/agent/customer），决定能不能登录后台
    role: str
    # tenant_role 是该用户**在当前租户内**的角色（owner/admin/agent/viewer）。
    # 权限判定（知识库、坐席台、渠道配置）看的是它，不是上面的 role——
    # 平台 admin 在别人租户里也只是普通成员，把两者混用会出现"菜单可见但接口 403"。
    tenant_role: str = ""
    tenant_id: uuid.UUID


class AuthResponse(BaseModel):
    tokens: TokenPair
    user: UserInfo
