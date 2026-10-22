# 智能客服 AI 系统 — 架构设计文档

> 版本：v1.0（首次评审稿）
> 日期：2026-10-20
> 状态：待评审
> 说明：
> 设计决策记录：坐席端实时通知采用 SSE（轮询降级，不引入 WebSocket）；匿名访客 guest_id 先聊、登录后双凭证合并；LLM 环境变量定名 `AI_API_KEY` / `AI_BASE_URL` / `AI_MODEL`；guest_id 采用 cookie 存储；多租户 / 多渠道 / 语音 / 检索增强纳入本期完整实现
> 依赖版本核实日期：2026-10-20（PyPI / npm）

---

## 1. 项目概述

### 1.1 目标

构建一个前后端分离的智能客服系统，核心能力（**均为本期完整实现范围**）：

| 能力 | 说明 |
|---|---|
| AI 问答 | 基于 LLM + 知识库（RAG）回答用户咨询，流式输出 |
| 知识库管理 | 文档上传、解析、分块、向量化、混合检索 + rerank |
| 会话管理 | 多轮对话、历史记录、上下文管理 |
| 人工接管 | AI 无法处理时转人工坐席，会话无缝移交（坐席端 SSE 实时通知） |
| 匿名访客会话 | 未登录访客通过 guest_id 先聊，登录后双凭证安全合并 |
| 多渠道接入 | Web / 邮件 / 微信 / 钉钉统一接入，共用一套对话链路 |
| 语音客服 | 语音消息（录音上传 → ASR → 对话 → TTS 回复）完整闭环 |
| 多租户隔离 | 从第一天开启 PostgreSQL 行级安全（RLS），租户级配置与配额 |
| 管理后台 | 租户/用户权限、知识库、会话记录、用量与成本统计 |

### 1.2 本期完整实现的能力与明确边界

| 能力 | 本期实现范围 | 设计章节 |
|---|---|---|
| 多租户隔离 | 全表 `tenant_id` + 租户上下文 + **开启 RLS** + 租户管理 API + 租户级配额/限流/模型配置 | 9.2 |
| 多渠道接入 | `ChannelAdapter` 抽象 + Web / Email / 微信 / 钉钉 **四个适配器全部实现**，凭证配置化（未配置则该渠道自动禁用） | 9.1 |
| 语音客服 | 语音消息全链路：上传 → ASR → 进入既有文本对话链路 → TTS 合成回复；前端录音组件 | 9.3 |
| 检索增强 | pgvector 向量检索 + PostgreSQL BM25 全文检索 + RRF 融合 + rerank 重排 + 可配置分块 | 9.4 |

**明确不做（本期内不实现，写明原因与重启条件）**

| 项 | 原因 | 重启条件 |
|---|---|---|
| 实时全双工语音通话（WebRTC + SFU） | 需引入媒体服务器与流式 ASR/LLM/TTS 全双工链路，投入与风险远高于收益；语音消息已覆盖"用户不方便打字"的核心诉求 | 业务明确要求通话功能时，按 9.3 的演进路径单独立项 |
| 自研向量检索引擎 | **不造轮子**：相似度算法、索引结构、HNSW 参数调优均已有成熟方案，自研无差异化收益且长期背维护债。检索能力本期做全（混合检索 + rerank），引擎只用 pgvector | 不设重启条件；若 pgvector 规模不足，仅换 Qdrant/Milvus 实现（见 9.4 第 4 级） |

---

## 2. 总体架构

### 2.1 架构图

```
┌───────────────────────────────────────────────────────────────┐
│  用户咨询端 (Vue 3 SPA)          管理后台 (Vue 3 SPA)          │
│  - 文本/语音消息 / SSE 流式        - 租户/知识库/会话/成本看板│
└──────────────┬────────────────────────┬──────────────────────┘
               │ HTTPS (REST + SSE)     │        ▲
               ▼                        │        │ 平台异步回调
┌───────────────────────────────┐        │   ┌────┴──────────────────┐
│  Nginx 反向代理 / 静态资源 /     │        └───┤ 微信 / 钉钉 / 邮件      │
│  SSE 长连接 / 限流入口           │            │ (webhook 入站, 验签)  │
└───────────────┬───────────────┘            └─────────────────────┘
                ▼
┌───────────────────────────────────────────────────────────────┐
│  FastAPI 应用层 (uvicorn, 多 worker)                             │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────────┐  │
│  │ API 路由  │ │ 认证鉴权  │ │ 会话服务  │ │  Chat 编排服务     │  │
│  │ /api/v1  │ │+租户上下文│ │ +渠道路由 │ │  (LangGraph)      │  │
│  └──────────┘ └──────────┘ └──────────┘ └────────┬─────────┘  │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐          │            │
│  │ 知识库服务│ │ 检索服务  │ │ 语音服务  │          ▼            │
│  │          │ │ 向量+BM25 │ │ ASR/TTS  │  ┌──────────────┐     │
│  └────┬─────┘ │  +rerank │ └────┬─────┘  │  LLM 适配层    │     │
│         │     └────┬─────┘      │        │ (OpenAI 兼容)  │     │
└─────────┼──────────┼────────────┼────────└──────┬───────┘     │
          ▼          ▼            ▼               ▼             │
┌───────────────┐ ┌──────────────┐ ┌──────────────────┐         │
│  PostgreSQL   │ │    Redis     │ │  对象存储 MinIO    │         │
│  + pgvector   │ │ (缓存/队列/  │ │  (音频/文档附件)  │         │
│  + RLS 多租户 │ │  会话热数据) │ └──────────────────┘         │
└───────────────┘ └──────────────┘                             │
        ▲         异步任务（解析 / 向量化 / ASR / TTS）          │
┌───────┴────────┐           ┌──────────────────────┐          │
│  arq Worker    │           │  LLM / ASR / TTS API │          │
└────────────────┘           └──────────────────────┘          │
```

### 2.2 分层职责

