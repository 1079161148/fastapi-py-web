<script setup lang="ts">
/**
 * 知识库管理（设计文档 5.2 / 9.4，M3）。
 *
 * 上传后接口立即返回，解析/向量化在 arq worker 中异步进行；
 * 页面在有 uploaded/processing 文档时轮询（3s），全部完成后自动停止，
 * 避免无意义的持续请求。
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import type { UploadRequestOptions } from 'element-plus'
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'

import { ApiError } from '@/api/client'
import {
  createKnowledgeBase,
  deleteDocument,
  listDocuments,
  listKnowledgeBases,
  reprocessDocument,
  searchKnowledge,
  uploadDocument,
} from '@/api/knowledge'
import type {
  ChunkStrategy,
  DocumentInfo,
  DocumentStatus,
  KnowledgeBaseInfo,
  SearchHit,
} from '@/api/types'

const POLL_INTERVAL_MS = 3000

const bases = ref<KnowledgeBaseInfo[]>([])
const activeKbId = ref<string | null>(null)
const documents = ref<DocumentInfo[]>([])
const loading = ref(false)
const uploading = ref(false)
const creating = ref(false)
const dialogVisible = ref(false)
const newBase = reactive<{
  name: string
  description: string
  chunk_strategy: ChunkStrategy
}>({
  name: '',
  description: '',
  chunk_strategy: 'by_heading',
})

const searchQuery = ref('')
const searching = ref(false)
const searchHits = ref<SearchHit[]>([])
const searchNotice = ref('')

/** 检索示例：演示环境的常见问法，点击填入并直接检索；换知识库后可按内容调整 */
const EXAMPLE_QUERIES = ['7 天内怎么退款', '电子发票怎么开', '超过 7 天还能退吗']

let pollTimer: ReturnType<typeof setInterval> | null = null

const activeKb = computed(() => bases.value.find((item) => item.id === activeKbId.value) ?? null)
const hasPending = computed(() =>
  documents.value.some((item) => item.status === 'uploaded' || item.status === 'processing'),
)

const STATUS_TEXT: Record<DocumentStatus, string> = {
  uploaded: '待解析',
  processing: '解析中',
  done: '已完成',
  failed: '失败',
}
const STATUS_TYPE: Record<DocumentStatus, 'info' | 'warning' | 'success' | 'danger'> = {
  uploaded: 'info',
  processing: 'warning',
  done: 'success',
  failed: 'danger',
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.body.message : fallback
}

/** 点击示例问题：填入输入框并立即检索，省去用户想问法的成本 */
function onExample(query: string): void {
  searchQuery.value = query
  void onSearch()
}

async function loadBases(): Promise<void> {
  try {
    bases.value = await listKnowledgeBases()
    if (!activeKbId.value && bases.value.length > 0) {
      activeKbId.value = bases.value[0].id
    }
  } catch {
    // 拦截器已提示
  }
}

async function loadDocuments(): Promise<void> {
  if (!activeKbId.value) {
    documents.value = []
    return
  }
  try {
    documents.value = (await listDocuments(activeKbId.value)).items
  } catch {
    // 拦截器已提示
  }
}

async function refreshAll(): Promise<void> {
  loading.value = true
  try {
    await loadBases()
    await loadDocuments()
  } finally {
    loading.value = false
  }
}

