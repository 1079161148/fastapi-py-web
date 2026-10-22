<script setup lang="ts">
/**
 * 快捷回复管理（M8）：坐席常用话术库。
 *
 * 分组固定四种（问候/FAQ/结束/其他）：运营可以随意建分组的日子，
 * 就是筛选器里出现 30 个几乎相同的分组的日子。
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'

import {
  createQuickReply,
  deleteQuickReply,
  listQuickReplies,
  updateQuickReply,
} from '@/api/support'
import type { QuickReplyInfo } from '@/api/types'

const CATEGORY_LABELS: Record<string, string> = {
  greeting: '问候语',
  faq: '常见问题',
  closing: '结束语',
  other: '其他',
}

const items = ref<QuickReplyInfo[]>([])
const loading = ref(false)
const dialogVisible = ref(false)
const saving = ref(false)
const editingId = ref('')

const form = reactive({
  title: '',
  content: '',
  category: 'faq',
  sort_order: 0,
})

function openCreate(): void {
  editingId.value = ''
  form.title = ''
  form.content = ''
  form.category = 'faq'
  form.sort_order = 0
  dialogVisible.value = true
}

function openEdit(row: QuickReplyInfo): void {
  editingId.value = row.id
  form.title = row.title
  form.content = row.content
  form.category = row.category
  form.sort_order = row.sort_order
  dialogVisible.value = true
}

async function load(): Promise<void> {
  loading.value = true
  try {
    items.value = await listQuickReplies()
  } catch {
    // 拦截器已提示
  } finally {
    loading.value = false
  }
}

async function onSave(): Promise<void> {
  if (!form.title.trim() || !form.content.trim()) {
    ElMessage.warning('标题和内容不能为空')
    return
  }
  saving.value = true
  try {
    if (editingId.value) {
      await updateQuickReply(editingId.value, { ...form })
      ElMessage.success('已更新')
    } else {
      await createQuickReply({ ...form })
      ElMessage.success('已创建')
    }
    dialogVisible.value = false
    await load()
  } catch {
    // 拦截器已提示
  } finally {
    saving.value = false
  }
}

async function onDelete(row: QuickReplyInfo): Promise<void> {
  try {
    await ElMessageBox.confirm(`删除「${row.title}」后无法恢复，确定删除吗？`, '删除快捷回复', {
      type: 'warning',
      confirmButtonText: '删除',
    })
  } catch {
    return // 取消不是错误
  }
  try {
    await deleteQuickReply(row.id)
    ElMessage.success('已删除')
    await load()
  } catch {
    // 拦截器已提示
  }
}

onMounted(load)
</script>

<template>
  <div class="qr">
    <header class="qr__header">
      <div>
        <h2 class="qr__title">快捷回复</h2>
        <p class="qr__hint">坐席回复时一键插入的常用话术，按分组筛选。</p>
      </div>
      <el-button type="primary" @click="openCreate">新建话术</el-button>
    </header>

    <el-card v-loading="loading" shadow="never">
      <el-empty v-if="!items.length" description="还没有话术，先建一条吧" />
      <el-table v-else :data="items" size="default">
        <el-table-column prop="title" label="标题" min-width="140" />
        <el-table-column prop="content" label="内容" min-width="300" show-overflow-tooltip />
        <el-table-column label="分组" width="110">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">
              {{ CATEGORY_LABELS[row.category] ?? row.category }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="sort_order" label="排序" width="80" />
        <el-table-column label="操作" width="140">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button link type="danger" @click="onDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="dialogVisible" :title="editingId ? '编辑话术' : '新建话术'" width="520px">
      <el-form label-width="72px">
        <el-form-item label="标题" required>
          <el-input v-model="form.title" maxlength="100" placeholder="例如：退款进度查询" />
        </el-form-item>
        <el-form-item label="内容" required>
          <el-input
            v-model="form.content"
            type="textarea"
            :rows="5"
            maxlength="2000"
            show-word-limit
            placeholder="发送给用户的完整内容"
          />
        </el-form-item>
        <el-form-item label="分组">
          <el-select v-model="form.category">
            <el-option
              v-for="(label, key) in CATEGORY_LABELS"
              :key="key"
              :label="label"
              :value="key"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="排序">
          <el-input-number v-model="form.sort_order" :min="0" :max="9999" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="onSave">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.qr__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.qr__title {
  margin: 0 0 4px;
  font-size: 16px;
  font-weight: 650;
}
.qr__hint {
  margin: 0;
  font-size: 12.5px;
  color: var(--el-text-color-secondary);
}
</style>