| 层 | 职责 | 约束 |
|---|---|---|
| 前端 SPA | 交互、流式渲染、录音上传、状态管理 | 不直连数据库/LLM；所有请求走 API |
| Nginx | 反向代理、静态资源、TLS 终止、限流 | 无业务逻辑 |
| API 层 | 参数校验、鉴权、租户上下文注入、编排、SSE 输出 | 不做重计算（向量化/转写等下沉到 Worker） |
| 渠道适配层 | 各渠道协议 ↔ 内部统一消息模型互转 | 渠道差异只允许出现在适配器内 |
| Chat 编排层 | 对话状态机、RAG 检索、工具调用 | 通过 LLM 适配层访问模型，不直接耦合 SDK |
| 数据层 | 业务数据 + 向量 + 缓存 + 附件 | 全参数绑定；Redis 仅作热数据与队列；RLS 强制租户隔离 |

---

## 3. 技术选型与理由

> 以下版本为 2026-10-20 核实的当前最新稳定版，实际以 `uv.lock` / `package-lock.json` 锁定为准。

### 3.1 后端

| 组件 | 选型 | 版本 | 为什么选它 | 取舍 / 备选 |
|---|---|---|---|---|
| 语言 | Python | 3.12+ | AI 生态绝对主场；SQLAlchemy 2.1 要求 ≥3.11，本项目实际环境 3.12.8 已满足全部依赖（FastAPI/Pydantic/LangChain/LangGraph 均 ≥3.10），版本由 uv 锁定 | 3.13+ 亦完全支持（uv 可随时切换），无实质备选 |
| Web 框架 | **FastAPI** | 0.150.0 | 原生 async（LLM 流式场景关键）；基于 Pydantic 的自动校验；自动生成 OpenAPI 文档，前后端联调成本低；Starlette 生态成熟 | Django：重 Admin、同步优先，不适合流式 AI 场景；Flask：过裸，需自行组装校验/文档/异步 |
| ASGI 服务器 | Uvicorn | 0.54.0 | FastAPI 官方推荐，纯 Python 可调试，生产可挂多 worker | Gunicorn+Uvicorn worker 作为生产备选 |
| 数据校验 | Pydantic | 2.14.1 | FastAPI 内置依赖，Rust core 性能足够；v2 API 稳定 | 无 |
| ORM | SQLAlchemy | 2.1.5 | 2.x 统一了 sync/async API；生态最成熟；支持 pgvector 扩展类型 | SQLModel 更轻但与 SQLAlchemy 2.x 的高级特性有摩擦，暂不引入 |
| 数据库 | PostgreSQL + pgvector | 17+ | 一套库同时解决业务数据与向量检索；客服知识库规模（百万级 chunk 以下）pgvector 完全够用，少维护一个组件 | 专用向量库（Qdrant/Milvus）：数据量到千万级 chunk 或需要高级混合检索/过滤时再拆，检索代码经 Repository 模式隔离，可平滑迁移（演进路径见 9.4） |
| 缓存/队列 | Redis | 7+ | 会话热数据、LLM 结果缓存、限流计数、异步任务队列 | 无 |
| 异步任务 | arq | 最新稳定版 | 基于 asyncio + Redis，与 FastAPI 异步栈同模型，学习成本低 | Celery：生态更成熟但同步模型，与 async 栈割裂；本期用 arq，规模上来后可换 |
| 迁移工具 | Alembic | 最新稳定版 | SQLAlchemy 官方配套 | 无 |
| 认证 | PyJWT + bcrypt | 最新稳定版 | JWT 无状态适配前后端分离；access + refresh 双 token | 第三方 auth 服务（Keycloak）过重，本期不做 |
| LLM 编排 | LangChain 1.x + LangGraph 1.2 | 1.4.6 / 1.2.16 | 1.x 后底层即 LangGraph：持久化执行、流式、human-in-the-loop 开箱即用，客服场景的"转人工中断/恢复"正是其强项 | **风险对冲**：所有模型调用收敛到自研 `LLMClient` 适配层（OpenAI 兼容协议），LangChain 只做编排；即使未来弃用 LangChain，核心链路不受影响 |
| LLM 接入协议 | OpenAI 兼容 API | — | 主流模型（OpenAI / DeepSeek / 通义 / 智谱 / 本地 vLLM）均提供该端点，一套代码可切换供应商，避免锁定；API Key 通过环境变量注入，禁入代码与日志 | 直连各家私有 SDK 会写死供应商 |
| 结构化日志 | structlog | 最新稳定版 | JSON 结构化日志，便于采集检索 | 标准 logging 也可，但结构化成本后置 |
| 对象存储 | MinIO（S3 兼容） | 最新稳定版 | 存文档原件与音频附件；协议与云 OSS/COS 一致，迁移成本为零 | 直接写本地文件系统无法多副本共享，是常见坑 |
| 邮件渠道收发 | aiosmtplib（发）+ 标准库 imaplib/email（收） | 最新稳定版 | 邮件是**无资质门槛**的外部渠道，适合作为首个真实多渠道落地验证 | 收发同步 IO 在线程池执行，不阻塞事件循环 |
| 语音识别/合成 | 走 `AI_*_TRANSCRIPTION` / `AI_*_TTS` 网关接口，本地 faster-whisper 作为备选 | — | 语音消息链路核心；优先走网关免本地算力 | 本地 Whisper 需 GPU，配置缺失时降级为"当前不支持语音"，不静默失败 |
| 关键词召回 | PostgreSQL 全文检索（tsvector + GIN） | 随 PG | BM25 类关键词召回，零新增依赖；RRF 融合在应用层/SQL 实现 | 不引入独立搜索引擎，规模扩大再评估 |

**LLM 配置约定（环境变量，已定名）**

