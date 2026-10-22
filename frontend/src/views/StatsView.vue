<script setup lang="ts">
/**
 * 数据报表（M8）：客服质量指标，与"管理后台"的 token 成本看板分工明确——
 * 那里回答"花了多少钱"，这里回答"服务得好不好"。
 *
 * 图表用纯 CSS 条形/迷你柱状：30 天的趋势引入图表库（echarts 约 1MB）
 * 对当前信息密度不划算，且本项目暂无其他图表需求。
 */
import { onMounted, ref } from 'vue'

import { getStatsOverview } from '@/api/support'
import type { StatsOverview } from '@/api/types'

const stats = ref<StatsOverview | null>(null)
const loading = ref(false)

const maxTrendConversations = ref(1)

function percent(value: number, total: number): number {
  if (total <= 0) return 0
  return Math.round((value / total) * 100)
}

function formatMs(value: number | null): string {
  if (value === null) return '-'
  if (value < 1000) return `${Math.round(value)}ms`
  return `${(value / 1000).toFixed(1)}s`
}

function formatDate(value: string): string {
  return value.slice(5) // MM-DD
}

async function load(): Promise<void> {
  loading.value = true
  try {
    stats.value = await getStatsOverview()
    maxTrendConversations.value = Math.max(
      1,
      ...stats.value.trend.map((point) => point.conversations),
    )
  } catch {
    // 拦截器已提示
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" class="stats">
    <header class="stats__header">
      <div>
        <h2 class="stats__title">数据报表</h2>
        <p class="stats__hint">统计窗口：近 30 天（与工作台 KPI 同源）</p>
      </div>
      <el-button @click="load">刷新</el-button>
    </header>

    <template v-if="stats">
      <div class="stats__kpis">
        <el-card shadow="never" class="stats__kpi">
          <p class="stats__kpi-label">总会话</p>
          <p class="stats__kpi-value">{{ stats.total_conversations }}</p>
        </el-card>
        <el-card shadow="never" class="stats__kpi">
          <p class="stats__kpi-label">AI 独立解决</p>
          <p class="stats__kpi-value">{{ stats.ai_resolved }}</p>
          <p class="stats__kpi-sub">
            占 {{ percent(stats.ai_resolved, stats.total_conversations) }}%
          </p>
        </el-card>
        <el-card shadow="never" class="stats__kpi">
          <p class="stats__kpi-label">转人工率</p>
          <p class="stats__kpi-value">{{ (stats.handoff_rate * 100).toFixed(1) }}%</p>
          <p class="stats__kpi-sub">
            人工 {{ stats.human_handled }} · 排队 {{ stats.pending_handoff }}
          </p>
        </el-card>
        <el-card shadow="never" class="stats__kpi">
          <p class="stats__kpi-label">平均首响</p>
          <p class="stats__kpi-value">{{ formatMs(stats.avg_first_reply_ms) }}</p>
        </el-card>
        <el-card shadow="never" class="stats__kpi">
          <p class="stats__kpi-label">满意度</p>
          <p class="stats__kpi-value">
            {{ stats.satisfaction_avg ? stats.satisfaction_avg.toFixed(2) : '-' }}
          </p>
          <p class="stats__kpi-sub">{{ stats.satisfaction_count }} 条评价</p>
        </el-card>
        <el-card shadow="never" class="stats__kpi">
          <p class="stats__kpi-label">待处理留言</p>
          <p class="stats__kpi-value">{{ stats.new_leave_messages }}</p>
        </el-card>
      </div>

      <el-card shadow="never">
        <template #header>
          <span>近 30 天会话量</span>
        </template>
        <div class="trend">
          <div v-for="point in stats.trend" :key="point.date" class="trend__col">
            <el-tooltip placement="top" :content="`${point.conversations} 个会话 · ${point.messages} 条消息`">
              <div
                class="trend__bar"
                :style="{ height: `${(point.conversations / maxTrendConversations) * 100}%` }"
              />
            </el-tooltip>
            <span class="trend__label">{{ formatDate(point.date) }}</span>
          </div>
        </div>
      </el-card>
    </template>

    <el-empty v-else-if="!loading" description="暂无统计数据" />
  </div>
</template>

<style scoped>
.stats__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.stats__title {
  margin: 0 0 4px;
  font-size: 16px;
  font-weight: 650;
}
.stats__hint {
  margin: 0;
  font-size: 12.5px;
  color: var(--el-text-color-secondary);
}
.stats__kpis {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}
.stats__kpi-label {
  margin: 0;
  font-size: 12.5px;
  color: var(--el-text-color-secondary);
}
.stats__kpi-value {
  margin: 6px 0 0;
  font-size: 24px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.stats__kpi-sub {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.trend {
  display: flex;
  align-items: flex-end;
  gap: 2px;
  height: 160px;
}
.trend__col {
  display: flex;
  flex: 1;
  min-width: 0;
  flex-direction: column;
  align-items: center;
  justify-content: flex-end;
  height: 100%;
  gap: 4px;
}
.trend__bar {
  width: 100%;
  min-height: 2px;
  border-radius: 3px 3px 0 0;
  background: var(--app-primary);
  opacity: 0.85;
}
.trend__label {
  font-size: 10px;
  color: var(--el-text-color-placeholder);
  white-space: nowrap;
}
</style>
