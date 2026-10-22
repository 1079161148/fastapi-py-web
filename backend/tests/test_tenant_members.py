"""M7 租户与成员管理测试（真实数据库，RLS 生效）。

覆盖不变量：
- 新增成员后可用新账号登录，且角色生效（agent 不能管成员/看成本）
- 邮箱已存在一律 409，不做静默接管
- 最后一个 owner 不可降级、不可移除
- 不能移除自己
- 移除主租户成员后账号被禁用、刷新令牌被吊销
"""

from chat_helpers import PASSWORD, auth_headers, register_user
from httpx import AsyncClient


async def _create_agent(
    client: AsyncClient, *, token: str, email: str, role: str = "agent"
) -> dict:
    resp = await client.post(
        "/api/v1/tenants/current/members",
        json={
            "email": email,
            "password": PASSWORD,
            "display_name": f"成员 {role}",
            "role": role,
        },
        headers=auth_headers(token),
    )
    return {"status": resp.status_code, "body": resp.json()}


async def test_add_member_and_login(client: AsyncClient, unique_email: str) -> None:
    owner = await register_user(client, unique_email)
    owner_token = owner["tokens"]["access_token"]
    agent_email = f"agent-{unique_email}"

    created = await _create_agent(client, token=owner_token, email=agent_email)
    assert created["status"] == 201, created["body"]
    assert created["body"]["role"] == "agent"
    assert created["body"]["email"] == agent_email

    login = await client.post(
        "/api/v1/auth/login", json={"email": agent_email, "password": PASSWORD}
    )
    assert login.status_code == 200, login.text
    assert login.json()["user"]["tenant_id"] == owner["user"]["tenant_id"]

    members = await client.get(
        "/api/v1/tenants/current/members", headers=auth_headers(owner_token)
    )
    assert members.status_code == 200
    emails = {item["email"] for item in members.json()}
    assert emails == {unique_email, agent_email}


async def test_add_member_rejects_existing_email(
    client: AsyncClient, unique_email: str
) -> None:
    owner = await register_user(client, unique_email)
    owner_token = owner["tokens"]["access_token"]

    again = await _create_agent(client, token=owner_token, email=unique_email)
    assert again["status"] == 409
    assert again["body"]["code"] == "CONFLICT"


async def test_agent_cannot_manage_members(client: AsyncClient, unique_email: str) -> None:
    owner = await register_user(client, unique_email)
    agent_email = f"agent-{unique_email}"
    await _create_agent(client, token=owner["tokens"]["access_token"], email=agent_email)

    login = await client.post(
        "/api/v1/auth/login", json={"email": agent_email, "password": PASSWORD}
    )
    agent_token = login.json()["tokens"]["access_token"]

    denied = await client.post(
        "/api/v1/tenants/current/members",
        json={"email": f"x-{unique_email}", "password": PASSWORD, "role": "agent"},
        headers=auth_headers(agent_token),
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "FORBIDDEN"

    rename = await client.patch(
        "/api/v1/tenants/current",
        json={"name": "改名尝试"},
        headers=auth_headers(agent_token),
    )
    assert rename.status_code == 403


async def test_last_owner_is_protected(client: AsyncClient, unique_email: str) -> None:
    owner = await register_user(client, unique_email)
    token = owner["tokens"]["access_token"]
    owner_user_id = owner["user"]["id"]

    demote = await client.patch(
        f"/api/v1/tenants/current/members/{owner_user_id}",
        json={"role": "agent"},
        headers=auth_headers(token),
    )
    assert demote.status_code == 409
    assert demote.json()["code"] == "LAST_OWNER_PROTECTED"

    remove = await client.delete(
        f"/api/v1/tenants/current/members/{owner_user_id}", headers=auth_headers(token)
    )
    # 移除自己优先命中"不能移除自己"，同样不会破坏 owner 唯一性
    assert remove.status_code == 409
    assert remove.json()["code"] == "CANNOT_REMOVE_SELF"


async def test_promote_then_demote_owner(client: AsyncClient, unique_email: str) -> None:
    owner = await register_user(client, unique_email)
    token = owner["tokens"]["access_token"]
    second_email = f"second-{unique_email}"
    created = await _create_agent(client, token=token, email=second_email, role="admin")
    assert created["status"] == 201
    second_id = created["body"]["user_id"]

    promoted = await client.patch(
        f"/api/v1/tenants/current/members/{second_id}",
        json={"role": "owner"},
        headers=auth_headers(token),
    )
    assert promoted.status_code == 200
    assert promoted.json()["role"] == "owner"

    # 现在有两个 owner：可以把原 owner 降级
    demoted = await client.patch(
        f"/api/v1/tenants/current/members/{owner['user']['id']}",
        json={"role": "admin"},
        headers=auth_headers(token),
    )
    assert demoted.status_code == 200
    assert demoted.json()["role"] == "admin"


async def test_remove_member_disables_account(
    client: AsyncClient, unique_email: str
) -> None:
    owner = await register_user(client, unique_email)
    token = owner["tokens"]["access_token"]
    agent_email = f"agent-{unique_email}"
    created = await _create_agent(client, token=token, email=agent_email)
    agent_id = created["body"]["user_id"]

    agent_login = await client.post(
        "/api/v1/auth/login", json={"email": agent_email, "password": PASSWORD}
    )
    assert agent_login.status_code == 200
    agent_refresh = agent_login.json()["tokens"]["refresh_token"]

    removed = await client.delete(
        f"/api/v1/tenants/current/members/{agent_id}", headers=auth_headers(token)
    )
    assert removed.status_code == 204

    # 账号被禁用：登录失败
    blocked = await client.post(
        "/api/v1/auth/login", json={"email": agent_email, "password": PASSWORD}
    )
    assert blocked.status_code == 401

    # 已签发的刷新令牌被吊销：不能靠旧令牌续命
    refreshed = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": agent_refresh}
    )
    assert refreshed.status_code == 401
    assert refreshed.json()["code"] in {"TOKEN_REVOKED", "UNAUTHENTICATED"}

    members = await client.get(
        "/api/v1/tenants/current/members", headers=auth_headers(token)
    )
    assert {item["email"] for item in members.json()} == {unique_email}


async def test_rename_tenant(client: AsyncClient, unique_email: str) -> None:
    owner = await register_user(client, unique_email)
    token = owner["tokens"]["access_token"]

    before = await client.get("/api/v1/tenants/current", headers=auth_headers(token))
    assert before.status_code == 200

    renamed = await client.patch(
        "/api/v1/tenants/current",
        json={"name": "新的租户名"},
        headers=auth_headers(token),
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "新的租户名"
    # slug 不变：它是子域名与既有链接的锚点
    assert renamed.json()["slug"] == before.json()["slug"]