| 变量 | 用途 | 必需性 |
|---|---|---|
| `AI_API_KEY` / `AI_BASE_URL` / `AI_MODEL` | 对话模型接入 | 必需 |
| `AI_EMBEDDING_API_KEY` / `AI_EMBEDDING_BASE_URL` / `AI_EMBEDDING_MODEL` | 嵌入模型接入 | 可选，缺省回退到对话模型端点 |
| `AI_RERANK_MODEL` | 检索重排模型 | 可选，缺省关闭 rerank（退化为向量+BM25 RRF） |
| `AI_TRANSCRIPTION_MODEL` / `AI_TTS_MODEL` | 语音转写与语音合成 | 可选，缺省关闭语音能力并在接口显式报错 |
| `WECHAT_APP_ID` / `WECHAT_APP_SECRET`、`DINGTALK_APP_KEY` / `DINGTALK_APP_SECRET` | 渠道凭证 | 可选，缺省该渠道自动禁用（不报错、不影响其他渠道） |
| `SMTP_HOST` / `SMTP_USERNAME` / `SMTP_PASSWORD`、`IMAP_HOST` / `IMAP_USERNAME` / `IMAP_PASSWORD` | 邮件收发凭证 | 邮件渠道启用时必需 |

- 供应商“Agnes”按 OpenAI 兼容端点接入（`AI_BASE_URL` 指向其网关即可），名称不影响实现，换供应商只改变量值
- 密钥只进环境变量/密钥管理，禁入代码、日志、前端产物；`.env` 不入库（`.gitignore` + 提供 `.env.example`）

### 3.2 前端

| 组件 | 选型 | 版本 | 为什么选它 | 取舍 / 备选 |
|---|---|---|---|---|
| 框架 | **Vue 3** | 3.5.44 | Composition API + TS 类型推导好；中文文档与生态完善；SFC 单文件组件内聚性强 | React：生态更大但 Vue 3 的类型体验和上手成本对国内团队更优（项目既定选择） |
| 构建 | Vite | 最新稳定版 | Vue 官方标配，冷启动与 HMR 快 | webpack 已无必要 |
| 语言 | TypeScript strict | 最新稳定版 | 前后端契约（OpenAPI 生成类型）必须 | 无 |
| 路由 | Vue Router | 4.x | 官方 | 无 |
| 状态管理 | Pinia | 最新稳定版 | 官方推荐，TS 支持优于 Vuex | Vuex 已停止大版本演进 |
| UI 组件库 | Element Plus | 最新稳定版 | 管理后台表格/表单/弹窗开箱即用，中文友好 | Naive UI / Ant Design Vue 均可，Element Plus 后台组件覆盖最全 |
| Markdown 渲染 | markdown-it + highlight.js | 最新稳定版 | LLM 回答含代码/表格时必须安全渲染（禁用原始 v-html，防 XSS） | 无 |
| HTTP | axios + fetch 流式 | — | 普通请求用 axios；**SSE 流式用原生 fetch + ReadableStream**（EventSource 不支持 POST 且无法自定义 header 带 token） | 无 |
| 录音/播放 | MediaRecorder +原生音频播放 | — | 语音消息采集、时长限制、录音中状态反馈 | 无 |
| 代码规范 | ESLint + Prettier | 最新稳定版 | 与后端 ruff 对齐 | 无 |
| 提交规范 | husky + lint-staged + commitlint（Conventional Commits） | 最新稳定版 | 提交信息遵循 `feat/fix/docs/refactor/perf/test/chore` 前缀，可读可追溯，后续可自动生成 CHANGELOG 与版本号；lint-staged 只校验暂存文件，提交不拖慢 | 无 |
| 单元测试 | Vitest | 最新稳定版 | 与 Vite 同源配置零成本 | Jest 在 ESM 下配置繁琐 |

### 3.3 工程化与部署

| 组件 | 选型 | 理由 |
|---|---|---|
| 依赖管理 | uv（后端）/ pnpm（前端） | uv 解析与安装速度快一个数量级；pnpm 省磁盘且严格 node_modules |
| 代码检查 | ruff（lint + format 一体） | 替代 flake8/black/isort 三件套 |
| 类型检查 | mypy | 后端静态类型门禁 |
| 测试 | pytest + httpx（异步测试） | FastAPI 官方文档推荐组合 |
| pre-commit 钩子 | pre-commit（ruff + mypy + eslint） | 入库前统一拦截 |
| 提交规范 | Conventional Commits（前后端统一，commitlint 校验） | 提交信息 `feat/fix/docs/refactor/perf/test/chore` 前缀，可读可追溯，支持自动生成 CHANGELOG |
| CI | GitHub Actions | lint + 类型 + 测试 + 构建镜像 |
| 部署 | Docker Compose（api / worker / postgres / redis / nginx） | 单机起步；服务拆分已完成，后续上 K8s 只改编排不动代码 |

---

## 4. 代码结构设计

### 4.1 仓库布局（前后端分离，同仓不同目录）

```
py-web-all/
├── docs/                     # 设计文档（本文档所在）
├── backend/
│   ├── app/
│   │   ├── main.py           # FastAPI 装配入口
│   │   ├── api/v1/           # 路由：auth / conversations / chat / knowledge / admin
│   │   ├── core/             # 配置、安全、日志、异常处理
│   │   ├── db/               # engine、session、models、migrations
│   │   ├── schemas/          # Pydantic 请求/响应模型
│   │   ├── services/         # 业务逻辑（chat / knowledge / retrieval / handoff）
│   │   ├── channels/         # ChannelAdapter 实现（web / email / wechat / dingtalk）
│   │   ├── speech/           # ASR / TTS 封装与音频时长、格式校验
│   │   ├── llm/              # LLMClient 适配层（唯一允许触碰模型 SDK 的目录）
│   │   ├── workers/          # arq 任务定义（解析 / 向量化 / ASR / TTS / 清理）
│   │   └── tests/
│   ├── pyproject.toml        # uv 管理
│   └── Dockerfile
├── frontend/
│   ├── src/
│   │   ├── api/              # 按 OpenAPI 生成的客户端 + 手写补充
│   │   ├── views/            # 用户端 / 管理后台页面
│   │   ├── components/       # 聊天窗口、消息气泡、知识库表单等
│   │   ├── stores/           # Pinia（auth / chat / knowledge）
│   │   ├── composables/      # useSSE 等复用逻辑
│   │   └── router/
│   ├── package.json          # pnpm 管理
│   └── Dockerfile
├── deploy/
│   ├── docker-compose.yml
│   └── nginx.conf
└── .github/workflows/ci.yml
```

