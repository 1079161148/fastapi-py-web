<script setup lang="ts">
/**
 * 管理后台（M7）：用量成本看板 + 租户成员管理。
 *
 * 权限：页面只对租户 owner/admin 有意义（全局 role=admin）。非管理员不请求接口——
 * 否则每次进来都会弹一次 403，体验很差；真正的门禁在后端 require_tenant_admin。
 *
 * 成本金额在未配置价格时恒为 0：这是"没配价目"而不是"没花钱"，
 * 页面上明确标注，避免误读。
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, ref } from 'vue'

import {
  addMember,
  getUsageOverview,
  listMembers,
  removeMember,
  renameTenant,
  updateMemberRole,
  updateQuota,
} from '@/api/admin'
import { api } from '@/api/client'
import type {
  TenantInfo,
  TenantMemberCreatePayload,
  TenantMemberInfo,
  TenantQuotaUpdatePayload,
  TenantRole,
  UsageOverview,
} from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import AuditLogsView from '@/views/AuditLogsView.vue'

const auth = useAuthStore()

// 与后端 require_tenant_admin 对齐：owner 与 admin 等价
const canManage = computed(() => ['owner', 'admin'].includes(auth.user?.tenant_role ?? ''))
const loading = ref(false)
const overview = ref<UsageOverview | null>(null)
const members = ref<TenantMemberInfo[]>([])
const tenant = ref<TenantInfo | null>(null)

const editingName = ref(false)
const nameDraft = ref('')

const memberDialog = ref(false)
const savingMember = ref(false)
const memberForm = ref<TenantMemberCreatePayload>({
  email: '',
  username: '',
  password: '',
  display_name: '',
  role: 'agent',
})

const quotaDialog = ref(false)
const savingQuota = ref(false)
/** 四个键都必填（Required）：表单不能留 undefined，否则 el-input-number 会显示空 */
const quotaForm = ref<Required<TenantQuotaUpdatePayload>>({
  max_messages_per_day: 0,
  max_concurrent: 0,
  max_storage_gb: 0,
  max_tokens_per_day: 0,
})

const ROLE_LABELS: Record<TenantRole, string> = {
  owner: '所有者',
  admin: '管理员',
  agent: '坐席',
  customer: '客户',
}
const ROLE_OPTIONS: TenantRole[] = ['owner', 'admin', 'agent', 'customer']

const quotaPercent = computed(() => {
  const quota = overview.value?.quota
  if (!quota || quota.tokens_limit <= 0) return 0
  return Math.min(Math.round((quota.tokens_used_today / quota.tokens_limit) * 100), 100)
})

const quotaStatus = computed(() => {
  if (quotaPercent.value >= 100) return 'exception'
  if (quotaPercent.value >= 80) return 'warning'
  return 'success'
})

function formatNumber(value: number): string {
  return value.toLocaleString('zh-CN')
}

function formatCost(value: number): string {
  // 只显示数字，"未配置价格"的解释放在卡片脚注里：
  // 26px 的主数值塞长句会把卡片撑变形
  return value.toFixed(4)
}

/** 用当前配额回填表单：后端 quota 是松散 dict，缺键按 0（不限）处理 */
function openQuotaDialog(): void {
  const quota = (tenant.value?.quota ?? {}) as Record<string, number | undefined>
  quotaForm.value = {
    max_messages_per_day: quota.max_messages_per_day ?? 0,
    max_concurrent: quota.max_concurrent ?? 0,
    max_storage_gb: quota.max_storage_gb ?? 0,
    max_tokens_per_day: quota.max_tokens_per_day ?? 0,
  }
  quotaDialog.value = true
}

async function saveQuota(): Promise<void> {
  savingQuota.value = true
  try {
    tenant.value = await updateQuota(quotaForm.value)
    // 看板上的"当日配额"直接读 usage 接口，改完必须重新拉一次
    overview.value = await getUsageOverview()
    quotaDialog.value = false
    ElMessage.success('配额已更新')
  } catch {
    // 拦截器已统一提示
  } finally {
    savingQuota.value = false
  }
}

async function load() {
  if (!canManage.value) return
  loading.value = true
  try {
    const [usage, memberList, current] = await Promise.all([
      getUsageOverview(),
      listMembers(),
      api.get<TenantInfo>('/v1/tenants/current'),
    ])
    overview.value = usage
    members.value = memberList
    tenant.value = current
    nameDraft.value = current.name
  } catch {
    // 拦截器已统一提示
  } finally {
    loading.value = false
  }
}

async function saveName() {
  const name = nameDraft.value.trim()
  if (!name) {
    ElMessage.warning('租户名不能为空')
    return
  }
  const updated = await renameTenant(name)
  tenant.value = updated
  editingName.value = false
  ElMessage.success('租户名已更新')
}

