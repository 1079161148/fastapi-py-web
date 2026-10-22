<script setup lang="ts">
/**
 * useTable 用法示例（后续列表页照此模板写）。
 *
 * 演示点：显式查询、页码越界钳制、多选、刷新。
 * 真实页面把 mockFetcher 换成 client.get 即可，调用方式不变。
 */
import type { PageQuery, PageResult } from '@/api/types'
import { useTable } from '@/composables/useTable'

interface DemoRow {
  id: number
  name: string
  status: 'active' | 'disabled'
  updatedAt: string
}

const MOCK_TOTAL = 57

function mockFetcher(params: PageQuery): Promise<PageResult<DemoRow>> {
  const keyword = params.keyword?.trim() ?? ''
  const list: DemoRow[] = []
  for (let index = 0; index < params.pageSize; index += 1) {
    const id = (params.page - 1) * params.pageSize + index + 1
    if (id > MOCK_TOTAL) break
    list.push({
      id,
      name: keyword ? `${keyword}-记录-${id}` : `记录-${id}`,
      status: id % 3 === 0 ? 'disabled' : 'active',
      updatedAt: new Date(Date.now() - id * 86_400_000).toISOString().slice(0, 10),
    })
  }
  // 模拟网络延迟，用于观察 loading 态与竞态防护效果
  return new Promise((resolve) => {
    setTimeout(() => resolve({ list, total: MOCK_TOTAL }), 300)
  })
}

const {
  list,
  loading,
  total,
  page,
  pageSize,
  queryState,
  selectedRows,
  search,
  reset,
  refresh,
  handleSizeChange,
  handleCurrentChange,
  handleSelectionChange,
  clearSelection,
} = useTable<DemoRow, { keyword: string }>({
  fetcher: mockFetcher,
  query: { keyword: '' },
  pageSize: 10,
  rowKey: 'id',
  onError: (error) => console.warn('[table-demo] fetch failed', error),
})

const statusTag = (status: DemoRow['status']) => (status === 'active' ? 'success' : 'info')
const statusText = (status: DemoRow['status']) => (status === 'active' ? '启用' : '禁用')
</script>

<template>
  <div class="page">
    <h2>useTable 封装示例</h2>

    <el-form :inline="true" class="page__filter">
      <el-form-item label="关键词">
        <el-input v-model="queryState.keyword" placeholder="输入后点击查询" clearable @keyup.enter="search" />
      </el-form-item>
      <el-form-item>
        <el-button type="primary" :loading="loading" @click="search">查询</el-button>
        <el-button :loading="loading" @click="reset">重置</el-button>
        <el-button :loading="loading" @click="refresh">刷新</el-button>
      </el-form-item>
    </el-form>

    <el-alert v-if="selectedRows.length > 0" type="info" :closable="false" class="page__selection">
      已选择 {{ selectedRows.length }} 项
      <el-button link type="primary" @click="clearSelection">清空选择</el-button>
    </el-alert>

    <el-table v-loading="loading" :data="list" row-key="id" @selection-change="handleSelectionChange">
      <el-table-column type="selection" width="48" />
      <el-table-column prop="id" label="ID" width="80" />
      <el-table-column prop="name" label="名称" />
      <el-table-column prop="status" label="状态" width="120">
        <template #default="{ row }">
          <el-tag :type="statusTag(row.status)">{{ statusText(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="updatedAt" label="更新时间" width="160" />
    </el-table>

    <el-pagination
      class="page__pager"
      :current-page="page"
      :page-size="pageSize"
      :total="total"
      :page-sizes="[10, 20, 50]"
      layout="total, sizes, prev, pager, next"
      @current-change="handleCurrentChange"
      @size-change="handleSizeChange"
    />
  </div>
</template>

<style scoped>
.page {
  padding: 24px;
}
.page__filter {
  margin-bottom: 8px;
}
.page__selection {
  margin-bottom: 8px;
}
.page__pager {
  margin-top: 16px;
  justify-content: flex-end;
}
</style>
