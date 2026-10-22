<script setup lang="ts">
/**
 * 客户管理（M8）：按 owner 聚合的客户视图。
 *
 * 没有独立客户档案表：客户就是"这段会话属于谁"。点击客户名跳到
 * 知识库/坐席都拿不到的会话明细不在此页做——当前版本只提供汇总，
 * 会话明细在坐席台按会话查看。
 */
import { onMounted, ref } from 'vue'

import { listCustomers } from '@/api/support'
import type { CustomerInfo } from '@/api/types'

const items = ref<CustomerInfo[]>([])
const total = ref(0)
const loading = ref(false)

const STATUS_LABELS: Record<string, string> = {
  ai: 'AI 服务中',
  pending_handoff: '等待人工',
  human: '人工中',
  closed: '已结束',
}

function formatTime(value: string | null): string {
  if (!value) return '-'
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const data = await listCustomers(100)
    items.value = data.items
    total.value = data.total
  } catch {
    // 拦截器已提示
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="customers">
    <header class="customers__header">
      <div>
        <h2 class="customers__title">客户管理</h2>
        <p class="customers__hint">
          共 {{ total }} 个客户（登录用户 + 匿名访客各按一个身份聚合，访客合并账号后归并到用户）。
        </p>
      </div>
      <el-button @click="load">刷新</el-button>
    </header>

    <el-card v-loading="loading" shadow="never">
      <el-empty v-if="!items.length" description="暂无客户" />
      <el-table v-else :data="items" size="default">
        <el-table-column label="客户" min-width="200">
          <template #default="{ row }">
            <p class="customers__name">{{ row.display_name }}</p>
            <p class="customers__email">{{ row.email ?? '访客（未登录）' }}</p>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.owner_type === 'user' ? 'success' : 'info'" effect="plain">
              {{ row.owner_type === 'user' ? '注册用户' : '访客' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="conversation_count" label="会话数" width="90" />
        <el-table-column label="最近状态" width="120">
          <template #default="{ row }">
            {{ (STATUS_LABELS[row.last_conversation_status] ?? row.last_conversation_status) || '-' }}
          </template>
        </el-table-column>
        <el-table-column label="最近活跃" width="180">
          <template #default="{ row }">{{ formatTime(row.last_active_at) }}</template>
        </el-table-column>
        <el-table-column label="满意度" width="110">
          <template #default="{ row }">
            <span v-if="row.total_ratings">{{ row.avg_rating?.toFixed(1) }}（{{ row.total_ratings }}）</span>
            <span v-else class="customers__muted">未评价</span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.customers__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.customers__title {
  margin: 0 0 4px;
  font-size: 16px;
  font-weight: 650;
}
.customers__hint {
  margin: 0;
  font-size: 12.5px;
  color: var(--el-text-color-secondary);
}
.customers__name {
  margin: 0;
  font-weight: 600;
}
.customers__email {
  margin: 2px 0 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.customers__muted {
  color: var(--el-text-color-placeholder);
}
</style>
