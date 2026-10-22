<script setup lang="ts">
/**
 * 工作台（M7 视觉重构）。
 *
 * 数据全部来自真实接口，按角色分级展示：
 * - 管理员：今日用量 KPI（/usage/overview）+ 待处理会话
 * - 坐席：待处理会话队列
 * - 客户：只有快捷入口（KPI 与队列对他们没有意义，硬塞只会是噪音）
 *
 * 不做"假数据占位"：拿不到就显示空状态或整块隐藏，避免看板数字看起来像真的。
 */
import { computed, onMounted, ref } from 'vue'

import { getUsageOverview } from '@/api/admin'
import { api } from '@/api/client'
import { getStatsOverview } from '@/api/support'
import type {
  AgentConversationInfo,
  AgentConversationListResponse,
  StatsOverview,
  UsageOverview,
} from '@/api/types'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const usage = ref<UsageOverview | null>(null)
const stats = ref<StatsOverview | null>(null)
const pending = ref<AgentConversationInfo[]>([])
const hasMorePending = ref(false)
const loading = ref(false)

// 一律按租户内角色判断：平台 admin 在别人租户里没有管理权限
const isAdmin = computed(() => ['owner', 'admin'].includes(auth.user?.tenant_role ?? ''))
const isAgent = computed(() =>
  ['owner', 'admin', 'agent'].includes(auth.user?.tenant_role ?? ''),
)

const displayName = computed(
  () => auth.user?.display_name || auth.user?.email.split('@')[0] || '你好',
)
const roleLabel = computed(() => {
  const map: Record<string, string> = {
    owner: '所有者',
    admin: '管理员',
    agent: '坐席',
    viewer: '只读',
  }
  return map[auth.user?.tenant_role ?? ''] ?? ''
})

const greeting = computed(() => {
  const hour = new Date().getHours()
  if (hour < 6) return '夜深了'
  if (hour < 12) return '早上好'
  if (hour < 18) return '下午好'
  return '晚上好'
})

const chatLink = computed(() =>
  auth.tenantId ? { path: '/chat', query: { tenant: auth.tenantId } } : { path: '/chat' },
)

const quotaPercent = computed(() => {
  const quota = usage.value?.quota
  if (!quota || quota.tokens_limit <= 0) return 0
  return Math.min(Math.round((quota.tokens_used_today / quota.tokens_limit) * 100), 100)
})

const quickLinks = computed(() => {
  const role = auth.user?.tenant_role
  const isAdmin = role === 'owner' || role === 'admin'
  return [
    {
      to: '/chat',
      icon: 'ChatDotRound',
      label: '在线客服',
      desc: '进入访客对话窗口',
      visible: true,
    },
    {
      to: '/knowledge',
      icon: 'Collection',
      label: '知识库',
      desc: '上传资料、查看分块与检索',
      visible: isAdmin || role === 'agent',
    },
    {
      to: '/agent',
      icon: 'Service',
      label: '人工坐席',
      desc: '接管会话并回复用户',
      visible: isAdmin || role === 'agent',
    },
    {
      to: '/leave-messages',
      icon: 'Message',
      label: '留言箱',
      desc: '处理离线留言与待办',
      visible: isAdmin || role === 'agent',
    },
    {
      to: '/stats',
      icon: 'TrendCharts',
      label: '数据报表',
      desc: '转人工率、满意度与趋势',
      visible: isAdmin || role === 'agent',
    },
    {
      to: '/quick-replies',
      icon: 'ChatLineSquare',
      label: '快捷回复',
      desc: '坐席常用话术库',
      visible: isAdmin || role === 'agent',
    },
    {
      to: '/customers',
      icon: 'User',
      label: '客户管理',
      desc: '按客户查看会话与评价',
      visible: isAdmin || role === 'agent',
    },
    {
      to: '/tickets',
      icon: 'Tickets',
      label: '工单管理',
      desc: '转人工工单台账与状态流转',
      visible: isAdmin || role === 'agent',
    },
    {
      to: '/insights',
      icon: 'DataAnalysis',
      label: 'AI 优化',
      desc: '命中率与未命中问题分析',
      visible: isAdmin || role === 'agent',
    },
    {
      to: '/quality',
      icon: 'Stamp',
      label: '会话质检',
      desc: '随机抽检、评分与敏感词命中',
      visible: isAdmin || role === 'agent',
    },
    {
      to: '/channels',
      icon: 'Connection',
      label: '渠道接入',
      desc: '网页 / 邮件 / 微信 / 钉钉',
      visible: isAdmin,
    },
    {
      to: '/settings',
      icon: 'Setting',
      label: '系统设置',
      desc: '欢迎语、转人工规则与工作时间',
      visible: isAdmin,
    },
    {
      to: '/admin',
      icon: 'DataLine',
      label: '管理后台',
      desc: '用量成本与成员管理',
      visible: isAdmin,
    },
  ].filter((item) => item.visible)
})

