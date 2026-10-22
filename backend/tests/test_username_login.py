"""用户名登录测试（迁移 0008：用户名租户内唯一）。

验收点：
- 用户名在**租户内**唯一：不同租户可以同名，同租户内（新增成员）不可重名
- 用户名只在本租户内唯一 → 登录必须带租户 slug，否则无法定位
- 缺 slug → 400 TENANT_REQUIRED；slug 错误 → 401（不暴露租户是否存在）
- 邮箱路径不受影响：全局唯一，无需 slug
- 大小写不敏感、被禁用账号不能登录、旧客户端字段兼容
"""

import uuid

from chat_helpers import PASSWORD, auth_headers, register_user
from httpx import AsyncClient


def _username(prefix: str = "u") -> str:
    return f"{prefix}{uuid.uuid4().hex[:10]}"


async def _slug_of(client: AsyncClient, access_token: str) -> str:
    resp = await client.get(
        "/api/v1/tenants/current", headers=auth_headers(access_token)
    )
    assert resp.status_code == 200, resp.text
    return str(resp.json()["slug"])


async def _register(
    client: AsyncClient, *, email: str, username: str, tenant_name: str
) -> tuple[dict, str]:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "username": username,
            "password": PASSWORD,
            "tenant_name": tenant_name,
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    return data, await _slug_of(client, data["tokens"]["access_token"])


async def test_login_by_username_with_tenant(
    client: AsyncClient, unique_email: str
) -> None:
    username = _username()
    data, slug = await _register(
        client, email=unique_email, username=username, tenant_name="用户名租户"
    )
    assert data["user"]["username"] == username

    by_name = await client.post(
        "/api/v1/auth/login",
        json={"identifier": username, "tenant": slug, "password": PASSWORD},
    )
    assert by_name.status_code == 200, by_name.text
    assert by_name.json()["user"]["email"] == unique_email

    # 邮箱是全局唯一标识，不需要租户
    by_email = await client.post(
        "/api/v1/auth/login", json={"identifier": unique_email, "password": PASSWORD}
    )
    assert by_email.status_code == 200
    assert by_email.json()["user"]["id"] == by_name.json()["user"]["id"]


async def test_username_login_requires_tenant(
    client: AsyncClient, unique_email: str
) -> None:
    username = _username()
    await _register(client, email=unique_email, username=username, tenant_name="缺租户")

    missing = await client.post(
        "/api/v1/auth/login", json={"identifier": username, "password": PASSWORD}
    )
    assert missing.status_code == 400
    assert missing.json()["code"] == "TENANT_REQUIRED"


async def test_wrong_tenant_slug_is_generic_401(
    client: AsyncClient, unique_email: str
) -> None:
    username = _username()
    await _register(client, email=unique_email, username=username, tenant_name="错租户")

    wrong = await client.post(
        "/api/v1/auth/login",
        json={
            "identifier": username,
            "tenant": f"no-such-tenant-{uuid.uuid4().hex[:6]}",
            "password": PASSWORD,
        },
    )
    assert wrong.status_code == 401
    # 不区分"租户不存在"与"用户不存在"，避免租户枚举
    assert wrong.json()["code"] == "INVALID_CREDENTIALS"


async def test_same_username_allowed_in_different_tenants(
    client: AsyncClient, unique_email: str
) -> None:
    """核心用例：两家公司各有一个 zhangsan，互不冲突，登录落到各自租户。"""
    shared = _username("same")
    email_a = f"a-{unique_email}"
    email_b = f"b-{unique_email}"

    data_a, slug_a = await _register(
        client, email=email_a, username=shared, tenant_name=f"甲公司 {shared}"
    )
    data_b, slug_b = await _register(
        client, email=email_b, username=shared, tenant_name=f"乙公司 {shared}"
    )
    assert slug_a != slug_b

    login_a = await client.post(
        "/api/v1/auth/login",
        json={"identifier": shared, "tenant": slug_a, "password": PASSWORD},
    )
    login_b = await client.post(
        "/api/v1/auth/login",
        json={"identifier": shared, "tenant": slug_b, "password": PASSWORD},
    )
    assert login_a.status_code == 200, login_a.text
    assert login_b.status_code == 200, login_b.text
    assert login_a.json()["user"]["email"] == email_a
    assert login_b.json()["user"]["email"] == email_b
    # 两个账号确实是不同的人（不同 user_id / tenant_id）
    assert login_a.json()["user"]["id"] != login_b.json()["user"]["id"]
    assert (
        data_a["user"]["tenant_id"] != data_b["user"]["tenant_id"]
    )


