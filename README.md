# 智能客服 AI 系统（AICS）

![alt text](image.png)
![alt text](image-1.png)

一套**可私有化部署、按租户强隔离**的 AI 客服平台：网页/邮件/微信/钉钉统一接入，
知识库增强（RAG）自动应答，答不上来时按触发词或非工作时间转人工，
并用**命中率分析、质检抽检、敏感词过滤**把 AI 的短板变成可运营的待办。

后端 FastAPI（异步全链路）+ PostgreSQL 16 / pgvector + Redis(arq)，
前端 Vue 3 + TypeScript + Element Plus。

---

## 目录

- [一、整体架构](#一整体架构)
- [二、核心亮点](#二核心亮点)
- [三、后端](#三后端)
- [四、前端](#四前端)
- [五、功能矩阵](#五功能矩阵)
- [六、数据模型](#六数据模型)
- [七、部署](#七部署)
- [八、开发与测试](#八开发与测试)

---

## 一、整体架构

```
                 ┌──────────────────────── 前端（Vue 3 + TS）────────────────────────┐
                 │  AppLayout（14 个菜单，按角色可见）                                │
                 │  工作台 · 在线客服 · 知识库 · 人工坐席 · 留言箱 · 数据报表          │
                 │  快捷回复 · 客户管理 · 工单管理 · AI 优化 · 会话质检                │
                 │  渠道接入 · 系统设置 · 管理后台（含审计日志）                       │
                 │  Pinia(auth) · useChat(SSE) · useRecorder(语音) · markdown-it      │
                 └───────────────────────────┬──────────────────────────────────────┘
                                             │ 同源 /api/v1（Nginx 反代）
                                             ▼
┌─────────────────────────────── 后端（FastAPI，异步）────────────────────────────────┐
│ 中间件（由外到内）：RequestContext（租户/Request-Id/耗时） → CORS → 限流              │
│                                                                                     │
│ API 层   /api/v1/* ：auth tenants conversations messages knowledge agent             │
│                     channels guest voice usage stats insights quality quick-replies   │
│                     customers audit-logs                                             │
│                                                                                     │
│ 服务层   chat（对话编排 / SSE）  retrieval（混合检索 RRF）  ingestion（分块+嵌入）     │
│          handoff（转人工工单）   conversations  knowledge  channels  usage            │
│          stats  insights  quality  sensitive  tenant_settings  audit                 │
│                                                                                     │
│ 基础设施  SQLAlchemy 2.0 async + RLS   arq worker + Redis pubsub   存储(local/MinIO)  │
└───────────┬─────────────────────────┬──────────────────────────┬────────────────────┘
            ▼                         ▼                          ▼
   PostgreSQL 16               Redis 7/8                  AI 网关（OpenAI 兼容）
   + pgvector 向量            队列 / 实时广播 / 限流       对话 · 嵌入 · rerank · ASR · TTS
   20+ 张业务表，全表 RLS      Lua 原子计数                任意兼容端点（含自建）
```

一次用户提问的完整链路：

1. Nginx → FastAPI → 限流（按用户/访客）→ 日 Token 配额校验
2. 落库用户消息 → **敏感词过滤**（warn 留痕 / handoff 转人工 / block 拒收且不落库）
3. 会话状态判断：坐席已接管则 AI 闭嘴；命中触发词则建工单
4. 混合检索（向量 + 关键词 RRF 融合，可选 rerank；嵌入失败自动降级为纯关键词）
5. 流式生成（SSE：message → sources → delta… → done），引用随 `sources` 事件下发
6. 用量落账（tokens / 延迟 / 成本），实时事件经 Redis 广播给坐席端

---

## 二、核心亮点

### 1. 租户隔离做到数据库层（fail-closed）

不是"每个查询记得加 `where tenant_id`"，而是 **PostgreSQL RLS**：
每次事务开启时 `SELECT set_config('app.tenant_id', …, true)`，所有表按
`tenant_id = current_setting('app.tenant_id')` 建策略。
应用运行用非超管角色 `aics_app`，只有 Alembic 迁移用超管连接
（RLS 对表属主/超管无效，所以运行时**禁止**使用迁移连接）。

忘设租户上下文的后果是**查不到任何行**，而不是"查出全部数据"——
这是刻意选的失败方向：`app/db/rls.py` 注释写明 fail-closed。

### 2. 检索既能打，也知道什么时候打不动

| 能力 | 实现 |
|---|---|
| 混合召回 | 向量（pgvector）+ 关键词（`tsvector` + jieba `cut_for_search`）双路，各召回 50 |
| 融合 | **RRF**（`1/(60+rank)` 累加，k=60），不依赖两路分数可比 |
| 精排 | 可选 rerank（取融合结果前 `top_k×4` 重排），失败自动回退 RRF 顺序 |
| 降级 | 嵌入超时（30s 上限）/ 返回空 / 未配置 → 退化为纯关键词，并把 `degraded` 与原因随 SSE 带给前端 |
| 分块 | `by_heading`（按标题，默认）与 `fixed_window`（800/100）两种策略 |

关键点：**降级不静默**。前端能看到"这次没检索到"，用户不会被一句凭空生成的答案糊弄。

### 3. AI 运营闭环（这是多数同类产品没有的部分）

```
   用户提问 ──► AI 回答 ──► 引用了知识？
                              │否
                              ▼
                  「AI 优化」看板：命中率 + 未命中问题 Top（按提问次数聚合）
                              │
                              ▼
                      补充知识库 ──► 命中率上升
```

- **命中率口径**写死在后端：坐席回复（`source='agent'`）不计入 AI 能力；
  转人工固定文案既不算 AI 回答，也不把前面的提问误报成知识缺口。
- **未命中问题聚合**用 SQL 窗口函数（`LEAD` 取下一条消息）一次扫描完成，
  按提问原文合并次数，找出"问得最多却答不上来"的问题。
- **质检抽检**：手动随机抽样 + 按租户比例自动抽样；同一会话只抽检一次（唯一索引保证）；
  评分、问题标签、敏感词命中记录一起构成复盘面。
- **敏感词过滤**：服务端匹配（词库不下发浏览器，否则等于公开"什么词会被拦"），
  三种处置：`warn` 仅留痕 / `handoff` 转人工 / `block` 拒绝且**不落库**。

### 4. 实时链路不靠轮询

- 用户端：SSE 流式返回（`message`/`sources`/`delta`/`done`/`error`/`handoff`/`audio`…，含心跳与 `ready` 帧）
- 坐席端：Redis Pub/Sub（`aics:agent:{tenant_id}`），工单创建/认领/结束实时推送
- Redis 不可用时实时通道返回 **503 `REALTIME_UNAVAILABLE`**，而不是假装正常
- 多 worker 下实时事件仍可达（广播走 Redis，进程间无共享状态，所以 uvicorn 可以开多进程）

### 5. 成本可控、可观测

- 每次调用一行用量：`kind`（对话/嵌入/rerank/ASR/TTS）+ `model` + `prompt/completion tokens` + `units` + `cost`
- 成本**写入时按配置单价算好**（"只存不算"），价格缺省为 0 即只记用量
- 日 Token 配额超限 → 429 + 告警；管理员有今日/7 天/30 天用量与配额余量视图

### 6. 上线前该有的都备好了

一键生产栈（`deploy/`）：镜像构建 → 独立迁移服务 → API/Worker/Nginx → HTTPS 与 SSE 调优 →
定时备份与**可演练的恢复**；另附 Linux systemd 单元。详见[部署](#七部署)。

---

## 三、后端

技术栈：Python 3.12 · FastAPI · SQLAlchemy 2.0（async）· Alembic · PostgreSQL 16 + pgvector ·
Redis（arq 队列 / Pub/Sub / 限流）· OpenAI 兼容网关（对话、嵌入、rerank、ASR、TTS）·
存储可切换本地目录或 MinIO。

### 分层

| 层 | 目录 | 职责 |
|---|---|---|
| 入口 | `app/main.py`、`app/api/middleware.py` | 中间件装配、路由注册、异常处理器、lifespan |
| API | `app/api/v1/*.py` | 参数校验与鉴权依赖，不含业务规则 |
| 服务 | `app/services/*.py` | 业务规则（对话编排、检索、转人工、统计、质检…） |
| 数据 | `app/db/models/`、`app/repositories/` | 模型定义与查询封装 |
| 基础设施 | `app/core/`（config、security、rls、pubsub、rate_limit、logging） | 横切能力 |

### 关键设计取舍（都有注释说明原因）

| 主题 | 决策 | 为什么 |
|---|---|---|
| 租户上下文 | 事务级 `set_config`，不设即查不到 | 漏传上下文是"查空"而不是"越权"，fail-closed |
| 迁移 vs 运行连接 | 两套 `DATABASE_URL` | RLS 对超管无效，运行态禁止用超管连接 |
| 对话流 | 用户消息先落库再生成；转人工时 AI 闭嘴 | 坐席要能看到上下文；AI 插话会淹掉坐席回复 |
| 敏感词 block | 在落库**之前**拦截 | 拦了还留在历史里等于没拦 |
| 嵌入超时 | `min(ai_timeout, 30s)` | 免费网关会 504，宁可降级也不要前端一直转圈 |
| 限流 | Redis 固定窗口 + Lua 原子脚本，**fail-open** | Redis 挂了不能把全站打死 |
| 质检粒度 | 按会话不按消息 | 质检评的是一次服务过程；同会话重复抽检会让平均分失真 |
| 敏感词留痕 | 独立 `sensitive_hits` 表 | block 时消息没落库，挂在消息上会漏掉最该看的部分 |
| 抽样随机性 | `ORDER BY random()` 而非取最近 N 条 | 取最近会让冷启动阶段的会话永远抽不到 |

### 主要接口分组

`/api/v1`：`auth` `tenants` `conversations` `messages` `knowledge` `agent` `channels`
`guest` `voice` `usage` `stats` `insights` `quality` `quick-replies` `customers` `audit-logs`

### 异步任务（arq）

| 任务 | 触发 | 说明 |
|---|---|---|
| `process_document` | 上传/重新解析 | 解析 → 分块 → 嵌入 → 重建 chunks |
| `process_channel_inbound` | 渠道 Webhook 入站 | 与 Web 共用同一对话链路 |
| `poll_email_channels` | cron（每 60s，可配 30s） | 邮件渠道轮询 |
| `cleanup_guest_sessions` | cron（每日 03:00） | 清理过期访客会话 |

---

## 四、前端

技术栈：Vue 3.5（`<script setup>` + TS）+ Vite + Pinia + vue-router + Element Plus 2.11 +
`markdown-it` + `dompurify`（Markdown 渲染与消毒）+ axios + 原生 SSE 读取。

### 结构

```
src/
├─ api/          client.ts(axios 拦截器) · types.ts(与后端 Pydantic 模型对齐) · 各模块封装
├─ views/        21 个页面（业务页 + 登录注册 + 示例页）
├─ layouts/      AppLayout（侧栏 + 顶栏 + 路由出口）· AuthLayout
├─ stores/       auth.ts（令牌刷新、租户与角色、配额）
├─ composables/  useChat（SSE 对话 + 图片 + 留言 + 评分）· useRecorder（录音）· useTable
├─ router/       路由与守卫（/chat 免登录，供访客直聊）
├─ utils/        markdown.ts
└─ styles/       theme.css（CSS 变量主题）
```

### 交互要点

- **流式渲染**：`useChat` 逐字追加，中途可停止；失败气泡保留原文并标记失败
- **引用可见**：回答下方展示命中的知识来源，检索降级时明确提示
- **满意度评价**：仅对已落库、生成完成的 AI 回答开放，可重复修改
- **快捷回复**：坐席侧下拉插入话术，不侵入用户输入
- **角色可见性**：侧栏、首页快捷入口、路由守卫三处一致地按角色过滤

### 命令

```bash
pnpm install
pnpm dev         # http://localhost:5173（已代理 /api 到本地后端）
pnpm typecheck   # vue-tsc
pnpm lint
pnpm build       # vue-tsc -b && vite build
```

---

## 五、功能矩阵

| 菜单 | 谁可见 | 能力 |
|---|---|---|
| 工作台 | 全部 | 关键指标卡 + 角色化快捷入口 |
| 在线客服 | 全部 | 多轮对话、流式回答、引用展示、语音、图片、评分、欢迎语 |
| 知识库 | 全部（管理受角色限制） | 知识库/文档/分块、上传与重解析、检索测试 |
| 人工坐席 | owner/admin/agent | 排队会话、认领与回复、实时通知、快捷回复、转交/结束 |
| 留言箱 | owner/admin/agent | 离线留言列表、标记已处理 |
| 数据报表 | owner/admin/agent | 30 天会话量、转人工率、满意度、趋势 |
| 快捷回复 | owner/admin/agent | 话术 CRUD（分组：问候/FAQ/结束语/其他） |
| 客户管理 | owner/admin/agent | 按客户聚合的会话与评价 |
| 工单管理 | owner/admin/agent | 工单台账：状态筛选、认领、结束、留痕 |
| AI 优化 | owner/admin/agent | 知识命中率 + 未命中问题 Top |
| 会话质检 | owner/admin/agent | 随机抽样、评分与问题标签、敏感词命中记录 |
| 渠道接入 | owner/admin | 网页/邮件/微信/钉钉配置、Webhook 与入站记录 |
| 系统设置 | owner/admin | 欢迎语、触发词、工作时间与离线动作、兜底回复、敏感词与处置、抽检比例 |
| 管理后台 | owner/admin | 成员与角色、配额与用量成本、审计日志 |

角色：`owner`（拥有者）· `admin`（管理员）· `agent`（坐席）· `customer`（普通成员）。

---

## 六、数据模型

| 域 | 表 |
|---|---|
| 租户与身份 | `tenants` `tenant_members` `users` `refresh_tokens` `guest_sessions` |
| 对话 | `conversations` `messages` |
| 知识库 | `knowledge_bases` `documents` `chunks` |
| 转人工 | `handoff_tickets` `leave_messages` |
| 渠道 | `channel_configs` `channel_messages` `channel_identities` |
| 运营支撑 | `quick_replies` `audit_logs` `quality_reviews` `sensitive_hits` |
| 成本 | `llm_usage_logs` |

要点：`messages.sources` 用 JSONB 存引用；`chunks` 的检索列是 PostgreSQL 生成列
（入库与查询共用同一套 jieba 分词，避免"写进去的和查出来的不是一回事"）；
`quality_reviews.conversation_id` 有唯一索引保证同会话只抽检一次。

---

## 七、部署

```bash
cp deploy/.env.prod.example deploy/.env     # 填 JWT_SECRET、各密码、AI 网关密钥
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env up -d --build
```

| 组件 | 说明 |
|---|---|
| `postgres` | `pgvector/pgvector:pg16`，不映射宿主机端口，仅内网可达 |
| `redis` | AOF 持久化，队列与限流计数不因重启丢失 |
| `migrate` | 一次性服务：`alembic upgrade head`，API 等它成功才启动 |
| `api` / `worker` | 同一镜像不同命令，非 root（uid 10001），共享上传卷 |
| `web` | 前端静态 + Nginx：80 只留证书校验并 301 跳 HTTPS；443 提供 TLS1.2/1.3 |
| `backup` | 周期备份（也可改用 `deploy/systemd/aics-backup.timer`） |

Nginx 为 SSE 做了三件事：关闭 `proxy_buffering`、HTTP/1.1 长连接、300s 读超时。
少任何一条，流式回答都会表现为"一直加载"。

备份与恢复：

```bash
./deploy/backup.sh                                    # pg_dump -Fc + 压缩 + 保留期清理
TARGET_DB=aics_restore_drill ./deploy/restore.sh <文件>  # 演练：恢复到临时库，不碰生产库
```

`TARGET_DB` 指向非生产库时自动免交互确认——**演练不该拿生产库冒险**。

生产环境的三处必改：`APP_ENV=production`（cookie 才带 Secure，必须有 HTTPS）、
`TRUST_PROXY_HEADERS=true`（否则限流把所有请求都算成 Nginx 一个 IP）、
`RATE_LIMIT_ENABLED=true`。

---

## 八、开发与测试

```bash
# 后端
cd backend
uv venv && uv pip install .
docker compose -f deploy/docker-compose.yml up -d     # Postgres / Redis / MinIO
alembic upgrade head
uvicorn app.main:app --reload                          # http://127.0.0.1:8000
arq app.workers.settings.WorkerSettings                # 异步任务

# 测试与静态检查
pytest -q                       # 真实数据库 + 假 LLM / 假嵌入
ruff check app/ tests/
```

测试策略：**打真实数据库**（覆盖 RLS 与迁移），只把外部 AI 网关替换为
`FakeLLM` / `FakeEmbedder`，这样租户隔离、SQL 口径这类最容易出错的地方不会被 mock 掉。

当前状态：后端 96 个用例通过（不含 `test_rate_limit`），ruff 无告警；
前端 `typecheck` / `eslint` / `build` 全部通过。

---

## 许可证与约定

- 密钥、密码、证书一律走环境变量，不入库（`deploy/.env` 与 `deploy/certs/` 已 gitignore）
- 提交前：后端 `ruff check` + `pytest`，前端 `typecheck` + `lint` + `build`
- 代码注释只解释「为什么」，不解释「做了什么」
