"""健康检查测试。

liveness 不依赖数据库，可在无基础设施的 CI 环境运行；
readiness 需要真实数据库，留给 M1 集成测试。
"""

from httpx import ASGITransport, AsyncClient

from app.main import app


async def test_liveness_returns_ok() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