### 4.2 关键分层规则

- `api/` 只做：参数校验 → 调 service → 组装响应；**不写业务逻辑**
- `services/` 不 import FastAPI 对象（保持可测试）
- 渠道 SDK / 邮件库 / 语音 SDK 只允许出现在 `channels/` 与 `speech/` 内，业务层只见统一模型
- 租户上下文由中间件注入，service 层通过依赖获取，禁止从请求参数取 `tenant_id`
- `llm/` 是唯一依赖 LLM SDK 的目录，对外暴露统一接口：

```python
class LLMClient(Protocol):
    async def chat_stream(self, messages: list[Message], **kw) -> AsyncIterator[str]: ...
    async def embed(self, texts: list[str]) -> list[list[float]]: ...
```

---

## 5. 核心流程设计

### 5.1 一次 AI 对话（流式）

```
用户前端                FastAPI                 Chat编排            LLM
  │  POST /chat/stream    │                      │                  │
  │──────────────────────▶│ 鉴权+参数校验         │                  │
  │                       │ 加载会话历史(Redis→PG)│                  │
  │                       │─────────────────────▶│                  │
  │                       │                      │ 知识库检索(top-k) │
  │                       │                      │ 组装 prompt       │
  │                       │                      │ chat_stream      │
  │                       │                      │─────────────────▶│
  │  SSE: token 增量      │                      │◀─────────────────│
  │◀──────────────────────│◀─────────────────────│ 逐 token 转发     │
  │  SSE: [引用来源]       │                      │                  │
  │◀──────────────────────│                      │                  │
  │  SSE: [DONE]          │ 落库 message + 用量   │                  │
```

要点：

- 传输用 **SSE** 而非 WebSocket：一问一答的生成场景 SSE 足够，天然复用 HTTP 基础设施（Nginx、限流、鉴权），断线由前端自动重连补拉历史即可；坐席端通知同样走 SSE（见 5.4），WebSocket 仅在出现高频双向交互需求时才引入
- 回答完成后**一次性落库**（user message 即时落，assistant message 收齐后落），附 tokens 用量与耗时，供成本核算
- 引用来源随回答尾部以独立 SSE 事件返回，前端可渲染"依据了哪几条知识"
- **所有渠道与语音消息都进入这一条编排链路**：语音先经 ASR 转成文本，渠道差异在适配器内消化，编排层零分支

### 5.2 知识库入库（异步）

```
上传文档 → 预签名直传对象存储 → API 立即返回 document_id
        → arq 任务：解析(PDF/DOCX/MD) → 清洗 → 分块(策略可配) → 调 embed
        → 写 pgvector 向量索引 + tsvector 关键词索引 → 状态回写 (processing / done / failed)
```

要点：文档解析可能耗时数十秒，**绝不能阻塞 HTTP 请求**，这是引入异步队列的核心原因；大文件走预签名直传，应用服务器不过流量。

### 5.3 转人工

1. AI 触发条件（置信度低 / 用户显式要求 / 工具调用判定）→ 会话状态置 `pending_handoff`
2. 生成 handoff ticket，通过 SSE 推送至坐席端（见 5.4）
3. 坐席接管后会话锁定，AI 停止自动回复，历史对坐席可见
4. LangGraph 的 interrupt/checkpoint 机制为后续"AI 辅助坐席"预留能力

### 5.4 坐席端实时通知（SSE 主方案，轮询降级）

**结论：SSE 单向推送；轮询仅作降级兜底；不上 WebSocket。**

坐席端需要接收的通知（新会话分配、访客新消息、会话状态变更）本质是**服务器到坐席的单向推送**；坐席发消息走普通 HTTP API 即可，无需双向连接。

| 方案 | 定位 | 理由 |
|---|---|---|
| **SSE** | 正式方案 | 纯 HTTP，服务端几行代码即可推送；浏览器原生断线自动重连；标准 HTTP 流量，Nginx/防火墙/企业代理零改造 |
| 轮询 | 降级兜底 | 仅当 SSE 不可用（极老旧环境）或极早期验证流程时启用；无效请求多、延迟不可控，不作正式方案 |
| WebSocket | 不做 | 需处理协议升级、连接保活、有状态负载均衡、自行实现重连/心跳/消息补偿；本期无高频双向或二进制传输需求，属过度设计 |

触发 WebSocket 迁移的条件（未来）：坐席间协作、实时语音状态同步、协同编辑、二进制数据传输。

实现要点：

- 端点：`GET /api/v1/agent/notifications/stream`（坐席 JWT 鉴权，EventSource 可用 GET）
- 推送通道：业务事件（新 ticket、访客消息、访客登录）写入 Redis Pub/Sub，API 实例订阅后经 SSE 转发，天然支持多 worker 水平扩展
- 前端：`EventSource` + 失败降级为 10s 轮询 `GET /api/v1/agent/notifications`
- 心跳保活：服务端每 15-30s 发送 SSE 注释行（`: ping`），防止 Nginx/企业代理按空闲连接切断；间隔可配
- Nginx 需关闭该路径的响应缓冲（`proxy_buffering off`）并放宽超时

### 5.5 匿名访客会话与登录合并

**核心原则：认证系统管"你是谁"，业务系统管"你在本次会话里做了什么"。访客阶段没有认证，只有临时会话标识 `guest_id`。**