async function changeRole(member: TenantMemberInfo, role: TenantRole) {
  if (member.role === role) return
  try {
    await updateMemberRole(member.user_id, role)
    ElMessage.success('角色已更新')
  } finally {
    // 无论成功失败都重新拉取：失败时把下拉框回滚成服务端的真实值
    await load()
  }
}

async function confirmRemove(member: TenantMemberInfo) {
  try {
    await ElMessageBox.confirm(
      `确定移除 ${member.email}？该账号将被禁用，已签发的登录凭证立即失效。`,
      '移除成员',
      { type: 'warning', confirmButtonText: '移除', cancelButtonText: '取消' },
    )
  } catch {
    // 取消不是错误：不捕获会在控制台留下未处理的 Promise 拒绝
    return
  }
  await removeMember(member.user_id)
  ElMessage.success('成员已移除')
  await load()
}

async function submitMember() {
  if (!memberForm.value.email.trim()) {
    ElMessage.warning('请填写邮箱')
    return
  }
  if (memberForm.value.password.length < 8) {
    ElMessage.warning('初始密码至少 8 位')
    return
  }
  savingMember.value = true
  try {
    await addMember({
      email: memberForm.value.email.trim(),
      username: memberForm.value.username?.trim() || undefined,
      password: memberForm.value.password,
      display_name: memberForm.value.display_name?.trim() || undefined,
      role: memberForm.value.role,
    })
    ElMessage.success('成员已创建')
    memberDialog.value = false
    memberForm.value = {
      email: '',
      username: '',
      password: '',
      display_name: '',
      role: 'agent',
    }
    await load()
  } finally {
    savingMember.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="admin">
    <!-- 标题由外壳顶栏承担，这里只放说明，避免同一句话出现两次 -->
    <p class="page-header__desc">租户用量成本、配额与成员权限管理</p>

    <el-alert
      v-if="!canManage"
      type="warning"
      :closable="false"
      show-icon
      title="当前角色无权访问"
      description="用量与成员管理只对租户所有者/管理员开放。"
    />

    <template v-else>
      <el-card v-loading="loading" class="admin__card">
        <template #header>
          <div class="admin__card-header">
            <span>租户信息</span>
            <el-button v-if="!editingName" link type="primary" @click="editingName = true">
              修改名称
            </el-button>
          </div>
        </template>
        <div v-if="editingName" class="admin__name-edit">
          <el-input v-model="nameDraft" maxlength="100" style="max-width: 320px" />
          <el-button type="primary" @click="saveName">保存</el-button>
          <el-button @click="editingName = false">取消</el-button>
        </div>
        <el-descriptions v-else :column="3" border>
          <el-descriptions-item label="名称">{{ tenant?.name ?? '-' }}</el-descriptions-item>
          <el-descriptions-item label="标识">{{ tenant?.slug ?? '-' }}</el-descriptions-item>
          <el-descriptions-item label="状态">{{ tenant?.status ?? '-' }}</el-descriptions-item>
        </el-descriptions>
      </el-card>

      <el-card v-loading="loading" class="admin__card">
        <template #header>用量与成本（今日）</template>
        <div class="admin__stats">
          <div class="admin__stat">
            <div class="admin__stat-label">Token 用量</div>
            <div class="admin__stat-value">{{ formatNumber(overview?.today.total_tokens ?? 0) }}</div>
          </div>
          <div class="admin__stat">
            <div class="admin__stat-label">请求次数</div>
            <div class="admin__stat-value">{{ formatNumber(overview?.today.requests ?? 0) }}</div>
          </div>
          <div class="admin__stat">
            <div class="admin__stat-label">近 7 天 Token</div>
            <div class="admin__stat-value">
              {{ formatNumber(overview?.last_7_days.total_tokens ?? 0) }}
            </div>
          </div>
          <div class="admin__stat">
            <div class="admin__stat-label">今日成本</div>
            <div class="admin__stat-value">{{ formatCost(overview?.today.cost ?? 0) }}</div>
            <div class="admin__stat-note">未配置价格时恒为 0</div>
          </div>
        </div>

        <div class="admin__quota">
          <div class="admin__quota-head">
            <span class="admin__stat-label">
              当日配额：{{ formatNumber(overview?.quota.tokens_used_today ?? 0) }} /
              {{ overview?.quota.tokens_limit ? formatNumber(overview.quota.tokens_limit) : '不限' }}
            </span>
            <el-button link size="small" :disabled="!canManage" @click="openQuotaDialog">
              调整配额
            </el-button>
          </div>
          <el-progress
            v-if="overview?.quota.tokens_limit"
            :percentage="quotaPercent"
            :status="quotaStatus"
          />
        </div>

        <el-table :data="overview?.daily ?? []" size="small" class="admin__table">
          <el-table-column prop="day" label="日期" width="140" />
          <el-table-column prop="requests" label="请求数" width="110" />
          <el-table-column label="Token">
            <template #default="{ row }">{{ formatNumber(row.total_tokens) }}</template>
          </el-table-column>
          <el-table-column label="成本">
            <template #default="{ row }">{{ row.cost.toFixed(4) }}</template>
          </el-table-column>
        </el-table>
      </el-card>

      <el-card v-loading="loading" class="admin__card">
        <template #header>
          <div class="admin__card-header">
            <span>成员（{{ members.length }}）</span>
            <el-button type="primary" size="small" @click="memberDialog = true">
              新增成员
            </el-button>
          </div>
        </template>
        <el-table :data="members" size="small">
          <el-table-column prop="email" label="邮箱" min-width="200" />
          <el-table-column label="用户名" width="130">
            <template #default="{ row }">{{ row.username || '—' }}</template>
          </el-table-column>
          <el-table-column prop="display_name" label="名称" width="120" />
          <el-table-column label="角色" width="150">
            <template #default="{ row }">
              <el-select
                :model-value="row.role"
                size="small"
                @change="(value: TenantRole) => changeRole(row, value)"
              >
                <el-option
                  v-for="role in ROLE_OPTIONS"
                  :key="role"
                  :label="ROLE_LABELS[role]"
                  :value="role"
                />
              </el-select>
            </template>
          </el-table-column>
          <el-table-column prop="status" label="状态" width="110" />
          <el-table-column label="操作" width="100">
            <template #default="{ row }">
              <el-button link type="danger" @click="confirmRemove(row)">移除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <p class="admin__hint">
          成员账号由租户创建（单主租户模型）：邮箱已被其他租户使用时无法邀请。
          最后一个所有者不可降级或移除。
        </p>
      </el-card>

      <!-- 审计日志：使用频率低（出事才查），放页面最底部，不单独占一级菜单 -->
      <AuditLogsView />
    </template>

    <el-dialog v-model="memberDialog" title="新增成员" width="480px">
      <el-form label-width="80px">
        <el-form-item label="邮箱" required>
          <el-input v-model="memberForm.email" placeholder="member@example.com" />
        </el-form-item>
        <el-form-item label="用户名">
          <el-input v-model="memberForm.username" placeholder="可选；留空则只能用邮箱登录" />
        </el-form-item>
        <el-form-item label="初始密码" required>
          <el-input
            v-model="memberForm.password"
            type="password"
            show-password
            placeholder="至少 8 位"
          />
        </el-form-item>
        <el-form-item label="名称">
          <el-input v-model="memberForm.display_name" placeholder="留空则取邮箱前缀" />
        </el-form-item>
        <el-form-item label="角色">
          <el-select v-model="memberForm.role">
            <el-option label="管理员" value="admin" />
            <el-option label="坐席" value="agent" />
            <el-option label="客户" value="customer" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="memberDialog = false">取消</el-button>
        <el-button type="primary" :loading="savingMember" @click="submitMember">创建</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="quotaDialog" title="调整配额" width="460px">
      <el-form label-width="120px">
        <el-form-item label="每日消息数">
          <el-input-number
            v-model="quotaForm.max_messages_per_day"
            :min="0"
            :step="100"
            controls-position="right"
          />
        </el-form-item>
        <el-form-item label="并发会话数">
          <el-input-number
            v-model="quotaForm.max_concurrent"
            :min="0"
            controls-position="right"
          />
        </el-form-item>
        <el-form-item label="存储上限（GB）">
          <el-input-number
            v-model="quotaForm.max_storage_gb"
            :min="0"
            controls-position="right"
          />
        </el-form-item>
        <el-form-item label="每日 Token">
          <el-input-number
            v-model="quotaForm.max_tokens_per_day"
            :min="0"
            :step="10000"
            controls-position="right"
          />
        </el-form-item>
      </el-form>
      <p class="admin__hint">
        0 表示不限。配额按库内当日用量判定（多 worker 下内存计数不准），保存后立即生效。
      </p>
      <template #footer>
        <el-button @click="quotaDialog = false">取消</el-button>
        <el-button type="primary" :loading="savingQuota" @click="saveQuota">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
/* 外壳已提供页边距与最大宽度，这里只负责内部纵向节奏 */
.admin {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.admin__card {
  margin-top: 0;
}
.admin__card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.admin__stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
}
.admin__stat-label {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.admin__stat-value {
  margin-top: 4px;
  font-size: 22px;
  font-weight: 600;
}
.admin__stat-note {
  color: var(--app-text-3);
  font-size: 11px;
}
.admin__quota {
  margin-top: 16px;
}
.admin__quota-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.admin__table {
  margin-top: 16px;
}
.admin__hint {
  margin-top: 12px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.admin__name-edit {
  display: flex;
  gap: 8px;
}
</style>
