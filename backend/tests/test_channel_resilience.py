"""M8 渠道异常演练。

目标：验证渠道在"配置缺失 / 上游拒绝 / 无需投递"三种异常下的行为**可预期**，
而不是让异常变成静默丢消息或整轮 500。

设计文档第 10 章降级要求：
- 凭证缺失 → 该渠道禁用并给出明确提示，而不是报错影响其他渠道
- 上游失败 → 明确错误（含平台错误码），由调用方决定重试还是提示用户

这些用例不依赖数据库：直接对 ChannelRouter + 适配器做故障注入。
"""

import uuid

import httpx
import pytest

from app.channels.base import ChannelNotConfiguredError, ChannelSendError
from app.channels.router import ChannelRouter
from app.core.config import get_settings


def _message() -> dict[str, object]:
    return {
        "conversation_id": uuid.uuid4(),
        "external_user_id": "openid-test",
        "content": "你好",
    }


async def test_router_reports_missing_credentials(monkeypatch) -> None:
    settings = get_settings()
    for name in ("wechat_app_id", "wechat_app_secret", "wechat_token"):
        monkeypatch.setattr(settings, name, "")

    router = ChannelRouter()
    with pytest.raises(ChannelNotConfiguredError) as excinfo:
        await router.deliver(channel="wechat", **_message())  # type: ignore[arg-type]

    # 错误信息必须指出缺哪几个变量，否则运维只能猜
    assert "WECHAT_APP_ID" in excinfo.value.message
    assert excinfo.value.status_code == 503
    assert excinfo.value.expose is True


async def test_wechat_send_failure_surfaces_platform_error(monkeypatch) -> None:
    settings = get_settings()
    # 唯一 app_id：微信 access_token 缓存以 app_id 为键，避免跨用例串味
    monkeypatch.setattr(settings, "wechat_app_id", f"app-{uuid.uuid4().hex[:8]}")
    monkeypatch.setattr(settings, "wechat_app_secret", "secret")
    monkeypatch.setattr(settings, "wechat_token", "token")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/cgi-bin/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 7200})
        return httpx.Response(
            200, json={"errcode": 45015, "errmsg": "response out of time limit"}
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        router = ChannelRouter(http_client=http_client)
        with pytest.raises(ChannelSendError) as excinfo:
            await router.deliver(channel="wechat", **_message())  # type: ignore[arg-type]

    # 平台错误码必须保留：这是排查"用户没收到消息"的唯一线索
    assert "45015" in excinfo.value.message
    assert excinfo.value.status_code >= 500


async def test_web_channel_outbound_is_noop() -> None:
    """web 渠道靠 SSE 推送，出站返回 None 是正常语义，不是失败。"""
    router = ChannelRouter()
    result = await router.deliver(channel="web", **_message())  # type: ignore[arg-type]
    assert result is None