```
① 访客进入聊天窗口
   后端签发 guest_id (UUID) → 经 cookie 返回
   访客的消息/会话全部挂 guest_id 名下（guest_sessions 表）
   此时不触及用户系统

② 访客完成登录/注册
   前端同时持有两个凭证：
   - access token（证明"他是谁"）
   - guest_id（关联"他之前聊了什么"）

③ 调用合并接口 POST /api/v1/auth/merge-guest
   后端【同时校验 access token + guest_id】：
   - 只信 token：可能把别人的访客数据合并过来
   - 只信 guest_id：可伪造 guest_id 劫持会话
   校验通过 → 会话所有权转移至 user_id，原 guest_id 作废
```

配套约束：

- **cookie 下发（已定）**：`guest_id` 通过 `Set-Cookie` 返回，属性 `HttpOnly + Secure + SameSite=Lax + Path=/`，`Max-Age` 30 天与 `guest_sessions.expires_at` 对齐；不用 localStorage（XSS 可直接窃取），也不暴露给 JS 读取
- **合并接口幂等**：merge-guest 重复提交或并发提交时，guest_id 已作废则直接返回成功（不报错），所有权转移与 guest 作废在同一事务内完成，杜绝竞态双写
- **TTL 30 天**：arq 定时任务清理未转化的僵尸 guest 会话，防止数据无限膨胀
- **坐席端感知访客登录**：合并完成后经同一套 SSE（5.4）向坐席推送"访客已登录"状态事件，提示当前对话对象已变为可识别用户，可关联历史订单
- guest 阶段同样受限流保护（按 guest_id 计数），防 LLM 额度被匿名流量打爆

---

## 6. 数据模型概要

核心表（字段精简示意，详细 DDL 在实现阶段用 Alembic 管理；**所有租户级表均带 `tenant_id` 并开启 RLS**）：

| 分组 | 表 | 关键字段 | 说明 |
|---|---|---|---|
| 租户 | `tenants` | id, name, slug, status, quota(jsonb) | 租户主体；slug 用于子域名解析 |
| 租户 | `tenant_members` | tenant_id, user_id, role(owner/admin/agent) | 租户内成员与角色 |
| 租户 | `users` | id, email, password_hash, role(customer/agent/admin) | 全局身份，归属由 `tenant_members` 决定 |
| 访客 | `guest_sessions` | guest_id(UUID PK), tenant_id, status, created_at, expires_at, merged_user_id | 访客临时会话标识；TTL 30 天，合并或过期后作废 |
| 对话 | `conversations` | id, tenant_id, channel, owner_type(user/guest), user_id, guest_id, status(ai/pending_handoff/human/closed), title, voice_reply_enabled | 一个会话=一个聊天窗口；owner 二选一，合并时由 guest 转为 user |
| 对话 | `messages` | id, conversation_id, message_type(text/audio/video), role(user/assistant/system), content, source(voice/email/...), attachments(jsonb), tokens, latency_ms, sources(jsonb) | 追加式，不改不删；语音消息存对象存储 key 与时长 |
| 渠道 | `channel_configs` | tenant_id, channel, enabled, credentials(jsonb, 加密), rate_limit | 每租户每渠道的配置与凭证 |
| 渠道 | `channel_messages` | tenant_id, channel, external_msg_id, conversation_id, direction | 唯一键 `(tenant_id, channel, external_msg_id)` 做幂等去重 |
| 知识库 | `knowledge_bases` | id, tenant_id, name, description | |
| 知识库 | `documents` | id, kb_id, filename, object_key, status, error_msg | 入库状态机；原件存对象存储 |
| 知识库 | `chunks` | id, document_id, tenant_id, content, embedding(vector), tsv(tsvector), chunk_strategy, metadata | 向量 HNSW + 关键词 GIN 双索引 |
| 运营 | `handoff_tickets` | id, tenant_id, conversation_id, agent_id, status, reason | 转人工工单 |
| 运营 | `llm_usage_logs` | id, tenant_id, model, prompt_tokens, completion_tokens, cost, conversation_id | 成本核算，按租户聚合 |

检索策略见 9.4：向量（HNSW）与关键词（GIN tsvector）双路召回后 RRF 融合，按 `tenant_id` + `kb_id` 过滤。

---

## 7. API 设计约定

- 前缀 `/api/v1`，REST 风格资源 + 个别流式端点
- 流式端点：
  - `POST /api/v1/conversations/{id}/messages/stream`（访客/用户对话，`text/event-stream`）
  - `GET /api/v1/agent/notifications/stream`（坐席通知，EventSource）
- 访客相关：
  - `POST /api/v1/guest/sessions` 签发 guest_id（Set-Cookie）
  - `POST /api/v1/auth/merge-guest` 登录后双凭证合并（access token + guest_id）
- 租户相关（超管/租户管理员）：
  - `/api/v1/tenants`、`/api/v1/tenants/{id}/members`、`/api/v1/tenants/{id}/quota`、`/api/v1/tenants/{id}/channels`
  - 租户上下文来源：子域名（生产）或 `X-Tenant-Id`（本地/内部调用），统一在中间件解析并注入
- 渠道相关：
  - `POST /api/v1/channels/{channel}/webhook` 各平台回调入站（验签 + 幂等）
- 语音相关：
  - `POST /api/v1/uploads/presign` 获取对象存储预签名上传地址（**大文件/音频直传对象存储，不过应用服务器**）
  - `POST /api/v1/messages/{id}/tts` 对指定回复生成语音
- 统一错误体：`{ "code": "VALIDATION_ERROR", "message": "...", "detail": {...} }`
- 分页：cursor 分页（`limit` + `next_cursor`），会话消息量大会持续增长
- 前端类型：由 FastAPI 自动生成的 OpenAPI schema 经 `openapi-typescript` 生成 TS 类型，**契约单一来源**

---

## 8. 安全设计

