<script setup lang="ts">
/**
 * 审计日志（M8）：租户内敏感操作留痕，只读。
 *
 * 入口放在管理后台（AdminView）而不是独立菜单：它的使用频率是"出事才查"，
 * 与用量成本、成员管理放在一起更合理。
 */
import { onMounted, ref } from 'vue'

import { listAuditLogs } from '@/api/support'
import type { AuditLogInfo } from '@/api/types'

const ACTION_LABELS: Record<string, string> = {
  'member.add': '新增成员',
  'member.role': '变更角色',
  'member.remove': '移除成员',
  'tenant.update': '修改租户信息',
  'tenant.quota': '修改配额',
  'quick_reply.create': '新建快捷回复',
  'quick_reply.update': '编辑快捷回复',
  'quick_reply.delete': '删除快捷回复',
}

const items = ref<AuditLogInfo[]>([])
const loading = ref(false)

function formatTime(value: string): string {
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

function formatDetail(detail: Record<string, unknown>): string {
  const entries = Object.entries(detail)
  if (!entries.length) return '-'
  return entries.map(([key, value]) => `${key}: ${String(value)}`).join('，')
}

async function load(): Promise<void> {
  loading.value = true
  try {
    items.value = await listAuditLogs()
  } catch {
    // 拦截器已提示
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="audit">
    <header class="audit__header">
      <div>
        <h2 class="audit__title">审计日志</h2>
        <p class="audit__hint">成员变更、租户配置与话术库的修改记录，仅 owner/admin 可查。</p>
      </div>
      <el-button @click="load">刷新</el-button>
    </header>

    <el-card v-loading="loading" shadow="never">
      <el-empty v-if="!items.length" description="暂无操作记录" />
      <el-table v-else :data="items" size="default">
        <el-table-column label="操作" min-width="120">
          <template #default="{ row }">
            {{ ACTION_LABELS[row.action] ?? row.action }}
          </template>
        </el-table-column>
        <el-table-column prop="actor_name" label="操作人" min-width="120" />
        <el-table-column label="对象" min-width="160">
          <template #default="{ row }">
            <!-- 租户级操作没有具体对象 ID（如改名、改配额），显示为"租户自身" -->
            <span v-if="row.target_id">{{ row.target_type }} / {{ row.target_id.slice(0, 8) }}</span>
            <span v-else>租户自身</span>
          </template>
        </el-table-column>
        <el-table-column label="详情" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">{{ formatDetail(row.detail) }}</template>
        </el-table-column>
        <el-table-column label="时间" width="180">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.audit__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.audit__title {
  margin: 0 0 4px;
  font-size: 16px;
  font-weight: 650;
}
.audit__hint {
  margin: 0;
  font-size: 12.5px;
  color: var(--el-text-color-secondary);
}
</style>
