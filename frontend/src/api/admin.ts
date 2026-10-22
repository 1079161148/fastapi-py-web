/**
 * 管理后台 API（M7：用量看板 + 成员管理）。
 *
 * 这些接口都要求租户 owner/admin：成本与成员属于租户经营信息，
 * 后端在执行层（require_tenant_admin）校验，前端不做权限判断的替代品。
 */

import { api } from './client'
import type {
  TenantInfo,
  TenantMemberCreatePayload,
  TenantMemberInfo,
  TenantQuotaUpdatePayload,
  TenantRole,
  UsageOverview,
  UsageSummary,
} from './types'

export function getUsageOverview(): Promise<UsageOverview> {
  return api.get<UsageOverview>('/v1/usage/overview')
}

export function getUsageSummary(params: {
  groupBy?: 'day' | 'kind' | 'model'
  start?: string
  end?: string
}): Promise<UsageSummary> {
  return api.get<UsageSummary>('/v1/usage/summary', {
    params: { group_by: params.groupBy, start: params.start, end: params.end },
  })
}

export function listMembers(): Promise<TenantMemberInfo[]> {
  return api.get<TenantMemberInfo[]>('/v1/tenants/current/members')
}

export function addMember(payload: TenantMemberCreatePayload): Promise<TenantMemberInfo> {
  return api.post<TenantMemberInfo>('/v1/tenants/current/members', payload)
}

export function updateMemberRole(userId: string, role: TenantRole): Promise<TenantMemberInfo> {
  return api.patch<TenantMemberInfo>(`/v1/tenants/current/members/${userId}`, { role })
}

export function removeMember(userId: string): Promise<void> {
  return api.delete<void>(`/v1/tenants/current/members/${userId}`)
}

export function renameTenant(name: string): Promise<TenantInfo> {
  return api.patch<TenantInfo>('/v1/tenants/current', { name })
}

/**
 * 配额局部更新：只提交要改的键，未提交的保持不变（后端 TenantQuotaUpdate）。
 * 显式传 0 表示"不限/关闭限制"，与"不修改"是两件事，所以不要过滤 0。
 */
export function updateQuota(payload: TenantQuotaUpdatePayload): Promise<TenantInfo> {
  return api.patch<TenantInfo>('/v1/tenants/current/quota', payload)
}