async def test_duplicate_username_in_same_tenant_rejected(
    client: AsyncClient, unique_email: str
) -> None:
    owner = await register_user(client, unique_email)
    owner_token = owner["tokens"]["access_token"]
    username = _username("dup")

    first = await client.post(
        "/api/v1/tenants/current/members",
        json={
            "email": f"m1-{unique_email}",
            "username": username,
            "password": PASSWORD,
            "role": "agent",
        },
        headers=auth_headers(owner_token),
    )
    assert first.status_code == 201, first.text

    # 同租户内重名（大小写不同也算同一名字）→ 409
    second = await client.post(
        "/api/v1/tenants/current/members",
        json={
            "email": f"m2-{unique_email}",
            "username": username.upper(),
            "password": PASSWORD,
            "role": "agent",
        },
        headers=auth_headers(owner_token),
    )
    assert second.status_code == 409
    assert second.json()["code"] == "CONFLICT"


async def test_username_is_case_insensitive(client: AsyncClient, unique_email: str) -> None:
    username = _username()
    data, slug = await _register(
        client, email=unique_email, username=username.upper(), tenant_name="大小写租户"
    )
    assert data["user"]["username"] == username

    for candidate in (username, username.upper(), username.capitalize()):
        resp = await client.post(
            "/api/v1/auth/login",
            json={"identifier": candidate, "tenant": slug, "password": PASSWORD},
        )
        assert resp.status_code == 200, f"{candidate} 登录失败：{resp.text}"


async def test_invalid_username_format_rejected(
    client: AsyncClient, unique_email: str
) -> None:
    for bad in ("ab", "a" * 33, "has space", "has@sign", "中文名"):
        resp = await client.post(
            "/api/v1/auth/register",
            json={
                "email": unique_email,
                "username": bad,
                "password": PASSWORD,
                "tenant_name": "格式租户",
            },
        )
        assert resp.status_code == 422, f"{bad!r} 应被拒绝，实际 {resp.status_code}"


async def test_register_without_username_keeps_email_only(
    client: AsyncClient, unique_email: str
) -> None:
    data = await register_user(client, unique_email)
    assert data["user"]["username"] is None

    by_email = await client.post(
        "/api/v1/auth/login", json={"email": unique_email, "password": PASSWORD}
    )
    assert by_email.status_code == 200

    # 不带 @ 的标识被当成用户名：缺 slug → 400，不会误判成邮箱
    by_bogus = await client.post(
        "/api/v1/auth/login", json={"identifier": "nobody-here", "password": PASSWORD}
    )
    assert by_bogus.status_code == 400
    assert by_bogus.json()["code"] == "TENANT_REQUIRED"


async def test_username_wrong_password_is_generic(
    client: AsyncClient, unique_email: str
) -> None:
    username = _username()
    _, slug = await _register(
        client, email=unique_email, username=username, tenant_name="密码租户"
    )
    resp = await client.post(
        "/api/v1/auth/login",
        json={"identifier": username, "tenant": slug, "password": "wrong-password"},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "INVALID_CREDENTIALS"


async def test_disabled_user_cannot_login_by_username(
    client: AsyncClient, unique_email: str
) -> None:
    owner = await register_user(client, unique_email)
    owner_token = owner["tokens"]["access_token"]
    slug = await _slug_of(client, owner_token)
    member_username = _username("d")

    created = await client.post(
        "/api/v1/tenants/current/members",
        json={
            "email": f"disabled-{unique_email}",
            "username": member_username,
            "password": PASSWORD,
            "role": "agent",
        },
        headers=auth_headers(owner_token),
    )
    assert created.status_code == 201
    member_id = created.json()["user_id"]

    ok = await client.post(
        "/api/v1/auth/login",
        json={"identifier": member_username, "tenant": slug, "password": PASSWORD},
    )
    assert ok.status_code == 200

    removed = await client.delete(
        f"/api/v1/tenants/current/members/{member_id}",
        headers=auth_headers(owner_token),
    )
    assert removed.status_code == 204

    blocked = await client.post(
        "/api/v1/auth/login",
        json={"identifier": member_username, "tenant": slug, "password": PASSWORD},
    )
    assert blocked.status_code == 401
    assert blocked.json()["code"] == "INVALID_CREDENTIALS"
