/**
 * 渠道接入 API（设计文档 9.1 / M4）。
 *
 * 渠道配置属于租户级设置（涉及外部账号与回调地址），后端只允许 owner/admin，
 * 页面对其他角色隐藏入口；前端过滤只是体验，门禁在 require_tenant_admin。
 */

import { api } from './client'
import type {
  ChannelConfigInfo,
  ChannelConfigUpdatePayload,
  ChannelMessageListResponse,
} from './types'

/** 未落库的渠道也会返回占位行，前端能一次看到全部渠道的可用状态 */
export function listChannels(): Promise<ChannelConfigInfo[]> {
  return api.get<ChannelConfigInfo[]>('/v1/channels')
}

/** 只改显式提交的字段：undefined 表示保持原值 */
export function updateChannel(
  channel: string,
  payload: ChannelConfigUpdatePayload,
): Promise<ChannelConfigInfo> {
  return api.put<ChannelConfigInfo>(`/v1/channels/${channel}`, payload)
}

export function listChannelMessages(
  channel: string,
  limit = 20,
): Promise<ChannelMessageListResponse> {
  return api.get<ChannelMessageListResponse>(`/v1/channels/${channel}/messages`, {
    params: { limit },
  })
}
