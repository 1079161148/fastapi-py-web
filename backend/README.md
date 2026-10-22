# 后端（FastAPI）

Python 3.12 · FastAPI · SQLAlchemy 2.0(async) · Alembic · PostgreSQL 16 + pgvector · Redis(arq) ·
OpenAI 兼容 AI 网关。完整架构与亮点见仓库根目录 `README.md`，这里只写后端日常开发需要的东西。

## 目录

```
app/
├─ main.py              入口：中间件、路由注册、异常处理器、lifespan
├─ api/
│  ├─ middleware.py     请求上下文（租户解析 / Request-Id / 耗时日志）
│  ├─ presenters.py     SSE 帧生成
│  └─ v1/               auth tenants conversations messages knowledge agent
│                       channels guest voice usage stats insights quality
│                       quick-replies customers audit-logs · deps.py(鉴权依赖)
├─ services/            chat retrieval ingestion knowledge conversations handoff
│                       channels usage stats insights quality sensitive
│                       tenant_settings audit quick_replies customers leave_message
├─ db/
│  ├─ models/           ORM 模型（表名与字段注释）
│  ├─ rls.py            行级安全：事务级 set_config('app.tenant_id')
│  └─ session.py        会话工厂（async）
├─ core/                config security rls pubsub rate_limit logging exceptions
├─ speech/              ASR / TTS 客户端（OpenAI 兼容）
├─ workers/             arq 任务与 cron
└─ migrations/          Alembic 版本（迁移用超管连接，运行时禁止）
```

## 本地运行

```bash
uv venv && uv pip install .
cp .env.example .env          # 至少填 JWT_SECRET（≥32 字符）与 AI 网关
docker compose -f ../deploy/docker-compose.yml up -d   # Postgres / Redis（MinIO 默认注释，用本地存储）
alembic upgrade head
uvicorn app.main:app --reload          # http://127.0.0.1:8000，文档 /docs
arq app.workers.settings.WorkerSettings # 另开一个终端跑任务
```

## 关键约定

| 主题 | 约定 |
|---|---|
| 租户隔离 | 所有表启用 RLS；应用连 `aics_app`（非超管），迁移才用超管连接。未设租户上下文 = 查不到行 |
| 运行时 vs 迁移 | `DATABASE_URL`（运行，受 RLS）/ `MIGRATION_DATABASE_URL`（迁移，超管）两套 |
| 分层 | API 只做校验与鉴权；业务规则一律在 `services/`；跨表查询放 `repositories/`（如有） |
| 错误 | 可预期失败用 `AppError(code, message, status_code, expose=...)`，统一 `{"code","message"}` |
| 日志 | 结构化 + `X-Request-Id` 透传；敏感信息（密码、令牌、证件号）不进日志 |
| 外部调用 | 一律走 `app/llm/factory.py` 与 `app/speech/` 的客户端工厂，便于测试替换 |

## 环境变量

见 `.env.example`，分组：运行 / 认证 / 数据库与缓存 / AI 网关 / 知识库与检索 /
文件存储 / 限流 / 告警 / 渠道凭证 / 用量价格。
生产另有 `deploy/.env.prod.example`（多了 `APP_ENV=production`、`TRUST_PROXY_HEADERS`、内网主机名）。

## 测试

```bash
pytest -q                 # 打真实数据库，仅替换外部 AI 网关
pytest tests/test_quality.py -q
ruff check app/ tests/
```

测试用真实 Postgres：RLS、迁移、SQL 口径这些最容易出错的地方不能被 mock 掉。
`FakeLLM` / `FakeEmbedder` / `FakeQueue` 在 `tests/` 与 `tests/knowledge_helpers.py`。

## 迁移

```bash
alembic revision -m "说明"     # 生成版本文件
alembic upgrade head
alembic downgrade -1
```

新增业务表时记得在迁移里补：索引、`GRANT` 给 `aics_app`、`ENABLE/FORCE ROW LEVEL SECURITY`
以及 `tenant_id` 隔离策略（参考 `migrations/versions/20261021_0011_quality_and_sensitive.py`）。
