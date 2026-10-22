<script setup lang="ts">
/**
 * 质检抽检（M9）：随机抽样 → 看会话 → 打分。
 *
 * 敏感词配置放在系统设置（属于租户运营配置），这里只做两件事：
 * 抽检评分 与 敏感词命中记录回看——命中是"事发后复盘"，和评分在一起才顺手。
 */
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'

import { listAgentMessages } from '@/api/agent'
import {
  getQualityOverview,
  listQualityReviews,
  listSensitiveHits,
  sampleQualityReviews,
  submitQualityReview,
} from '@/api/quality'
import type {
  MessageInfo,
  QualityOverview,
  QualityReviewInfo,
  SensitiveHitInfo,
} from '@/api/types'

type TabStatus = 'pending' | 'done' | ''

const overview = ref<QualityOverview | null>(null)
const items = ref<QualityReviewInfo[]>([])
const hits = ref<SensitiveHitInfo[]>([])
const loading = ref(false)
const sampling = ref(false)
const submitting = ref(false)
const tab = ref<TabStatus>('pending')
const sampleLimit = ref(10)

const reviewDialog = ref(false)
const current = ref<QualityReviewInfo | null>(null)
const transcript = ref<MessageInfo[]>([])
const loadingTranscript = ref(false)
const score = ref(5)
const tags = ref<string[]>([])
const comment = ref('')

const TAG_LABELS: Record<string, string> = {
  unresolved: '未解答',
  off_topic: '答非所问',
  tone: '语气/态度问题',
  slow: '响应太慢',
  policy: '话术违规',
  sensitive: '敏感信息处置不当',
  other: '其他',
}
const TAG_KEYS = Object.keys(TAG_LABELS)

const ACTION_LABELS: Record<string, string> = {
  warn: '仅记录',
  handoff: '转人工',
  block: '拒绝输入',
}
const ACTION_TYPES: Record<string, 'info' | 'warning' | 'danger'> = {
  warn: 'info',
  handoff: 'warning',
  block: 'danger',
}

const STATUS_LABELS: Record<string, string> = {
  ai: 'AI 服务中',
  pending_handoff: '等待接管',
  human: '人工中',
  closed: '已结束',
}

const coveragePercent = computed(() => Math.round((overview.value?.coverage ?? 0) * 1000) / 10)

async function load(): Promise<void> {
  loading.value = true
  try {
    const [summary, list, hitList] = await Promise.all([
      getQualityOverview(),
      listQualityReviews({ status: tab.value || undefined }),
      listSensitiveHits(),
    ])
    overview.value = summary
    items.value = list
    hits.value = hitList
  } catch {
    // 拦截器已提示
  } finally {
    loading.value = false
  }
}

async function onSample(): Promise<void> {
  sampling.value = true
  try {
    const created = await sampleQualityReviews(sampleLimit.value)
    if (created === 0) {
      ElMessage.info('没有可抽样的会话：窗口内会话都已抽过')
    } else {
      ElMessage.success(`已抽取 ${created} 条会话`)
    }
    await load()
  } catch {
    // 拦截器已提示
  } finally {
    sampling.value = false
  }
}

async function openReview(row: QualityReviewInfo): Promise<void> {
  current.value = row
  score.value = row.score ?? 5
  tags.value = [...row.tags]
  comment.value = row.comment || ''
  transcript.value = []
  reviewDialog.value = true
  loadingTranscript.value = true
  try {
    const data = await listAgentMessages(row.conversation_id, 100)
    transcript.value = data.items
  } catch {
    // 拦截器已提示；会话明细拿不到不影响评分
  } finally {
    loadingTranscript.value = false
  }
}

async function onSubmit(): Promise<void> {
  const review = current.value
  if (!review) return
  submitting.value = true
  try {
    await submitQualityReview(review.id, {
      score: score.value,
      tags: tags.value,
      comment: comment.value.trim(),
    })
    ElMessage.success('评分已保存')
    reviewDialog.value = false
    await load()
  } catch {
    // 拦截器已提示
  } finally {
    submitting.value = false
  }
}