const CHANNEL_LABELS: Record<string, string> = {
  web: '网页',
  email: '邮件',
  wechat: '微信',
  dingtalk: '钉钉',
}

const STATUS_LABELS: Record<string, string> = {
  ai: 'AI 服务中',
  pending_handoff: '等待接管',
  human: '人工中',
  closed: '已结束',
}

function formatNumber(value: number): string {
  return value.toLocaleString('zh-CN')
}

function formatTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '-' : date.toLocaleString('zh-CN', { hour12: false })
}

onMounted(async () => {
  loading.value = true
  try {
    const tasks: Promise<void>[] = []
    if (isAdmin.value) {
      tasks.push(
        getUsageOverview()
          .then((data) => {
            usage.value = data
          })
          .catch(() => {
            usage.value = null
          }),
      )
    }
    // 客服质量指标对所有登录角色开放（口径与"数据报表"页一致）
    tasks.push(
      getStatsOverview()
        .then((data) => {
          stats.value = data
        })
        .catch(() => {
          stats.value = null
        }),
    )
    if (isAgent.value) {
      tasks.push(
        api
          .get<AgentConversationListResponse>('/v1/agent/conversations', {
            params: { status: 'pending_handoff,human', limit: 5 },
          })
          .then((data) => {
            pending.value = data.items
            hasMorePending.value = Boolean(data.next_cursor)
          })
          .catch(() => {
            pending.value = []
          }),
      )
    }
    await Promise.all(tasks)
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <div v-loading="loading" class="workbench">
    <section class="hero">
      <div class="hero__text">
        <p class="hero__eyebrow">
          {{ roleLabel }}<template v-if="auth.tenant"> · {{ auth.tenant.name }}</template>
        </p>
        <h2 class="hero__title">{{ greeting }}，{{ displayName }}</h2>
        <p class="hero__desc">
          智能客服已就绪：AI 先行应答，知识库提供依据，转人工时自动带入上下文。
        </p>
      </div>
      <router-link :to="chatLink" class="hero__cta">
        <el-icon><ChatDotRound /></el-icon>
        开始对话
      </router-link>
    </section>

    <!-- 客服质量 KPI：近 30 天口径，与"数据报表"页同源；管理员另见配额行 -->
    <section v-if="stats" class="kpis">
      <article class="kpi">
        <span class="kpi__label">近 30 天会话</span>
        <strong class="kpi__value tabular">{{ formatNumber(stats.total_conversations) }}</strong>
        <span class="kpi__hint">AI 解决 {{ stats.ai_resolved }}</span>
      </article>
      <article class="kpi">
        <span class="kpi__label">待人工</span>
        <strong class="kpi__value tabular">{{ pending.length }}{{ hasMorePending ? '+' : '' }}</strong>
        <span class="kpi__hint">等待坐席接手</span>
      </article>
      <article class="kpi">
        <span class="kpi__label">转人工率</span>
        <strong class="kpi__value tabular">{{ (stats.handoff_rate * 100).toFixed(1) }}%</strong>
        <span class="kpi__hint">人工 {{ stats.human_handled }} · 排队 {{ stats.pending_handoff }}</span>
      </article>
      <article class="kpi">
        <span class="kpi__label">满意度</span>
        <strong class="kpi__value tabular">
          {{ stats.satisfaction_avg ? stats.satisfaction_avg.toFixed(2) : '-' }}
        </strong>
        <span class="kpi__hint">{{ stats.satisfaction_count }} 条评价</span>
      </article>
      <article v-if="isAdmin && usage" class="kpi">
        <span class="kpi__label">今日 Token</span>
        <strong class="kpi__value tabular">{{ formatNumber(usage.today.total_tokens) }}</strong>
        <span class="kpi__hint">配额已用 {{ quotaPercent }}%</span>
      </article>
      <article class="kpi">
        <span class="kpi__label">待处理留言</span>
        <strong class="kpi__value tabular">{{ stats.new_leave_messages }}</strong>
        <router-link to="/leave-messages" class="kpi__link">去留言箱</router-link>
      </article>
    </section>

    <div class="grid">
      <section v-if="isAgent" class="panel">
        <header class="panel__head">
          <h3>待处理会话</h3>
          <span class="panel__meta">
            {{ pending.length }}{{ hasMorePending ? '+' : '' }} 条
          </span>
        </header>

        <ul v-if="pending.length" class="queue">
          <li v-for="item in pending" :key="item.id" class="queue__item">
            <span class="queue__dot" :class="`queue__dot--${item.status}`" />
            <div class="queue__body">
              <div class="queue__title">
                {{ item.title || '未命名会话' }}
              </div>
              <div class="queue__meta">
                {{ CHANNEL_LABELS[item.channel] ?? item.channel }} ·
                {{ item.owner_type === 'guest' ? '访客' : '登录用户' }} ·
                {{ formatTime(item.updated_at) }}
              </div>
            </div>
            <el-tag size="small" :type="item.status === 'human' ? 'success' : 'warning'">
              {{ STATUS_LABELS[item.status] ?? item.status }}
            </el-tag>
          </li>
        </ul>

        <div v-else class="empty">
          <el-icon class="empty__icon"><CircleCheck /></el-icon>
          <p class="empty__title">暂无待处理会话</p>
          <p class="empty__desc">AI 正在自动应答；需要人工时，会话会自动进入这个队列。</p>
        </div>
      </section>

      <section class="panel">
        <header class="panel__head">
          <h3>快捷入口</h3>
        </header>
        <div class="quick">
          <router-link
            v-for="item in quickLinks"
            :key="item.to"
            :to="item.to"
            class="quick__item"
          >
            <span class="quick__icon">
              <el-icon><component :is="item.icon" /></el-icon>
            </span>
            <span class="quick__text">
              <strong>{{ item.label }}</strong>
              <small>{{ item.desc }}</small>
            </span>
            <el-icon class="quick__arrow"><ArrowRight /></el-icon>
          </router-link>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.workbench {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

/* ---------- Hero ---------- */
.hero {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  padding: 26px 28px;
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius-lg);
  background:
    radial-gradient(120% 140% at 0% 0%, rgba(37, 99, 235, 0.1), transparent 55%),
    linear-gradient(180deg, #ffffff, #f7f9ff);
  box-shadow: var(--app-shadow-sm);
}

.hero__eyebrow {
  margin: 0 0 6px;
  color: var(--app-primary);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.02em;
}
.hero__title {
  margin: 0;
  font-size: 24px;
  font-weight: 650;
  letter-spacing: -0.02em;
}
.hero__desc {
  margin: 8px 0 0;
  max-width: 520px;
  color: var(--app-text-2);
  font-size: 13.5px;
  line-height: 1.6;
}

.hero__cta {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 7px;
  padding: 10px 18px;
  border-radius: 10px;
  background: var(--app-primary);
  color: #fff;
  font-size: 13.5px;
  font-weight: 600;
  text-decoration: none;
  box-shadow: 0 6px 16px -8px rgba(37, 99, 235, 0.7);
  transition: background 0.15s ease;
}
.hero__cta:hover {
  background: var(--app-primary-hover);
}

/* ---------- KPI ---------- */
.kpis {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 14px;
}

.kpi {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 16px 18px;
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius);
  box-shadow: var(--app-shadow-sm);
}
.kpi__label {
  color: var(--app-text-3);
  font-size: 12.5px;
}
.kpi__value {
  font-size: 26px;
  font-weight: 650;
  letter-spacing: -0.02em;
  line-height: 1.1;
}
.kpi__hint {
  color: var(--app-text-3);
  font-size: 11.5px;
}
.kpi__link {
  color: var(--app-primary);
  font-size: 11.5px;
  text-decoration: none;
}
.kpi__link:hover {
  text-decoration: underline;
}
.kpi__progress {
  margin-top: 2px;
}

/* ---------- 两栏 ---------- */
.grid {
  display: grid;
  grid-template-columns: minmax(0, 1.55fr) minmax(0, 1fr);
  gap: 14px;
  align-items: start;
}

.panel {
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius);
  box-shadow: var(--app-shadow-sm);
  overflow: hidden;
}
.panel__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 18px;
  border-bottom: 1px solid var(--app-border);
}
.panel__head h3 {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
}
.panel__meta {
  color: var(--app-text-3);
  font-size: 12px;
}

/* ---------- 队列 ---------- */
.queue {
  margin: 0;
  padding: 6px 0;
  list-style: none;
}
.queue__item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 11px 18px;
}
.queue__item:hover {
  background: var(--app-surface-muted);
}
.queue__dot {
  width: 7px;
  height: 7px;
  flex: 0 0 7px;
  border-radius: 50%;
  background: var(--app-warning);
}
.queue__dot--human {
  background: var(--app-success);
}
.queue__body {
  min-width: 0;
  flex: 1;
}
.queue__title {
  overflow: hidden;
  font-size: 13.5px;
  font-weight: 500;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.queue__meta {
  margin-top: 3px;
  color: var(--app-text-3);
  font-size: 11.5px;
}

/* ---------- 快捷入口 ---------- */
.quick {
  display: flex;
  flex-direction: column;
  padding: 6px;
}
.quick__item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 11px 12px;
  border-radius: 9px;
  color: inherit;
  text-decoration: none;
}
.quick__item:hover {
  background: var(--app-surface-muted);
}
.quick__icon {
  display: grid;
  width: 34px;
  height: 34px;
  flex: 0 0 34px;
  place-items: center;
  border-radius: 9px;
  background: var(--app-primary-soft);
  color: var(--app-primary);
  font-size: 16px;
}
.quick__text {
  display: flex;
  min-width: 0;
  flex: 1;
  flex-direction: column;
}
.quick__text strong {
  font-size: 13.5px;
  font-weight: 550;
}
.quick__text small {
  color: var(--app-text-3);
  font-size: 11.5px;
}
.quick__arrow {
  color: var(--app-text-3);
  font-size: 13px;
}

/* ---------- 空状态 ---------- */
.empty {
  padding: 40px 24px;
  text-align: center;
}
.empty__icon {
  color: var(--app-success);
  font-size: 28px;
}
.empty__title {
  margin: 10px 0 4px;
  font-size: 13.5px;
  font-weight: 550;
}
.empty__desc {
  margin: 0 auto;
  max-width: 340px;
  color: var(--app-text-3);
  font-size: 12px;
  line-height: 1.6;
}

@media (max-width: 1080px) {
  .grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .hero {
    flex-direction: column;
    align-items: flex-start;
    gap: 16px;
  }
}
</style>
