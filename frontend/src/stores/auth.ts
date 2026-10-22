/**
 * 认证状态（设计文档 M1）。
 *
 * 令牌存 localStorage（tokenStorage 单一出口，api 客户端从这里读取），
 * 用户信息不持久化：刷新页面后经 restore() 调 /auth/me 重建，
 * 避免本地缓存的组织/角色信息过期后与服务端不一致。
 */

import { defineStore } from 'pinia'

import { api, tokenStorage } from '@/api/client'
import type {
  AuthResponse,
  LoginPayload,
  RegisterPayload,
  TenantInfo,
  UserInfo,
} from '@/api/types'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    user: null as UserInfo | null,
    /**
     * 当前租户详情：外壳（顶栏）与工作台都要用。
     * 放在 store 里而不是各自请求，避免同一个 GET 在每次进页面时发两遍。
     */
    tenant: null as TenantInfo | null,
    /** 是否已完成首次登录态探测（决定守卫是否放行，避免刷新时误跳登录页） */
    restored: false,
  }),
  getters: {
    isLoggedIn: (state) => state.user !== null,
    tenantId: (state) => state.user?.tenant_id ?? null,
  },
  actions: {
    _applyAuth(data: AuthResponse) {
      tokenStorage.set(data.tokens.access_token)
      tokenStorage.setRefresh(data.tokens.refresh_token)
      tokenStorage.setTenant(data.user.tenant_id)
      this.user = data.user
    },

    async login(payload: LoginPayload) {
      const data = await api.post<AuthResponse>('/v1/auth/login', payload)
      this._applyAuth(data)
    },

    async register(payload: RegisterPayload) {
      const data = await api.post<AuthResponse>('/v1/auth/register', payload)
      this._applyAuth(data)
    },

    /** 拉取当前租户详情（外壳与工作台共用）；失败时保持 null，页面按缺失降级 */
    async loadTenant() {
      try {
        this.tenant = await api.get<TenantInfo>('/v1/tenants/current')
      } catch {
        this.tenant = null
      }
    },

    /** 用本地令牌恢复会话：令牌无效时静默失败，交由路由守卫跳登录页 */
    async restore() {
      if (!tokenStorage.get()) {
        this.restored = true
        return
      }
      try {
        this.user = await api.get<UserInfo>('/v1/auth/me')
        tokenStorage.setTenant(this.user.tenant_id)
      } catch {
        tokenStorage.clear()
        tokenStorage.clearTenant()
        this.user = null
      } finally {
        this.restored = true
      }
    },

    /**
     * 本地登出（不调接口）。
     * 401 由 api 层统一触发，必须清掉内存里的 user，否则 isLoggedIn 仍为 true、
     * 路由守卫会放行，页面停在原地等用户手动刷新才跳登录页。
     */
    reset() {
      tokenStorage.clear()
      tokenStorage.clearTenant()
      this.user = null
      this.tenant = null
      // 已判定过登录态，守卫不必再等 restore()
      this.restored = true
    },

    async logout() {
      const refresh = tokenStorage.getRefresh()
      try {
        if (refresh) {
          await api.post('/v1/auth/logout', { refresh_token: refresh })
        }
      } finally {
        // 无论接口是否成功，本地状态一律清理
        tokenStorage.clear()
        tokenStorage.clearTenant()
        this.user = null
        this.tenant = null
      }
    },
  },
})