function formatTime(value: string | null): string {
  return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'
}

function tagLabel(tag: string): string {
  return TAG_LABELS[tag] ?? tag
}

onMounted(load)
</script>

<template>
  <div class="quality">
    <header class="quality__header">
      <div>
        <h2 class="quality__title">会话质检</h2>
        <p class="quality__hint">
          随机抽取近 {{ overview?.window_days ?? 30 }} 天的会话人工复核。
          敏感词配置在系统设置里，命中记录在这里回看。
        </p>
      </div>
      <div class="quality__actions">
        <el-input-number v-model="sampleLimit" :min="1" :max="100" size="small" controls-position="right" />
        <el-button type="primary" :loading="sampling" @click="onSample">随机抽样</el-button>
      </div>
    </header>

    <div v-loading="loading" class="quality__body">
      <div class="quality__kpis">
        <el-card shadow="never" class="quality__kpi">
          <div class="quality__kpi-label">抽检覆盖率</div>
          <div class="quality__kpi-value">{{ coveragePercent }}%</div>
          <div class="quality__kpi-note">
            已评 {{ overview?.reviewed_count ?? 0 }} / 会话 {{ overview?.conversations_total ?? 0 }}
          </div>
        </el-card>
        <el-card shadow="never" class="quality__kpi">
          <div class="quality__kpi-label">平均质检分</div>
          <div class="quality__kpi-value">{{ overview?.avg_score ?? '—' }}</div>
        </el-card>
        <el-card shadow="never" class="quality__kpi">
          <div class="quality__kpi-label">待抽检</div>
          <div class="quality__kpi-value">{{ overview?.pending_count ?? 0 }}</div>
        </el-card>
        <el-card shadow="never" class="quality__kpi">
          <div class="quality__kpi-label">敏感词命中</div>
          <div class="quality__kpi-value">{{ overview?.sensitive_hits ?? 0 }}</div>
        </el-card>
      </div>

      <el-card shadow="never">
        <template #header>
          <div class="quality__card-header">
            <el-radio-group v-model="tab" size="small" @change="load">
              <el-radio-button value="pending">待抽检</el-radio-button>
              <el-radio-button value="done">已抽检</el-radio-button>
              <el-radio-button value="">全部</el-radio-button>
            </el-radio-group>
            <span class="quality__card-note">同一会话只抽检一次，重复抽样不会重复建单</span>
          </div>
        </template>
        <el-empty v-if="!items.length" description="暂无抽检记录，点击右上角「随机抽样」开始" />
        <el-table v-else :data="items" size="default">
          <el-table-column label="会话" min-width="200" show-overflow-tooltip>
            <template #default="{ row }">{{ row.conversation_title || '新对话' }}</template>
          </el-table-column>
          <el-table-column label="状态" width="110">
            <template #default="{ row }">
              {{ STATUS_LABELS[row.conversation_status] ?? row.conversation_status }}
            </template>
          </el-table-column>
          <el-table-column label="最近更新" width="170">
            <template #default="{ row }">{{ formatTime(row.conversation_updated_at) }}</template>
          </el-table-column>
          <el-table-column label="质检分" width="110">
            <template #default="{ row }">
              <el-rate
                v-if="row.score"
                :model-value="row.score"
                disabled
                size="small"
              />
              <span v-else class="quality__muted">未评分</span>
            </template>
          </el-table-column>
          <el-table-column label="问题标签" min-width="180">
            <template #default="{ row }">
              <el-tag
                v-for="tag in row.tags"
                :key="tag"
                size="small"
                type="warning"
                effect="plain"
                class="quality__tag"
              >
                {{ tagLabel(tag) }}
              </el-tag>
              <span v-if="!row.tags.length" class="quality__muted">—</span>
            </template>
          </el-table-column>
          <el-table-column prop="comment" label="评语" min-width="160" show-overflow-tooltip />
          <el-table-column label="操作" width="110" fixed="right">
            <template #default="{ row }">
              <el-button link :type="row.status === 'done' ? 'primary' : 'primary'" @click="openReview(row)">
                {{ row.status === 'done' ? '修改评分' : '评分' }}
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-card>

      <el-card shadow="never">
        <template #header>敏感词命中记录</template>
        <el-empty v-if="!hits.length" description="近 30 天没有敏感词命中" />
        <el-table v-else :data="hits" size="default">
          <el-table-column label="命中词" width="180">
            <template #default="{ row }">
              <el-tag
                v-for="word in row.words"
                :key="word"
                size="small"
                type="danger"
                effect="plain"
                class="quality__tag"
              >
                {{ word }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="处置" width="110">
            <template #default="{ row }">
              <el-tag :type="ACTION_TYPES[row.action] ?? 'info'" size="small">
                {{ ACTION_LABELS[row.action] ?? row.action }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="snippet" label="命中片段" min-width="240" show-overflow-tooltip />
          <el-table-column label="时间" width="170">
            <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
          </el-table-column>
        </el-table>
      </el-card>
    </div>

    <el-dialog v-model="reviewDialog" :title="`质检评分：${current?.conversation_title || '新对话'}`" width="720px">
      <div v-loading="loadingTranscript" class="quality__transcript">
        <el-empty v-if="!transcript.length && !loadingTranscript" description="该会话暂无消息" />
        <div v-for="message in transcript" :key="message.id" class="quality__message">
          <span class="quality__role">
            {{ message.role === 'user' ? '用户' : message.source === 'agent' ? '坐席' : 'AI' }}
          </span>
          <span class="quality__content">{{ message.content }}</span>
        </div>
      </div>
      <el-form label-width="80px" class="quality__form">
        <el-form-item label="评分">
          <el-rate v-model="score" :max="5" show-text :texts="['很差', '较差', '一般', '良好', '优秀']" />
        </el-form-item>
        <el-form-item label="问题标签">
          <el-checkbox-group v-model="tags">
            <el-checkbox v-for="key in TAG_KEYS" :key="key" :value="key" :label="key">
              {{ tagLabel(key) }}
            </el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item label="评语">
          <el-input v-model="comment" type="textarea" :rows="3" maxlength="2000" show-word-limit placeholder="可留空；有问题时写清楚便于复盘" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="reviewDialog = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="onSubmit">保存评分</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.quality__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}
.quality__title {
  margin: 0 0 4px;
  font-size: 16px;
  font-weight: 650;
}
.quality__hint {
  margin: 0;
  font-size: 12.5px;
  color: var(--el-text-color-secondary);
}
.quality__actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.quality__kpis {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}
.quality__kpi-label {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.quality__kpi-value {
  margin: 6px 0 4px;
  font-size: 24px;
  font-weight: 650;
}
.quality__kpi-note {
  font-size: 11px;
  color: var(--app-text-3);
}
.quality__card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.quality__card-note {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.quality__tag {
  margin-right: 4px;
}
.quality__muted {
  color: var(--el-text-color-placeholder);
  font-size: 12px;
}
/* 会话回放：固定高度滚动，否则长会话会把评分表单挤到屏幕外 */
.quality__transcript {
  max-height: 260px;
  overflow-y: auto;
  padding: 10px 12px;
  margin-bottom: 16px;
  border: 1px solid var(--app-border);
  border-radius: 8px;
  background: var(--app-surface-muted);
}
.quality__message {
  display: flex;
  gap: 8px;
  padding: 4px 0;
  font-size: 13px;
  line-height: 1.6;
}
.quality__role {
  flex: none;
  width: 44px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.quality__content {
  word-break: break-word;
}
.quality__form {
  margin-top: 4px;
}
</style>