| 面 | 措施 |
|---|---|
| 认证 | JWT access(短) + refresh(长，可吊销)；密码 bcrypt |
| 访客合并 | merge-guest 必须同时校验 access token + guest_id；guest_id 仅作会话标识，不构成认证凭证；合并成功后立即作废 |
| 输入 | 后端独立校验（Pydantic），前端校验仅优化体验 |
| 注入 | SQL 全参数绑定；Markdown 渲染禁用原始 HTML |
| 密钥 | `AI_API_KEY` 等模型密钥走环境变量，禁入代码与日志 |
| 敏感数据 | 会话内容可能含 PII：日志脱敏，`llm_usage_logs` 不存原文 |
| 限流 | Nginx 层 IP 限流 + Redis 计数按用户/接口限流，防 LLM 额度被打爆 |
| 越权 | 每个资源查询强制带 owner/tenant 条件，集中到 service 层处理 |
| 租户隔离 | 全表 `tenant_id` + PostgreSQL RLS 强制；连接池借出前 `SET LOCAL app.tenant_id`；超管操作走独立数据库角色，不与租户共用连接 |
| 渠道回调 | 微信/钉钉按平台签名算法验签；邮件以 SPF/DKIM 校验结果判定来源；验签失败直接拒绝并记安全日志 |
| 文件上传 | 预签名直传对象存储（应用服务器不过流量）+ MIME/大小/后缀白名单校验；存储桶私有，下载签发短时效 URL |

---

## 9. 高级能力完整设计（本期实现）

> 对应 1.2 的四项能力：本期全部实现，以下为实现设计与关键取舍。

### 9.1 多渠道接入

设计目标：**一套内部消息模型，渠道差异全部收敛在适配器内**。核心对话链路（Chat 编排、RAG、转人工）对渠道无感知。

- 适配器接口：

```python
class ChannelAdapter(Protocol):
    channel: str  # "web" | "email" | "wechat" | "dingtalk"

    async def parse_inbound(self, raw: dict) -> InboundMessage:
        """渠道原始报文 → 内部统一消息"""

    async def send_outbound(self, conversation_id: str, message: OutboundMessage) -> None:
        """内部消息 → 渠道协议发送"""
```

- 内部统一消息模型 `InboundMessage` / `OutboundMessage` 与渠道无关（sender_id、content、message_type、attachments、external_msg_id）
- `conversations.channel` 标记来源；新增 `channel_messages` 表，以 `(tenant_id, channel, external_msg_id)` 唯一键做**幂等去重**——各平台都会重推回调，不去重必然导致重复回复
- **本期实现四个适配器**：

| 适配器 | 入站 | 出站 | 说明 |
|---|---|---|---|
| `WebAdapter` | 站内 SSE | SSE 推送 | 首发渠道 |
| `EmailAdapter` | IMAP 轮询收信（线程池执行） | SMTP 发信 | 无资质门槛，作为首个外部渠道落地验证；按 `Message-ID` 去重，按主题前缀 `[工单号]` 关联会话 |
| `WechatAdapter` | 公众号/企业微信回调验签 | 客服消息 API | 报文校验后 5s 内响应、实际处理转异步 |
| `DingTalkAdapter` | 机器人/应用回调验签 | 机器人发消息 API | 按平台限频发送，超限入重试队列 |

- 入站统一入口 `POST /api/v1/channels/{channel}/webhook`：验签 → 反序列化 → 幂等检查 → 落 `channel_messages` → 转内部消息 → 进入同一条对话链路
- 出站 `ChannelRouter` 按 `conversation.channel` 路由；坐席回复自动回到原渠道
- 渠道配置存 `channel_configs` 表（按租户），凭证优先取环境变量；未配置的渠道自动禁用并给出明确提示，不影响其他渠道
- **外部依赖提醒**：微信/钉钉回调需企业资质 + 回调域名备案，这是平台侧前置条件而非代码工作项，M4 启动前必须确认

### 9.2 多租户隔离

- 全表带 `tenant_id`：`tenants` / `tenant_members` / `users` / `guest_sessions` / `conversations` / `messages` / `knowledge_bases` / `documents` / `chunks` / `channel_configs` / `handoff_tickets` / `llm_usage_logs`
- **本期直接开启 PostgreSQL 行级安全（RLS）**：每张租户表 `ENABLE ROW LEVEL SECURITY` + 基于 `current_setting('app.tenant_id')` 的策略；请求事务开启时（连接从池中借出后）由 SQLAlchemy 事件钩子执行 `SET LOCAL app.tenant_id`——`SET LOCAL` 仅在事务内有效，事务结束自动失效，天然防串号
- 应用层仍做**显式 tenant 过滤**（双保险，且报错信息对开发者可读）；跨租户访问尝试记安全日志
- 租户管理 API：租户 CRUD、成员与角色、配额（消息数 / 并发 / 存储 / Token 预算）、启停
- 租户级配置：模型 Key 与模型名、知识库隔离、限流阈值、渠道启用与凭证
- 访客合并约束：`guest_id` 合并到的 `user_id` 必须与 guest 会话同租户，防止跨租户串数据
- 迁移路径：本期即 RLS，避免"先应用层过滤、后补 RLS"的历史包袱

### 9.3 语音客服（语音消息完整闭环）

目标：**语音消息完整实现**，实时全双工通话不做（理由与重启条件见 1.2）。

```
用户录音(前端 MediaRecorder)
  → 上传音频 → MinIO 存对象
  → arq 任务调 ASR 转写
  → 以文本消息进入既有对话链路（完全复用，零改动）
  → 回答文本；若会话开启语音回复 → 调 TTS 生成音频 → MinIO
  → SSE 推送音频 URL → 前端播放
```

- 数据模型：`messages.message_type` = `text/audio/video`，`attachments(jsonb)` 存对象存储 key、时长、大小；保留原始音频以便复听与人工质检
- 前端：录音组件 + "识别中…"状态；识别/合成失败降级为纯文字并明确提示，不静默吞错
- 成本与体验控制：单次音频时长上限（可配，默认 60s）、大文件分片上传、ASR/TTS 独立超时与重试
- 演进路径（未来）：实时通话需引入 WebRTC SFU（如 LiveKit）+ 流式 ASR/LLM/TTS 全双工链路，届时只替换 5.1 的传输层，LangGraph 编排与知识库不变

**M5 实现说明（与上文流程图的差异，均为有意取舍）**

