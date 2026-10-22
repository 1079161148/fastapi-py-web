"""认证流程集成测试（真实数据库 + RLS）。"""

import uuid

from httpx import AsyncClient

from app.core.tenant import set_current_tenant
from app.db.models.tenant import Tenant
from app.db.session import async_session_factory

PASSWORD = "test-password-123"


async def _register(client: AsyncClient, email: str, tenant_name: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "tenant_name": tenant_name},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_register_returns_tokens_and_user(client: AsyncClient, unique_email: str) -> None:
    data = await _register(client, unique_email, "测试租户")
    assert data["tokens"]["access_token"]
    assert data["tokens"]["refresh_token"]
    assert data["user"]["email"] == unique_email
    # 注册者即租户管理员
    assert data["user"]["role"] == "admin"


async def test_duplicate_register_rejected(client: AsyncClient, unique_email: str) -> None:
    await _register(client, unique_email, "重复租户")
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": unique_email, "password": PASSWORD, "tenant_name": "另一个租户"},
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "CONFLICT"


async def test_login_and_me_flow(client: AsyncClient, unique_email: str) -> None:
    await _register(client, unique_email, "登录租户")

    resp = await client.post(
        "/api/v1/auth/login", json={"email": unique_email, "password": PASSWORD}
    )
    assert resp.status_code == 200
    tokens = resp.json()["tokens"]

    me = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    assert me.status_code == 200
    assert me.json()["email"] == unique_email


async def test_login_wrong_password_is_generic(client: AsyncClient, unique_email: str) -> None:
    await _register(client, unique_email, "密码租户")
    resp = await client.post(
        "/api/v1/auth/login", json={"email": unique_email, "password": "wrong-password"}
    )
    assert resp.status_code == 401
    # 不区分"邮箱不存在"与"密码错误"，防账号枚举
    assert resp.json()["code"] == "INVALID_CREDENTIALS"


async def test_me_requires_token(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 401
    assert resp.json()["code"] == "UNAUTHENTICATED"


async def test_refresh_rotates_and_rejects_replay(client: AsyncClient, unique_email: str) -> None:
    data = await _register(client, unique_email, "刷新租户")
    old_refresh = data["tokens"]["refresh_token"]

    first = await client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert first.status_code == 200
    new_refresh = first.json()["tokens"]["refresh_token"]
    assert new_refresh != old_refresh

    # 旧 refresh 已吊销：重放必须失败
    replay = await client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert replay.status_code == 401
    assert replay.json()["code"] == "TOKEN_REVOKED"


async def test_current_tenant_and_members(client: AsyncClient, unique_email: str) -> None:
    data = await _register(client, unique_email, "成员租户")
    headers = {"Authorization": f"Bearer {data['tokens']['access_token']}"}

    current = await client.get("/api/v1/tenants/current", headers=headers)
    assert current.status_code == 200
    assert current.json()["id"] == data["user"]["tenant_id"]

    members = await client.get("/api/v1/tenants/current/members", headers=headers)
    assert members.status_code == 200
    assert len(members.json()) == 1
    assert members.json()[0]["role"] == "owner"


async def test_rls_blocks_cross_tenant_read(unique_email: str) -> None:
    """RLS 核心验证：A 租户上下文里看不到 B 租户的数据。"""
    from app.services import auth as auth_service

    email_a = f"rls-a-{uuid.uuid4().hex[:8]}@example.com"
    email_b = f"rls-b-{uuid.uuid4().hex[:8]}@example.com"

    async with async_session_factory() as session:
        auth_a = await auth_service.register(
            session, email=email_a, password=PASSWORD, display_name="", tenant_name="RLS-A"
        )
        auth_b = await auth_service.register(
            session, email=email_b, password=PASSWORD, display_name="", tenant_name="RLS-B"
        )

    tenant_a = auth_a.user.tenant_id  # 已是 UUID 对象，无需再包装
    tenant_b = auth_b.user.tenant_id

    async with async_session_factory() as session:
        set_current_tenant(tenant_a)
        visible = await session.execute(Tenant.__table__.select().where(Tenant.id == tenant_b))
        assert visible.first() is None, "RLS 失效：A 租户读到了 B 租户"

        own = await session.execute(Tenant.__table__.select().where(Tenant.id == tenant_a))
        assert own.first() is not None, "RLS 失效：A 租户读不到自己的数据"

    set_current_tenant(None)
