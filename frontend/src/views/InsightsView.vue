<script setup lang="ts">
/**
 * AI 优化（未命中问题看板，M9）。
 *
 * 回答一个问题："AI 每天在哪些问题上裸奔？该补哪些知识？"
 * 口径见后端 services/insights.py：AI 回答未引用任何知识 = 未命中，
 * 转人工固定文案不计入（否则每次成功转人工都是假缺口）。
 */
import { computed, onMounted, ref } from 'vue'

import { getInsightOverview } from '@/api/insights'
import type { InsightOverview } from '@/api/types'

const data = ref<InsightOverview | null>(null)
const loading = ref(false)

const hitPercent = computed(() => {
  const rate = data.value?.hit_rate
  return rate === null || rate === undefined ? null : Math.round(rate * 1000) / 10
})

const hitStatus = computed(() => {
  const percent = hitPercent.value
  if (percent === null) return 'info'
  if (percent >= 70) return 'success'
  if (percent >= 40) return 'warning'
  return 'exception'
})

function formatTime(value: string): string {
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

async function load(): Promise<void> {
  loading.value = true
  try {
    data.value = await getInsightOverview()
  } catch {
    // 拦截器已提示
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="insights">
    <header class="insights__header">
      <div>
        <h2 class="insights__title">AI 优化</h2>
        <p class="insights__hint">
          近 {{ data?.window_days ?? 30 }} 天 AI 回答的知识命中率与未命中问题。
          未命中 = AI 回答时没有引用任何知识库内容。
        </p>
      </div>
      <el-button @click="load">刷新</el-button>
    </header>

    <el-alert
      v-if="data && data.knowledge_documents === 0"
      class="insights__banner"
      type="warning"
      :closable="false"
      show-icon
      title="还没有可用的知识文档"
      description="未上传知识库时所有回答都不引用知识，命中率没有参考意义。请先到「知识库」上传并完成解析。"
    />

    <div v-loading="loading" class="insights__body">
      <div class="insights__kpis">
        <el-card shadow="never" class="insights__kpi">
          <div class="insights__kpi-label">知识命中率</div>
          <div class="insights__kpi-value">
            <template v-if="hitPercent !== null">{{ hitPercent }}%</template>
            <template v-else>—</template>
          </div>
          <el-progress
            v-if="hitPercent !== null"
            :percentage="hitPercent"
            :status="hitStatus"
            :stroke-width="6"
            :show-text="false"
          />
        </el-card>
        <el-card shadow="never" class="insights__kpi">
          <div class="insights__kpi-label">AI 回答数</div>
          <div class="insights__kpi-value">{{ data?.total_answers ?? 0 }}</div>
        </el-card>
        <el-card shadow="never" class="insights__kpi">
          <div class="insights__kpi-label">引用知识回答</div>
          <div class="insights__kpi-value">{{ data?.cited_answers ?? 0 }}</div>
        </el-card>
        <el-card shadow="never" class="insights__kpi">
          <div class="insights__kpi-label">未命中提问次数</div>
          <div class="insights__kpi-value">{{ data?.unanswered_total ?? 0 }}</div>
        </el-card>
      </div>

      <el-card shadow="never">
        <template #header>
          <div class="insights__card-header">
            <span>未命中问题 Top（按提问次数）</span>
            <span class="insights__card-note">重复提问越多，越应该优先补充知识</span>
          </div>
        </template>
        <el-empty
          v-if="data && !data.unanswered.length"
          description="近 30 天没有未命中问题，或者还没有对话数据"
        />
        <el-table v-else-if="data" :data="data.unanswered" size="default">
          <el-table-column prop="question" label="用户提问" min-width="280" show-overflow-tooltip />
          <el-table-column prop="ask_count" label="提问次数" width="100" sortable />
          <el-table-column label="最近提问" width="170">
            <template #default="{ row }">{{ formatTime(row.last_asked_at) }}</template>
          </el-table-column>
          <el-table-column label="会话" width="120">
            <template #default="{ row }">
              <span class="insights__conv">{{ row.conversation_id.slice(0, 8) }}</span>
            </template>
          </el-table-column>
        </el-table>
      </el-card>
    </div>
  </div>
</template>

<style scoped>
.insights__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.insights__title {
  margin: 0 0 4px;
  font-size: 16px;
  font-weight: 650;
}
.insights__hint {
  margin: 0;
  font-size: 12.5px;
  color: var(--el-text-color-secondary);
}
.insights__banner {
  margin-bottom: 16px;
}
.insights__kpis {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}
.insights__kpi-label {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.insights__kpi-value {
  margin: 6px 0 8px;
  font-size: 24px;
  font-weight: 650;
}
.insights__card-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}
.insights__card-note {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.insights__conv {
  font-family: monospace;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
