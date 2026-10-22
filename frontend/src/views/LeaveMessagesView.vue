<script setup lang="ts">
/**
 * 留言箱（M8）：离线留言的待处理/已处理队列。
 *
 * 与坐席台的差异：坐席台按"会话"组织（当前要回复谁），
 * 这里按"待办"组织（谁还没被联系过）——留言是"稍后回你"，
 * 用已处理/待处理筛远比按会话翻更贴近使用方式。
 */
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'

import { handleLeaveMessage, listLeaveMessages } from '@/api/agent'
import type { LeaveMessageInfo } from '@/api/types'

const items = ref<LeaveMessageInfo[]>([])
const loading = ref(false)
const handlingId = ref('')
const statusFilter = ref<'new' | 'handled' | ''>('')

const STATUS_TEXT: Record<string, string> = { new: '待处理', handled: '已处理' }
const STATUS_TYPE: Record<string, 'warning' | 'success'> = {
  new: 'warning',
  handled: 'success',
}

const newCount = computed(() => items.value.filter((item) => item.status === 'new').length)

async function load(): Promise<void> {
  loading.value = true
  try {
    const data = await listLeaveMessages(
      statusFilter.value ? { status: statusFilter.value } : {},
    )
    items.value = data.items
  } catch {
    // 拦截器已提示
  } finally {
    loading.value = false
  }
}

async function onHandle(row: LeaveMessageInfo): Promise<void> {
  handlingId.value = row.id
  try {
    await handleLeaveMessage(row.id)
    ElMessage.success('已标记为处理')
    await load()
  } catch {
    // 拦截器已提示
  } finally {
    handlingId.value = ''
  }
}

function formatTime(value: string): string {
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

onMounted(load)
</script>

<template>
  <div class="leave">
    <header class="leave__header">
      <div>
        <h2 class="leave__title">留言箱</h2>
        <p class="leave__hint">
          非工作时间或用户不愿排队时留下的离线留言，联系后标记为已处理。
        </p>
      </div>
      <el-radio-group v-model="statusFilter" @change="load">
        <el-radio-button value="">全部</el-radio-button>
        <el-radio-button value="new">待处理 {{ newCount }}</el-radio-button>
        <el-radio-button value="handled">已处理</el-radio-button>
      </el-radio-group>
    </header>

    <el-card v-loading="loading" shadow="never">
      <el-empty v-if="!items.length" description="暂无留言" />
      <el-table v-else :data="items" size="default">
        <el-table-column label="联系人" min-width="140">
          <template #default="{ row }">
            <p class="leave__name">{{ row.name || '匿名访客' }}</p>
            <p class="leave__contact">{{ row.contact }}</p>
          </template>
        </el-table-column>
        <el-table-column prop="content" label="留言内容" min-width="280" show-overflow-tooltip />
        <el-table-column label="提交时间" width="170">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="STATUS_TYPE[row.status] ?? 'info'" size="small">
              {{ STATUS_TEXT[row.status] ?? row.status }}
            </el-tag>
            <p v-if="row.handled_at" class="leave__handled">
              {{ formatTime(row.handled_at) }}
            </p>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="120">
          <template #default="{ row }">
            <el-button
              v-if="row.status === 'new'"
              link
              type="primary"
              :loading="handlingId === row.id"
              @click="onHandle(row)"
            >
              标记已处理
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.leave__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.leave__title {
  margin: 0 0 4px;
  font-size: 16px;
  font-weight: 650;
}
.leave__hint {
  margin: 0;
  font-size: 12.5px;
  color: var(--el-text-color-secondary);
}
.leave__name {
  margin: 0;
  font-weight: 600;
}
.leave__contact,
.leave__handled {
  margin: 2px 0 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
