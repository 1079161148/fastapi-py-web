"""测试基础设施。

策略：集成测试直连真实 PostgreSQL（本地 Docker 或 CI service），
不用 mock 数据库——RLS 行为只有真实库才能验证。
测试数据用随机邮箱，互不干扰；会话开始前确保迁移到 head（幂等）。
"""

# 必须在导入 app 之前设置：settings 在 import 时即求值（lru_cache 单例），
# PyJWT 拒绝空密钥，本地开发若无 .env 会直接报错
import os
import tempfile
from pathlib import Path

os.environ.setdefault("JWT_SECRET", "test-secret-not-for-production-use-only-in-ci")
# 限流默认关闭：用例共享同一 Redis，计数会跨用例累积导致随机失败。
# 限流的专项用例在 tests/test_rate_limit.py 里显式打开。
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
# 上传文件落系统临时目录：测试不应把产物写进仓库（backend/var 已在 .gitignore）
os.environ.setdefault("STORAGE_LOCAL_DIR", str(Path(tempfile.gettempdir()) / "aics-test-uploads"))

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from alembic import command
from alembic.config import Config
from chat_helpers import FakeLLM
from httpx import ASGITransport, AsyncClient
from knowledge_helpers import FakeEmbedder, FakeQueue, FakeReranker

from app.api.v1 import deps
from app.core.config import get_settings
from app.core.pubsub import close_redis
from app.db.session import engine
from app.main import app

settings = get_settings()


@pytest.fixture(scope="session", autouse=True)
def apply_migrations() -> None:
    """确保库结构是最新的（幂等，已到 head 时立即返回）。"""
    config = Config(str(Path(__file__).parent.parent / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.migration_database_url)
    asyncio.run(asyncio.to_thread(command.upgrade, config, "head"))


@pytest.fixture(autouse=True)
async def _isolate_connections():
    """每个测试后释放数据库与 Redis 连接。

    pytest-asyncio 为每个测试创建独立事件循环，而 engine / Redis 客户端
    都是模块级单例：池中的连接跨循环复用时 transport 已随循环关闭，必然报
    "'NoneType' object has no attribute 'send'"（Redis 侧表现为 Event loop is closed）。
    测试后 dispose/close 是最小代价的解法。
    """
    yield
    await close_redis()
    await engine.dispose()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.fixture
def unique_email() -> str:
    return f"test-{uuid.uuid4().hex[:12]}@example.com"


@pytest.fixture
def fake_llm() -> FakeLLM:
    """把 LLM 客户端换成本地假实现（不触网，验证完整流式链路）。

    两个 provider 都要覆盖：文本入口用 provide_optional_llm_client（未配置时
    允许 None 以走兜底），语音入口仍用 provide_llm_client（缺配置必须 503）。
    只覆盖一个会让另一条链路在测试里真实触网。
    """
    llm = FakeLLM()
    app.dependency_overrides[deps.provide_llm_client] = lambda: llm
    app.dependency_overrides[deps.provide_optional_llm_client] = lambda: llm
    try:
        yield llm
    finally:
        app.dependency_overrides.pop(deps.provide_llm_client, None)
        app.dependency_overrides.pop(deps.provide_optional_llm_client, None)


@pytest.fixture
def fake_embedder() -> FakeEmbedder:
    """确定性假嵌入：让"同词文本更相似"成立，从而能断言向量召回。"""
    embedder = FakeEmbedder()
    app.dependency_overrides[deps.provide_embedding_client] = lambda: embedder
    app.dependency_overrides[deps.provide_required_embedding_client] = lambda: embedder
    try:
        yield embedder
    finally:
        app.dependency_overrides.pop(deps.provide_embedding_client, None)
        app.dependency_overrides.pop(deps.provide_required_embedding_client, None)


@pytest.fixture
def fake_queue() -> FakeQueue:
    """记录入队调用的假队列：避免测试依赖 Redis / 真实 worker。"""
    queue = FakeQueue()
    app.dependency_overrides[deps.provide_job_queue] = lambda: queue
    try:
        yield queue
    finally:
        app.dependency_overrides.pop(deps.provide_job_queue, None)


@pytest.fixture
def fake_reranker() -> FakeReranker:
    reranker = FakeReranker()
    app.dependency_overrides[deps.provide_reranker] = lambda: reranker
    try:
        yield reranker
    finally:
        app.dependency_overrides.pop(deps.provide_reranker, None)
