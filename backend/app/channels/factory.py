"""适配器工厂：渠道名 → 适配器实例（凭证缺省即该渠道禁用）。

凭证策略（设计文档 3.1 / 9.1）：
- **敏感凭证只从环境变量取**（AppSecret、邮箱密码）。channel_configs.credentials
  只存非敏感展示信息，避免"库里一份、环境里一份"导致错配——两个公众号
  共用一个进程时，用库里的 app_id 配环境里的 secret 必然静默失败。
- 缺凭证时抛 ChannelNotConfiguredError（503），而不是返回一个"哑适配器"：
  否则消息会被静默丢弃，比直接报错更难排查。
"""

from dataclasses import dataclass

import httpx

from app.channels.base import ChannelAdapter, ChannelNotConfiguredError
from app.channels.dingtalk import DingTalkAdapter
from app.channels.dingtalk import account_id_from_body as dingtalk_account
from app.channels.email import EmailAdapter
from app.channels.web import WebAdapter
from app.channels.wechat import WechatAdapter
from app.channels.wechat import account_id_from_body as wechat_account
from app.core.config import get_settings
from app.core.exceptions import AppError
from app.db.models.channel import (
    CHANNEL_DINGTALK,
    CHANNEL_EMAIL,
    CHANNEL_WEB,
    CHANNEL_WECHAT,
)


def build_adapter(
    channel: str,
    *,
    webhook_secret: str = "",
    http_client: httpx.AsyncClient | None = None,
) -> ChannelAdapter:
    """按渠道构造适配器；缺少必要凭证时 503。"""
    settings = get_settings()

    if channel == CHANNEL_WEB:
        return WebAdapter()

    if channel == CHANNEL_WECHAT:
        missing = [
            name
            for name, value in {
                "WECHAT_APP_ID": settings.wechat_app_id,
                "WECHAT_APP_SECRET": settings.wechat_app_secret,
                # 验签必须有 token，缺了连回调都验不过，不能算"已配置"
                "WECHAT_TOKEN": settings.wechat_token,
            }.items()
            if not value.strip()
        ]
        if missing:
            raise ChannelNotConfiguredError(channel, missing)
        return WechatAdapter(
            app_id=settings.wechat_app_id,
            app_secret=settings.wechat_app_secret,
            token=settings.wechat_token,
            http_client=http_client,
        )

    if channel == CHANNEL_DINGTALK:
        missing = [
            name
            for name, value in {
                "DINGTALK_APP_KEY": settings.dingtalk_app_key,
                "DINGTALK_APP_SECRET": settings.dingtalk_app_secret,
            }.items()
            if not value.strip()
        ]
        if missing:
            raise ChannelNotConfiguredError(channel, missing)
        return DingTalkAdapter(
            app_key=settings.dingtalk_app_key,
            app_secret=settings.dingtalk_app_secret,
            robot_code=settings.dingtalk_robot_code,
            http_client=http_client,
        )

    if channel == CHANNEL_EMAIL:
        missing = [
            name
            for name, value in {
                "SMTP_HOST": settings.smtp_host,
                "SMTP_USERNAME": settings.smtp_username,
                "SMTP_PASSWORD": settings.smtp_password,
            }.items()
            if not value.strip()
        ]
        if missing:
            raise ChannelNotConfiguredError(channel, missing)
        return EmailAdapter(
            smtp_host=settings.smtp_host,
            smtp_port=settings.smtp_port,
            smtp_username=settings.smtp_username,
            smtp_password=settings.smtp_password,
            sender=settings.smtp_from,
            imap_host=settings.imap_host,
            imap_port=settings.imap_port,
            imap_username=settings.imap_username,
            imap_password=settings.imap_password,
            webhook_secret=webhook_secret,
        )

    raise AppError(
        code="CHANNEL_UNKNOWN",
        message=f"不支持的渠道：{channel}",
        status_code=404,
        expose=True,
    )


def extract_account_id(channel: str, body: bytes) -> str:
    """从原始报文提取平台账号标识，用于入站反查租户。

    邮件没有平台签名，账号由调用方通过 `?account=` 指定（见 webhook 路由）。
    """
    if channel == CHANNEL_WECHAT:
        return wechat_account(body)
    if channel == CHANNEL_DINGTALK:
        return dingtalk_account(body)
    return ""


@dataclass(frozen=True, slots=True)
class CredentialStatus:
    """渠道凭证状态：供管理接口解释"为什么配了却不可用"。"""

    configured: bool
    missing: list[str]
    required: str


# 各渠道必需的环境变量（仅用于提示，实际校验在 build_adapter 里）
REQUIRED_ENV: dict[str, str] = {
    CHANNEL_WECHAT: "WECHAT_APP_ID / WECHAT_APP_SECRET / WECHAT_TOKEN",
    CHANNEL_DINGTALK: "DINGTALK_APP_KEY / DINGTALK_APP_SECRET",
    CHANNEL_EMAIL: "SMTP_HOST / SMTP_USERNAME / SMTP_PASSWORD",
}


def credentials_hint(channel: str) -> CredentialStatus:
    required = REQUIRED_ENV.get(channel, "")
    try:
        build_adapter(channel)
    except ChannelNotConfiguredError as exc:
        return CredentialStatus(configured=False, missing=list(exc.missing), required=required)
    return CredentialStatus(configured=True, missing=[], required=required)


__all__ = [
    "CredentialStatus",
    "build_adapter",
    "credentials_hint",
    "extract_account_id",
]