| 决策点 | 原计划 | 实际实现 | 理由 |
|---|---|---|---|
| 音频上传 | 预签名直传（`POST /uploads/presign`） | multipart 直传应用服务器（`POST /conversations/{id}/messages/audio`） | 单条上限 60s/8MB，中转成本可忽略；预签名需要对象存储支持（当前 local 后端做不到），两套上传路径会让前端与校验逻辑翻倍。接 MinIO 后再按需切换 |
| ASR 时机 | arq 任务异步转写 | **在 SSE 流内同步转写** | 转写结果就是"用户说的话"，异步只是把等待搬到别处，还会引入"消息转写中"的第三种状态同步；流内可以先发 `transcribing` 事件，失败也能用既有 error 事件收尾 |
| TTS 时机 | 任务 + 推送 URL | 流内 `done` 之后追加 `audio` 事件 | 单连接、无轮询；音频永远晚于文字，合成慢不阻塞阅读；失败只发 `audio_failed`，文字回答不受影响 |
| 音频访问 | MinIO 短时效签名 URL | 自签 JWT 短时令牌（`?token=`，默认 1 小时） | `<audio src>` 无法携带 Authorization 头，登录用户也没有访客 cookie；令牌只绑定单条消息 + 租户 |
| 时长校验 | 服务端统一校验 | wav 精确解析；ogg/webm/mp4 用客户端上报 + 大小与超时兜底 | 容器时长需要解析索引，为此引入依赖不划算；伪造上报的收益被"大小上限 + ASR 超时"限制住 |

语音能力的配置状态通过 `GET /api/v1/voice/capabilities` 暴露，前端据此禁用麦克风按钮，避免"录完 30 秒才告知不支持"。

### 9.4 检索增强（引擎不自研，能力做全）

原则：**不造轮子**——引擎只用 pgvector，不实现相似度算法与索引结构；但**检索质量能力本期全部实现**。

完整链路：

```
查询 → 分块/向量化（离线）
     → 向量召回 top-50（pgvector HNSW）
     → 关键词召回 top-50（PostgreSQL tsvector + GIN）
     → RRF 融合去重
     → rerank 模型重排（未配置则跳过）
     → 取 top-k 组装 prompt 并附引用来源
```

- 检索入口抽象，引擎可换：

```python
class ChunkRepository(Protocol):
    async def search(
        self, query_vector: list[float], kb_ids: list[str], top_k: int
    ) -> list[Chunk]: ...
```

- 分块策略可配置（固定窗口 / 按标题层级 / 语义分块），策略与来源信息记入 chunk 元数据，供引用溯源
- 评估集：维护"查询 → 期望 chunk"样本集，调整分块、换嵌入模型、改 top-k、启用 rerank 都必须跑回归，指标为召回率与引用准确率
- 规模上限应对：chunk 到千万级或需要复杂过滤时，迁移 Qdrant / Milvus——仅替换 `ChunkRepository` 实现，业务层不动

---

## 10. 非功能性需求

| 项 | 目标 / 方案 |
|---|---|
| 首 token 延迟 | 依赖模型侧，目标 P95 < 3s；prompt 缓存 + 检索 top-k 控制上下文长度 |
| 并发 | uvicorn 多 worker（CPU 核数参考）+ 全异步 IO；LLM 等待时间不占worker |
| 可观测 | structlog JSON 日志 + request_id 全链路透传；LLM 调用带 trace_id |
| 降级 | LLM 超时/失败 → 明确错误提示 + 可转人工；知识库检索失败 → 纯模型回答并标注"无知识库依据"；ASR/TTS 失败 → 降级为纯文字并提示；渠道凭证缺失 → 该渠道禁用而非报错 |
| 多租户开销 | RLS 为每行附加条件，所有租户级查询索引须以 `tenant_id` 打头；RLS 依赖事务内 `SET LOCAL`，连接不可跨事务复用，用连接池管理 |
| 语音链路延迟 | ASR 目标 P95 < 3s，整轮（转写+生成+合成）P95 < 8s；超阈值先推文本、音频后补 |
| 渠道回调时效 | 平台要求 5s 内响应：验签 + 落库同步完成，AI 处理全部异步；幂等键保证重复推送不产生重复回复 |
| 邮件收取 | IMAP 轮询间隔 30-60s（可配），单次批量上限，避免高频轮询被邮件服务商判为异常 |
| 备份恢复 | PostgreSQL 每日全量 + WAL 连续归档（RPO ≤ 5min）；MinIO 开启版本化；恢复演练纳入 M8，备份不可只建不验 |
| 成本 | 每次调用记 tokens；后台看板按天/模型聚合 |

---

## 11. 实施路线图

| 阶段 | 内容 | 产出 |
|---|---|---|
| M0 脚手架 | uv/pnpm 工程、环境变量骨架（`AI_*` 配置 + `.env.example`）、Docker Compose（PG+pgvector / Redis / MinIO）、CI、目录骨架、OpenAPI 联调 | 前后端 hello + 健康检查 |
| M1 多租户与账号 | `tenants` / `tenant_members` 表、租户上下文中间件、**RLS 策略与首个迁移**、租户管理 API、JWT 登录（租户内成员与角色） | 租户隔离可验证（跨租户访问被拒） |
| M2 基础对话 | guest_id 签发与登录合并、会话 CRUD、SSE 流式对话（无知识库）、消息落库 | 可用的流式聊天 Demo（含匿名访客） |
| M3 知识库与检索 | 上传、arq 异步解析向量化、分块策略、向量+BM25+RRF+rerank、RAG 回答带引用、检索评估集 | 高质量 AI 客服闭环 |
| M4 多渠道 | `ChannelAdapter` 抽象 + Web/Email/微信/钉钉四适配器、幂等去重、`ChannelRouter` 出站路由 | 多渠道统一接入跑通 |
| M5 语音客服 | 录音组件、预签名上传、ASR 转写、TTS 合成、消息类型扩展 | 语音消息完整闭环 |
| M6 人工接管 | 转人工工单、坐席端 SSE 通知（含访客登录事件、轮询降级）、会话锁定、guest 会话 TTL 清理任务 | 完整客服工作流 |
| M7 后台与观测 | 租户/成员/知识库/渠道/会话管理后台、用量成本看板（按租户聚合）、日志与告警 | 可运营、可多租户交付 |
| M8 加固 | 压测（含 RLS 开销）、限流调优、检索与回答质量回归、渠道异常演练 | 生产就绪 |