function stopPolling(): void {
  if (pollTimer !== null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function startPolling(): void {
  if (pollTimer !== null) return
  pollTimer = setInterval(() => {
    void loadDocuments()
    void loadBases()
  }, POLL_INTERVAL_MS)
}

watch(hasPending, (pending) => {
  if (pending) startPolling()
  else stopPolling()
})

watch(activeKbId, () => {
  void loadDocuments()
})

onMounted(() => {
  void refreshAll()
})

onBeforeUnmount(() => {
  stopPolling()
})

async function onCreateBase(): Promise<void> {
  if (!newBase.name.trim()) return
  creating.value = true
  try {
    const created = await createKnowledgeBase({
      name: newBase.name.trim(),
      description: newBase.description.trim(),
      chunk_strategy: newBase.chunk_strategy,
    })
    ElMessage.success('知识库已创建')
    dialogVisible.value = false
    newBase.name = ''
    newBase.description = ''
    activeKbId.value = created.id
    await refreshAll()
  } catch (error) {
    ElMessage.error(errorMessage(error, '创建失败'))
  } finally {
    creating.value = false
  }
}

async function onUpload(options: UploadRequestOptions): Promise<void> {
  if (!activeKbId.value) {
    ElMessage.warning('请先选择或创建知识库')
    return
  }
  uploading.value = true
  try {
    const document = await uploadDocument(activeKbId.value, options.file)
    ElMessage.success(`${document.filename} 已上传，正在后台解析`)
    await refreshAll()
  } catch (error) {
    ElMessage.error(errorMessage(error, '上传失败'))
  } finally {
    uploading.value = false
  }
}

async function onReprocess(document: DocumentInfo): Promise<void> {
  try {
    await reprocessDocument(document.id)
    ElMessage.success('已重新入队解析')
    await refreshAll()
  } catch (error) {
    ElMessage.error(errorMessage(error, '重新解析失败'))
  }
}

async function onDelete(document: DocumentInfo): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `删除《${document.filename}》会同时清除其全部知识片段，且不可恢复。`,
      '确认删除',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return // 用户取消
  }
  try {
    await deleteDocument(document.id)
    ElMessage.success('已删除')
    await refreshAll()
  } catch (error) {
    ElMessage.error(errorMessage(error, '删除失败'))
  }
}

async function onSearch(): Promise<void> {
  // searching 守卫：按钮 loading 自带禁用，但回车与示例链接不走按钮状态，这里兜底防并发
  if (searching.value) return
  const query = searchQuery.value.trim()
  if (!query) return
  searching.value = true
  searchNotice.value = ''
  try {
    const result = await searchKnowledge(query, activeKbId.value ?? undefined)
    searchHits.value = result.items
    searchNotice.value = result.degraded ? result.reason : ''
  } catch (error) {
    searchHits.value = []
    ElMessage.error(errorMessage(error, '检索失败'))
  } finally {
    searching.value = false
  }
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}
</script>

