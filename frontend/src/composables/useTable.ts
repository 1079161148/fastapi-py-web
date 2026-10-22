/**
 * useTable —— 表格场景的自定义 hooks 二次封装（设计文档 4.1 composables）。
 *
 * 只封装「通用且高频」的能力：分页、查询、loading、多选。
 * 一次封装解决以下边界问题，调用方零处理：
 *
 * 1. 竞态覆盖：快速翻页/连续搜索时，旧请求的响应晚于新请求返回会覆盖新数据
 *    → 请求序号守卫，只接受最新一次响应
 * 2. 卸载后写状态：请求返回时组件已销毁导致 Vue 警告与内存泄漏
 *    → alive 守卫，卸载后直接丢弃响应
 * 3. 搜索后页码越界：新查询结果总页数小于当前页
 *    → search/reset 重置到第 1 页；handleSizeChange 同样重置
 * 4. 数据变更后页码越界：删除当前页最后一条后 total 变小
 *    → 收到响应后钳制页码并自动重新请求
 * 5. 失败态：请求失败时 loading 卡死或列表残留旧数据
 *    → finally 复位 loading，catch 清空列表并向上抛出
 * 6. 隐式触发：query 对象被模板直接修改导致意外请求
 *    → 查询是显式动作（search/reset），数据获取只由本 hook 发起
 *
 * 用法约束：
 * - fetcher 必须是稳定引用（在 setup 内定义，不要写成模板内联箭头）
 * - queryState 是响应式对象，直接改字段后调用 search()
 */

import { onUnmounted, reactive, ref } from 'vue'
import type { Ref } from 'vue'

import type { PageResult } from '@/api/types'

/** 多选行的 key 提取方式：字段名或函数 */
export type RowKeyResolver<T> = keyof T | ((row: T) => string | number)

export interface UseTableOptions<
  T,
  Q extends Record<string, unknown> = Record<string, unknown>,
> {
  /** 数据获取函数；入参 = queryState + page/pageSize */
  fetcher: (params: Q & { page: number; pageSize: number }) => Promise<PageResult<T>>
  /** 初始查询条件，默认空对象 */
  query?: Q
  /** 是否挂载即请求，默认 true；SSR/对话框懒加载场景可设 false */
  immediate?: boolean
  /** 每页条数，默认 10 */
  pageSize?: number
  /** 多选 row-key，缺省不做 key 提取（仍返回选中行对象） */
  rowKey?: RowKeyResolver<T>
  /** 请求失败回调；注意 axios 层已有统一 ElMessage 提示，这里只做局部补充 */
  onError?: (error: unknown) => void
}

export interface UseTableReturn<
  T,
  Q extends Record<string, unknown> = Record<string, unknown>,
> {
  list: Ref<T[]>
  loading: Ref<boolean>
  total: Ref<number>
  page: Ref<number>
  pageSize: Ref<number>
  /** 查询条件：直接改字段，然后调用 search() */
  queryState: Q
  selectedRows: Ref<T[]>
  /** 选中行的 key 列表（批量操作用）；未配置 rowKey 时退化为 JSON 序列化值 */
  selectedKeys: Ref<Array<string | number>>
  /** 显式查询：重置到第 1 页 */
  search: () => Promise<void>
  /** 恢复初始查询条件并回到第 1 页 */
  reset: () => Promise<void>
  /** 刷新当前页（保持页码与查询条件） */
  refresh: () => Promise<void>
  /** 绑定到 el-table 的 @size-change */
  handleSizeChange: (size: number) => Promise<void>
  /** 绑定到 el-table 的 @current-change */
  handleCurrentChange: (current: number) => Promise<void>
  /** 绑定到 el-table 的 @selection-change */
  handleSelectionChange: (rows: T[]) => void
  /** 清空多选（业务操作后调用） */
  clearSelection: () => void
}

export function useTable<
  T,
  Q extends Record<string, unknown> = Record<string, unknown>,
>(options: UseTableOptions<T, Q>): UseTableReturn<T, Q> {
  const {
    immediate = true,
    pageSize: defaultPageSize = 10,
    rowKey,
    onError,
  } = options

  const list = ref<T[]>([]) as Ref<T[]>
  const loading = ref(false)
  const total = ref(0)
  const page = ref(1)
  const pageSize = ref(defaultPageSize)
  const queryState = reactive({ ...(options.query ?? {}) }) as Q
  const selectedRows = ref<T[]>([]) as Ref<T[]>
  const selectedKeys = ref<Array<string | number>>([])

  /** 请求序号守卫：仅最新序号可写入状态 */
  let requestSeq = 0
  /** 卸载守卫：组件销毁后不再写入状态 */
  let alive = true
  onUnmounted(() => {
    alive = false
  })

  function extractKey(row: T): string | number {
    if (!rowKey) return JSON.stringify(row)
    return typeof rowKey === 'function' ? rowKey(row) : (row[rowKey] as string | number)
  }

  async function fetchData(): Promise<void> {
    const seq = ++requestSeq
    loading.value = true
    try {
      const result = await options.fetcher({ ...queryState, page: page.value, pageSize: pageSize.value })
      // 过期响应或已卸载：直接丢弃
      if (seq !== requestSeq || !alive) return

      list.value = result.list
      total.value = result.total

      // 数据变更后页码越界（如删完当前页最后一条）：钳制并重新请求
      const maxPage = Math.max(1, Math.ceil(result.total / pageSize.value))
      if (page.value > maxPage) {
        page.value = maxPage
        return fetchData()
      }
    } catch (error) {
      if (seq !== requestSeq || !alive) return
      // 失败即清空，避免展示与总数自相矛盾的旧数据
      list.value = []
      total.value = 0
      onError?.(error)
    } finally {
      // 仅最新一次请求负责收尾 loading
      if (seq === requestSeq && alive) {
        loading.value = false
      }
    }
  }

  async function search(): Promise<void> {
    page.value = 1
    await fetchData()
  }

  async function reset(): Promise<void> {
    Object.assign(queryState, options.query ?? {})
    page.value = 1
    await fetchData()
  }

  async function refresh(): Promise<void> {
    await fetchData()
  }

  async function handleSizeChange(size: number): Promise<void> {
    pageSize.value = size
    page.value = 1
    await fetchData()
  }

  async function handleCurrentChange(current: number): Promise<void> {
    page.value = current
    await fetchData()
  }

  function handleSelectionChange(rows: T[]): void {
    selectedRows.value = rows
    selectedKeys.value = rows.map(extractKey)
  }

  function clearSelection(): void {
    selectedRows.value = []
    selectedKeys.value = []
  }

  if (immediate) {
    void fetchData()
  }

  return {
    list,
    loading,
    total,
    page,
    pageSize,
    queryState,
    selectedRows,
    selectedKeys,
    search,
    reset,
    refresh,
    handleSizeChange,
    handleCurrentChange,
    handleSelectionChange,
    clearSelection,
  }
}
