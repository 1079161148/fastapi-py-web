/**
 * M8 运营支撑 API：数据报表、快捷回复、客户管理、审计日志、满意度。
 *
 * 与坐席/知识库 API 分开：这些是"运营视角"的聚合数据，
 * 调用方是管理后台与工作台，不是对话链路。
 */

import { api } from './client'
import type {
  AuditLogInfo,
  CustomerListResponse,
  QuickReplyInfo,
  RatingRequest,
  StatsOverview,
  TenantSettings,
} from './types'

/** 工作台 KPI 与报表页共用：两处数字必须同源 */
export function getStatsOverview(): Promise<StatsOverview> {
  return api.get<StatsOverview>('/v1/stats/overview')
}

// ---- 快捷回复 ----
export function listQuickReplies(category?: string): Promise<QuickReplyInfo[]> {
  return api.get<QuickReplyInfo[]>('/v1/quick-replies', {
    params: { category: category || undefined },
  })
}

export function createQuickReply(payload: {
  title: string
  content: string
  category: string
  sort_order?: number
}): Promise<QuickReplyInfo> {
  return api.post<QuickReplyInfo>('/v1/quick-replies', payload)
}

export function updateQuickReply(
  replyId: string,
  payload: Partial<{ title: string; content: string; category: string; sort_order: number }>,
): Promise<QuickReplyInfo> {
  return api.patch<QuickReplyInfo>(`/v1/quick-replies/${replyId}`, payload)
}

export function deleteQuickReply(replyId: string): Promise<void> {
  return api.delete<void>(`/v1/quick-replies/${replyId}`)
}

// ---- 客户管理 ----
export function listCustomers(
  limit = 50,
): Promise<CustomerListResponse> {
  return api.get<CustomerListResponse>('/v1/customers', { params: { limit } })
}

// ---- 审计日志 ----
export function listAuditLogs(action?: string): Promise<AuditLogInfo[]> {
  return api.get<AuditLogInfo[]>('/v1/audit-logs', {
    params: { action: action || undefined },
  })
}

// ---- 满意度评价 ----
export function rateMessage(
  conversationId: string,
  messageId: string,
  score: number,
): Promise<{ message_id: string; score: number }> {
  return api.post(`/v1/conversations/${conversationId}/messages/${messageId}/rating`, {
    score,
  } satisfies RatingRequest)
}

// ---- 租户运营配置（设置页保存）----
export function updateTenantSettings(settings: TenantSettings): Promise<void> {
  return api.patch<void>('/v1/tenants/current', { settings })
}