<template>
  <div class="knowledge">
    <header class="knowledge__header">
      <div>
        <p class="knowledge__hint">
          上传 PDF / DOCX / Markdown / TXT，后台自动分块并建立向量与关键词索引；对话时自动引用。
        </p>
      </div>
      <div class="knowledge__actions">
        <el-button type="primary" @click="dialogVisible = true">新建知识库</el-button>
      </div>
    </header>

    <el-card shadow="never" class="knowledge__toolbar">
      <div class="knowledge__row">
        <el-select
          v-model="activeKbId"
          placeholder="选择知识库"
          class="knowledge__select"
          :disabled="!bases.length"
        >
          <el-option
            v-for="item in bases"
            :key="item.id"
            :label="`${item.name}（${item.document_count} 篇）`"
            :value="item.id"
          />
        </el-select>

        <el-upload
          :auto-upload="true"
          :show-file-list="false"
          :http-request="onUpload"
          accept=".pdf,.docx,.md,.markdown,.txt"
          :disabled="!activeKbId || uploading"
        >
          <el-button type="primary" :loading="uploading" :disabled="!activeKbId">
            上传文档
          </el-button>
        </el-upload>

        <el-tag v-if="activeKb" type="info" effect="plain">
          分块策略：{{ activeKb.chunk_strategy === 'by_heading' ? '按标题' : '固定窗口' }}
        </el-tag>
      </div>
    </el-card>

    <el-card v-loading="loading" shadow="never" class="knowledge__card">
      <template #header>
        <span>文档列表</span>
      </template>
      <el-empty v-if="!documents.length" description="还没有文档，先上传一份吧" />
      <el-table v-else :data="documents" size="small">
        <el-table-column prop="filename" label="文件名" min-width="200" show-overflow-tooltip />
        <el-table-column label="大小" width="100">
          <template #default="{ row }">{{ formatSize(row.size_bytes) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tooltip :disabled="!row.error_msg" :content="row.error_msg" placement="top">
              <el-tag :type="STATUS_TYPE[row.status as DocumentStatus]" size="small">
                {{ STATUS_TEXT[row.status as DocumentStatus] }}
              </el-tag>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column prop="chunk_count" label="片段数" width="90" />
        <el-table-column label="上传时间" width="170">
          <template #default="{ row }">
            {{ new Date(row.created_at).toLocaleString() }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="150">
          <template #default="{ row }">
            <el-button link type="primary" @click="onReprocess(row)">重新解析</el-button>
            <el-button link type="danger" @click="onDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <p v-if="hasPending" class="knowledge__hint knowledge__hint--pending">
        有文档正在解析，页面会自动刷新（每 {{ POLL_INTERVAL_MS / 1000 }} 秒）。
      </p>
    </el-card>

    <el-card shadow="never" class="knowledge__card">
      <template #header>
        <span>检索结果</span>
      </template>
      <div class="knowledge__row">
        <el-input
          v-model="searchQuery"
          placeholder="例如：7 天内怎么退款"
          class="knowledge__search"
          clearable
          @keyup.enter="onSearch"
        />
        <el-button type="primary" :loading="searching" @click="onSearch">检索</el-button>
      </div>
      <div class="knowledge__examples">
        <span class="knowledge__examples-label">不知道问什么？试试：</span>
        <el-button
          v-for="example in EXAMPLE_QUERIES"
          :key="example"
          link
          type="primary"
          size="small"
          :disabled="searching"
          @click="onExample(example)"
        >
          {{ example }}
        </el-button>
      </div>

      <el-alert
        v-if="searchNotice"
        class="knowledge__notice"
        type="warning"
        :closable="false"
        show-icon
        :title="searchNotice"
      />

      <el-empty
        v-if="!searchHits.length && !searching"
        description="输入问题后查看命中的知识片段，或点击上方示例直接体验"
      />
      <ol v-else class="hits">
        <li v-for="hit in searchHits" :key="hit.chunk_id" class="hits__item">
          <div class="hits__head">
            <span class="hits__doc">{{ hit.document_name }}</span>
            <el-tag size="small" effect="plain">
              融合 {{ hit.score.toFixed(4) }}
            </el-tag>
            <el-tag v-if="hit.vector_score !== null" size="small" type="info" effect="plain">
              向量 {{ hit.vector_score.toFixed(3) }}
            </el-tag>
            <el-tag v-if="hit.keyword_score !== null" size="small" type="success" effect="plain">
              关键词 {{ hit.keyword_score.toFixed(3) }}
            </el-tag>
            <el-tag v-if="hit.reranked" size="small" type="warning" effect="plain">已重排</el-tag>
          </div>
          <p class="hits__content">{{ hit.content }}</p>
        </li>
      </ol>
    </el-card>

    <el-dialog v-model="dialogVisible" title="新建知识库" width="440px">
      <el-form label-position="top">
        <el-form-item label="名称" required>
          <el-input v-model="newBase.name" maxlength="100" placeholder="例如：售后政策" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input
            v-model="newBase.description"
            type="textarea"
            :rows="2"
            maxlength="500"
            placeholder="可选，便于团队区分用途"
          />
        </el-form-item>
        <el-form-item label="分块策略">
          <el-select v-model="newBase.chunk_strategy" class="knowledge__select">
            <el-option label="按标题层级（推荐，Markdown/DOCX）" value="by_heading" />
            <el-option label="固定窗口（无标题结构时）" value="fixed_window" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button
          type="primary"
          :loading="creating"
          :disabled="!newBase.name.trim()"
          @click="onCreateBase"
        >
          创建
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
/* 外壳已提供页边距与最大宽度，这里只负责内部纵向节奏 */
.knowledge {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.knowledge__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.knowledge__header h1 {
  margin: 0 0 4px;
  font-size: 22px;
  font-weight: 650;
  letter-spacing: -0.01em;
}
.knowledge__hint {
  margin: 0;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.knowledge__hint--pending {
  margin-top: 12px;
}
.knowledge__actions {
  display: flex;
  align-items: center;
  gap: 8px;
  white-space: nowrap;
}
.knowledge__toolbar {
  margin-bottom: 16px;
}
.knowledge__row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.knowledge__select {
  width: 260px;
}
.knowledge__examples {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 8px;
}
.knowledge__examples-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.knowledge__card {
  margin-bottom: 16px;
}
.knowledge__search {
  flex: 1;
  min-width: 260px;
}
.knowledge__notice {
  margin-top: 12px;
}
.hits {
  margin: 12px 0 0;
  padding-left: 20px;
}
.hits__item {
  margin-bottom: 12px;
}
.hits__head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.hits__doc {
  font-weight: 600;
}
.hits__content {
  margin: 4px 0 0;
  color: var(--el-text-color-regular);
  font-size: 13px;
  line-height: 1.7;
  white-space: pre-wrap;
}
</style>
