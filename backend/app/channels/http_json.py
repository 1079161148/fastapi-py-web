"""渠道出站 HTTP 公共封装（微信 / 钉钉共用）。

为什么单独抽出来：两个平台都是"HTTP + JSON + errcode 约定"，
错误处理（网络失败、非 2xx、非 JSON）必须一致，各写一份必然漂移。
只处理传输层，业务错误码仍由各适配器翻译成可读消息。
"""

from typing import Any

import httpx

from app.channels.base import ChannelSendError


async def request_json(
    *,
    client: httpx.AsyncClient | None,
    channel: str,
    method: str,
    url: str,
    timeout_seconds: float,
    **kwargs: Any,
) -> dict[str, Any]:
    """发送请求并返回 JSON 对象；任何异常都转成 ChannelSendError。

    client 由调用方注入（测试用 MockTransport），为 None 时临时建连：
    渠道出站频率低，连接复用收益不值得引入长生命周期客户端。
    """
    try:
        if client is not None:
            response = await client.request(method, url, **kwargs)
        else:
            async with httpx.AsyncClient(timeout=timeout_seconds) as temporary:
                response = await temporary.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise ChannelSendError(channel, f"网络请求失败：{type(exc).__name__}") from exc

    if response.status_code >= 400:
        raise ChannelSendError(channel, f"接口返回 HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise ChannelSendError(channel, "接口返回非 JSON 报文") from exc
    if not isinstance(data, dict):
        raise ChannelSendError(channel, "接口返回格式异常")
    return data


__all__ = ["request_json"]
