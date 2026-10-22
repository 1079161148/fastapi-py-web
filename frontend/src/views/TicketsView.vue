<script setup lang="ts">
/**
 * 工单管理（M9）：转人工工单的完整生命周期视图。
 *
 * 与坐席台的差异：坐席台只看"现在要处理谁"（队列视角），
 * 这里是台账视角——历史工单、状态流转、谁认领的、什么时候结束的。
 * SLA 暂未实现：先有"创建→认领→结束"的可审计记录，超时告警是下一步。
 */
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'

import { claimTicket, listTickets, resolveTicket } from '@/api/agent'
import type { HandoffTicketInfo } from '@/api/types'

type StatusFilter = '' | 'pending' | 'claimed' | 'resolved' | 'cancelled'

const items = ref<HandoffTicketInfo[]>([])
const loading = ref(false)
const actingId = ref('')
const statusFilter = ref<StatusFilter>('')
const mineOnly = ref(false)

const STATUS_TEXT: Record<string, string> = {
  pending: '待接入',
  claimed: '处理中',
  resolved: '已结束',
  cancelled: '已取消',
}
const STATUS_TYPE: Record<string, 'warning' | 'primary' | 'success' | 'info'> = {
  pending: 'warning',
  claimed: 'primary',
  resolved: 'success',
  cancelled: 'info',
}

const REASON_TEXT: Record<string, string> = {
  user_request: '用户请求',
  keyword: '触发词',
  ai_marker: 'AI 判定',
}

const pendingCount = computed(
  () => items.value.filter((item) => item.status === 'pending').length,
)

/** 认领人展示：坐席工作台只有自己的 id 可比，其余显示短 ID 便于对账 */
function agentLabel(row: HandoffTicketInfo): string {
  if (!row.agent_id) return '—'
  return row.agent_id.slice(0, 8)
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const data = await listTickets({
      status: statusFilter.value || undefined,
      mine: mineOnly.value || undefined,
    })
    items.value = data.items
  } catch {
    // 拦截器已提示
  } finally {
    loading.value = false
  }
}

async function onClaim(row: HandoffTicketInfo): Promise<void> {
  actingId.value = row.id
  try {
    await claimTicket(row.id)
    ElMessage.success('已认领，请到人工坐席页回复')
    await load()
  } catch {
    // 409（被别人先认领）等由拦截器提示；刷新拿到服务端真实状态
    await load()
  } finally {
    actingId.value = ''
  }
}

async function onResolve(row: HandoffTicketInfo): Promise<void> {
  actingId.value = row.id
  try {
    await resolveTicket(row.id)
    ElMessage.success('工单已结束，会话交还 AI')
    await load()
  } catch {
    await load()
  } finally {
    actingId.value = ''
  }
}

function formatTime(value: string | null): string {
  return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'
}

onMounted(load)
</script>

<template>
  <div class="tickets">
    <header class="tickets__header">
      <div>
        <h2 class="tickets__title">工单管理</h2>
        <p class="tickets__hint">
          转人工工单台账：排队、认领与结束全程留痕。回复请到人工坐席页进行。
        </p>
      </div>
      <div class="tickets__filters">
        <el-switch v-model="mineOnly" active-text="只看我认领的" @change="load" />
        <el-radio-group v-model="statusFilter" @change="load">
          <el-radio-button value="">全部</el-radio-button>
          <el-radio-button value="pending">待接入 {{ pendingCount }}</el-radio-button>
          <el-radio-button value="claimed">处理中</el-radio-button>
          <el-radio-button value="resolved">已结束</el-radio-button>
          <el-radio-button value="cancelled">已取消</el-radio-button>
        </el-radio-group>
      </div>
    </header>

    <el-card v-loading="loading" shadow="never">
      <el-empty v-if="!items.length" description="暂无工单" />
      <el-table v-else :data="items" size="default">
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="STATUS_TYPE[row.status] ?? 'info'" size="small">
              {{ STATUS_TEXT[row.status] ?? row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="summary" label="问题摘要" min-width="240" show-overflow-tooltip>
          <template #default="{ row }">{{ row.summary || '（无摘要）' }}</template>
        </el-table-column>
        <el-table-column label="转人工原因" width="110">
          <template #default="{ row }">{{ REASON_TEXT[row.reason] ?? row.reason }}</template>
        </el-table-column>
        <el-table-column prop="note" label="用户备注" min-width="140" show-overflow-tooltip>
          <template #default="{ row }">{{ row.note || '—' }}</template>
        </el-table-column>
        <el-table-column label="认领人" width="100">
          <template #default="{ row }">{{ agentLabel(row) }}</template>
        </el-table-column>
        <el-table-column label="创建时间" width="170">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="结束时间" width="170">
          <template #default="{ row }">{{ formatTime(row.resolved_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="140" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="row.status === 'pending'"
              link
              type="primary"
              :loading="actingId === row.id"
              @click="onClaim(row)"
            >
              认领
            </el-button>
            <el-button
              v-if="row.status === 'claimed'"
              link
              type="warning"
              :loading="actingId === row.id"
              @click="onResolve(row)"
            >
              结束工单
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.tickets__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}
.tickets__title {
  margin: 0 0 4px;
  font-size: 16px;
  font-weight: 650;
}
.tickets__hint {
  margin: 0;
  font-size: 12.5px;
  color: var(--el-text-color-secondary);
}
.tickets__filters {
  display: flex;
  align-items: center;
  gap: 16px;
}
</style>
