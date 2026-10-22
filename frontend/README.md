# 前端（Vue 3 + TypeScript）

Vue 3.5（`<script setup>`）· TypeScript · Vite · Pinia · vue-router · Element Plus 2.11 ·
markdown-it + dompurify · axios · 原生 SSE。
整体架构与功能矩阵见仓库根目录 `README.md`，这里只写前端日常开发需要的东西。

## 目录

```
src/
├─ api/
│  ├─ client.ts      axios 实例：统一 /api 前缀、401 刷新重试、错误信息提示
│  ├─ types.ts       与后端 Pydantic 模型一一对应的接口类型
│  └─ *.ts           按后端模块拆分：chat agent knowledge channels voice
│                    support(报表/快捷回复/客户/留言/评分) insights quality admin
├─ views/            21 个页面（业务页 + 登录注册）
├─ layouts/          AppLayout（侧栏 + 顶栏 + 路由出口）· AuthLayout
├─ stores/auth.ts    令牌刷新、当前用户、租户与角色、配额
├─ composables/      useChat（SSE 对话 + 图片 + 留言 + 评分）· useRecorder · useTable
├─ router/index.ts   路由表与守卫（/chat 免登录，供访客直接咨询）
├─ utils/markdown.ts Markdown 渲染（dompurify 消毒）
└─ styles/theme.css  CSS 变量主题
```

说明：项目**没有独立的 `components/` 目录**——页面内聚度足够高，
通用能力抽在 `composables/`（逻辑）与 `layouts/`（外壳）里，避免为抽而抽。

## 命令

```bash
pnpm install
pnpm dev          # http://localhost:5173，已代理 /api 到 127.0.0.1:8000
pnpm typecheck    # vue-tsc -b
pnpm lint         # eslint --fix
pnpm build        # vue-tsc -b && vite build
```

## 菜单与角色

| 菜单 | 路由 | 可见 |
|---|---|---|
| 工作台 | `/` | 全部 |
| 在线客服 | `/chat` | 全部（免登录，访客直聊） |
| 知识库 | `/knowledge` | 全部 |
| 人工坐席 | `/agent` | owner / admin / agent |
| 留言箱 | `/leave-messages` | owner / admin / agent |
| 数据报表 | `/stats` | owner / admin / agent |
| 快捷回复 | `/quick-replies` | owner / admin / agent |
| 客户管理 | `/customers` | owner / admin / agent |
| 工单管理 | `/tickets` | owner / admin / agent |
| AI 优化 | `/insights` | owner / admin / agent |
| 会话质检 | `/quality` | owner / admin / agent |
| 渠道接入 | `/channels` | owner / admin |
| 系统设置 | `/settings` | owner / admin |
| 管理后台 | `/admin` | owner / admin（审计日志内嵌在页面底部） |

角色判断集中在三处且口径一致：**侧栏菜单**、**首页快捷入口**、**路由守卫**。
新增页面时三处都要同步，否则会出现"菜单看得见、点进去 403"。

## 约定

| 主题 | 约定 |
|---|---|
| 类型 | 后端模型变更先改 `api/types.ts`，再改视图，避免 `any` 兜底 |
| 请求 | 一律走 `api/*.ts` 封装，不在组件里直接写 URL |
| 流式 | SSE 在 `useChat` 里统一处理（心跳、结束、错误），页面只消费状态 |
| 错误提示 | axios 拦截器统一弹出；业务代码只在需要额外说明时才自己提示 |
| 样式 | 优先用 `theme.css` 的 CSS 变量；`scoped` 样式只写页面特有部分 |
| 组件库 | Element Plus 图标在 `main.ts` 全量注册，模板里直接用字符串名 |

## 生产

构建产物由 Nginx 托管，API 走同源 `/api` 反代（**不需要在构建期注入后端地址**）。
构建与部署细节见 `deploy/Dockerfile.web` 与 `deploy/nginx/`。
