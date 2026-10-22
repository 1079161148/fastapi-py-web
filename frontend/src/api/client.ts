/**
 * axios 客户端（设计文档 3.2：普通请求用 axios，SSE 流式用 fetch）。
 *
 * 收敛的边界问题：
 * - 响应体统一解包为业务数据（api.get/post 返回 T 而非 AxiosResponse<T>），
 *   类型与运行时一致，调用方不需要任何断言
 * - 错误统一转换为 ApiError（携带可分支的 code）后抛出
 * - token / 租户标识从存储注入 header，业务代码不感知
 * - 401 由 handleUnauthorized 统一处理：清 token 并交回应用层重定向（M2 接入 refresh token 轮换）
 *
 * 注意：不要在拦截器里解包 response.data——那样返回类型与运行时不符，
 * 调用方被迫写 as unknown。解包只发生在 api.* helper 内。
 */

import axios, { AxiosError, type AxiosRequestConfig } from 'axios'
import { ElMessage } from 'element-plus'

import type { AppErrorBody } from './types'

const TOKEN_KEY = 'access_token'
const REFRESH_KEY = 'refresh_token'
const TENANT_KEY = 'tenant_id'

/**
 * 本轮会话是否已因 401 处理过失效。
 * 并发请求会同时 401，只允许跳一次登录页、弹一条提示。
 * 拿到新 token（登录成功）即视为新会话，在 tokenStorage.set 里复位。
 */
let sessionExpired = false

let unauthorizedHandler: (() => void) | null = null

/** 由应用层（main.ts）注册：重置登录态并跳转登录页。client 不直接依赖 router / pinia */
export function setUnauthorizedHandler(handler: () => void): void {
  unauthorizedHandler = handler
}

/**
 * 处理 401，返回 true 表示"登录态失效，已交给应用层"。
 *
 * - 没有 token（访客 cookie 失效、登录接口密码错）返回 false：访客没有账号可登，
 *   调用方照常提示错误，不能把匿名访客弹到登录页
 * - 同一轮失效只通知一次，避免并发 401 重复跳转与提示刷屏
 */
export function handleUnauthorized(): boolean {
  if (sessionExpired) return true
  if (!tokenStorage.get()) return false
  sessionExpired = true
  tokenStorage.clear()
  unauthorizedHandler?.()
  return true
}

/**
 * 认证入口自身的 401 表示凭证不对（密码错、refresh 失效），
 * 不是"当前会话过期"，不应触发跳登录与提示替换。
 */
const AUTH_ENTRY_PATHS = ['/v1/auth/login', '/v1/auth/register', '/v1/auth/refresh', '/v1/auth/logout']

function isAuthEntry(url: string | undefined): boolean {
  if (!url) return false
  return AUTH_ENTRY_PATHS.some((path) => url.startsWith(path))
}

export const tokenStorage = {
  get: () => localStorage.getItem(TOKEN_KEY),
  set: (token: string) => {
    sessionExpired = false
    localStorage.setItem(TOKEN_KEY, token)
  },
  setRefresh: (token: string) => localStorage.setItem(REFRESH_KEY, token),
  getRefresh: () => localStorage.getItem(REFRESH_KEY),
  clear: () => {
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(REFRESH_KEY)
  },
  getTenant: () => localStorage.getItem(TENANT_KEY),
  setTenant: (tenantId: string) => localStorage.setItem(TENANT_KEY, tenantId),
  clearTenant: () => localStorage.removeItem(TENANT_KEY),
}

export class ApiError extends Error {
  constructor(
    readonly body: AppErrorBody,
    readonly status: number,
  ) {
    super(body.message)
    this.name = 'ApiError'
  }
}

const client = axios.create({
  baseURL: '/api',
  timeout: 60_000,
  headers: { 'Content-Type': 'application/json' },
})

client.interceptors.request.use((config) => {
  const token = tokenStorage.get()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  const tenant = tokenStorage.getTenant()
  if (tenant) {
    config.headers['X-Tenant-Id'] = tenant
  }
  return config
})

client.interceptors.response.use(
  (response) => response,
  (error: AxiosError<AppErrorBody>) => {
    const status = error.response?.status ?? 0
    const body: AppErrorBody =
      error.response?.data?.code !== undefined
        ? error.response.data
        : {
            // 网络错误 / 超时 / 非 JSON 响应：统一为可识别的错误码
            code: status === 0 ? 'NETWORK_ERROR' : 'UNKNOWN_ERROR',
            message:
              status === 0 ? '网络连接失败，请检查网络后重试' : `请求失败（HTTP ${status}），请稍后重试`,
          }

    // 登录态失效：token 已清、页面会跳到登录页，这里不再重复弹后端原文。
    // 登录/注册入口的 401 是"账号密码不对"，不能当会话失效处理（否则错误提示被吞掉）
    if (status === 401 && !isAuthEntry(error.config?.url) && handleUnauthorized()) {
      return Promise.reject(new ApiError(body, status))
    }
    // 错误同时提示与抛出：UI 有反馈，调用方仍可按 code 分支处理
    ElMessage.error(body.message)
    return Promise.reject(new ApiError(body, status))
  },
)

/**
 * 原生 fetch 的鉴权头（axios 拦截器管不到 fetch）。
 *
 * 坐在 localStorage 的 token / 租户在这里读，供流式端点使用；
 * 访客场景另有显式租户参数，走 chat.ts 的 authHeaders。
 */
export function fetchAuthHeaders(): Record<string, string> {
  const headers: Record<string, string> = {}
  const token = tokenStorage.get()
  if (token) headers.Authorization = `Bearer ${token}`
  const tenant = tokenStorage.getTenant()
  if (tenant) headers['X-Tenant-Id'] = tenant
  return headers
}

/** 类型安全的请求门面：统一解包 data，业务代码只面对 T */
export const api = {
  get: <T>(url: string, config?: AxiosRequestConfig) =>
    client.get<T>(url, config).then((resp) => resp.data),
  post: <T>(url: string, data?: unknown, config?: AxiosRequestConfig) =>
    client.post<T>(url, data, config).then((resp) => resp.data),
  put: <T>(url: string, data?: unknown, config?: AxiosRequestConfig) =>
    client.put<T>(url, data, config).then((resp) => resp.data),
  patch: <T>(url: string, data?: unknown, config?: AxiosRequestConfig) =>
    client.patch<T>(url, data, config).then((resp) => resp.data),
  delete: <T>(url: string, config?: AxiosRequestConfig) =>
    client.delete<T>(url, config).then((resp) => resp.data),
}

export default client