实现阶段建议按 FastAPI 官方中文教程（https://fastapi.tiangolo.com/zh/tutorial/）的路径推进：先路径参数/查询参数/请求体，再依赖注入、安全、测试，与本架构的 service 分层自然对齐。

---

## 12. 风险与开放问题

| 风险 | 影响 | 应对 |
|---|---|---|
| LLM 供应商锁定 | 换供应商成本高 | OpenAI 兼容协议 + LLMClient 适配层收敛 |
| 幻觉 / 错误回答 | 客服场景直接面对终端用户 | RAG 强制引用；无依据时显式声明；低置信度转人工 |
| LangChain 版本变动快 | 升级破坏性 | 锁版本；编排与模型调用解耦 |
| pgvector 规模上限 | 检索变慢 | 已用 `ChunkRepository` 抽象隔离，监控 chunk 量，超千万级评估迁 Qdrant/Milvus |
| SSE 连接稳定性 | 弱网下断流 | 前端断线重连 + 从服务端补拉完整消息 |
| guest_id 伪造 / 会话劫持 | 匿名会话被冒领 | guest_id 不构成认证凭证；合并时双凭证校验；guest 阶段同样限流 |
| 数据合规 | 会话含用户隐私，出境取决于模型供应商 | 部署前确认供应商数据政策；必要时选国内端点或本地模型 |
| 成本失控 | 恶意刷量 | 限流 + 用量告警 + 单用户/单租户配额 |
| 微信/钉钉资质与备案 | 渠道无法上线，阻塞 M4 | 前置确认企业资质与回调域名备案；先以 Email 渠道跑通多渠道路径，凭证缺失时渠道自动禁用 |
| RLS 性能与连接管理 | 租户过滤拖慢查询，或连接复用导致租户串号 | 索引以 `tenant_id` 打头；`SET LOCAL` 与事务严格绑定；压测用例必须覆盖跨租户访问 |
| ASR/TTS 成本与准确率 | 语音链路费用上升、误识别引发误解 | 单次时长上限、租户配额、识别失败降级为文字并提示 |
| 邮件被判异常发送 | Email 渠道进垃圾箱或被封 | SPF/DKIM 配置、轮询间隔可配、控制单次批量；必要时改用平台官方 API |

## 13. 已确认决策

| 议题 | 结论 |
|---|---|
| 本期实现范围 | **多租户（RLS）、多渠道（Web/Email/微信/钉钉）、语音客服（ASR/TTS 消息闭环）、检索增强（向量+BM25+RRF+rerank）本期全部完整实现**，路线图 M1/M3/M4/M5 交付 |
| 明确不做 | 实时全双工语音通话（WebRTC + SFU + 流式 ASR/LLM/TTS）；不自研向量检索引擎（引擎用 pgvector，能力用混合检索 + rerank 做全） |
| LLM 供应商接入 | 环境变量定名 `AI_API_KEY` / `AI_BASE_URL` / `AI_MODEL`；"Agnes" 按 OpenAI 兼容端点接入（网关名不影响实现）。嵌入模型用 `AI_EMBEDDING_*` 独立配置，缺省回退对话端点 |
| 渠道凭证策略 | 环境变量优先、缺省自动禁用该渠道；微信/钉钉的资质与备案为平台侧前置条件，不阻塞代码实现 |
| guest_id 存储 | 采用 cookie（`HttpOnly + Secure + SameSite=Lax`，30 天过期与 DB TTL 对齐），不用 localStorage |
| 坐席端实时通知 | 用 SSE，轮询仅作降级兜底，不引入 WebSocket |
| 匿名访客会话 | 必须做；guest_id 仅为会话标识（非认证凭证），登录后双凭证校验后合并 |

后续如需调整走第 11 章实施路线图的迭代节奏，不在 M0 阶段变更已定决策。

---

## 14. 开工前置检查清单（完整范围）

**环境与依赖**

- [ ] `.env` 填入 `AI_API_KEY` / `AI_BASE_URL` / `AI_MODEL`，连通性验证通过（一次最小 chat + 一次 embed）
- [ ] 可选能力按需配置并验证：`AI_RERANK_MODEL`、`AI_TRANSCRIPTION_MODEL` / `AI_TTS_MODEL`
- [ ] `.env.example` 入库，`.env` 加入 `.gitignore`

**基础设施**

- [ ] 本地 Docker 可启动 PostgreSQL（pgvector 镜像）+ Redis + MinIO
- [ ] 宿主机端口错峰映射（本机 5432/6379/6381 已被其他项目容器占用）：PostgreSQL → `55432:5432`，Redis → `56379:6379`，MinIO → `9000:9000`（API）/ `9001:9001`（控制台）
- [ ] 首个 Alembic 迁移包含全部表（`tenants` / `tenant_members` / `users` / `guest_sessions` / `conversations` / `messages` / `knowledge_bases` / `documents` / `chunks` / `channel_configs` / `channel_messages` / `handoff_tickets` / `llm_usage_logs`）
- [ ] RLS 策略已随首个迁移启用，索引以 `tenant_id` 打头

**外部前置（不阻塞 M0，但阻塞对应里程碑上线）**

- [ ] 微信/钉钉：企业资质与回调域名备案是否具备（决定 M4 能否全量上线）
- [ ] 邮件：发信域名的 SPF / DKIM 是否配置（决定 Email 渠道能否进投递箱）
- [ ] 对象存储：生产环境桶名、访问凭证、生命周期策略（音频/文档留存期）
