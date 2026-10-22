/**
 * 坐席工作台 API（设计文档 5.3 / 5.4）。
 *
 * 通知流不用 EventSource：它无法附加 Authorization 头，只能把 token 塞进 URL，
 * 而 URL 会进访问日志与代理日志。这里用 fetch + ReadableStream，
 * 复用 sse.ts 的帧解析；重连由 useAgentDesk 显式控制。
 *
 * 降级：SSE 不可用时同页的工单/会话列表接口都是普通 REST，轮询即可。
 */

import { api, fetchAuthHeaders, handleUnauthorized } from './client'
import { readErrorBody, readSseStream } from './sse'
import type {
  AgentConversationListResponse,
  AgentReplyResult,
  AgentTicketListResponse,
  AppErrorBody,
  HandoffTicketInfo,
  LeaveMessageInfo,
  LeaveMessageListResponse,
  MessageListResponse,
  RealtimeEvent,
} from './types'

export interface TicketQuery {
  /** 逗号分隔：pending,claimed,resolved,cancelled */
  status?: string
  /** 只看我认领的 */
  mine?: boolean
  limit?: number
  cursor?: string | null
}

export function listTickets(query: TicketQuery = {}): Promise<AgentTicketListResponse> {
  return api.get<AgentTicketListResponse>('/v1/agent/tickets', {
    params: { ...query, cursor: query.cursor ?? undefined },
  })
}

export function claimTicket(ticketId: string): Promise<HandoffTicketInfo> {
  return api.post<HandoffTicketInfo>(`/v1/agent/tickets/${ticketId}/claim`)
}

export function resolveTicket(ticketId: string): Promise<HandoffTicketInfo> {
  return api.post<HandoffTicketInfo>(`/v1/agent/tickets/${ticketId}/resolve`)
}

export interface AgentConversationQuery {
  /** 逗号分隔：ai,pending_handoff,human,closed */
  status?: string
  channel?: string
  owner_type?: string
  limit?: number
  cursor?: string | null
}

export function listAgentConversations(
  query: AgentConversationQuery = {},
): Promise<AgentConversationListResponse> {
  return api.get<AgentConversationListResponse>('/v1/agent/conversations', {
    params: { ...query, cursor: query.cursor ?? undefined },
  })
}

export function listAgentMessages(
  conversationId: string,
  limit = 50,
): Promise<MessageListResponse> {
  return api.get<MessageListResponse>(`/v1/agent/conversations/${conversationId}/messages`, {
    params: { limit },
  })
}

/**
 * 坐席回复。后端要求工单已由当前坐席认领，否则 409 HANDOFF_NOT_CLAIMED。
 * delivered=false 表示消息已落库但没送到原渠道（微信窗口关闭、SMTP 故障），
 * 调用方必须把这件事显式告诉坐席。
 */
export function replyToConversation(
  conversationId: string,
  content: string,
): Promise<AgentReplyResult> {
  return api.post<AgentReplyResult>(`/v1/agent/conversations/${conversationId}/messages`, {
    content,
  })
}

export interface LeaveMessageQuery {
  /** new / handled */
  status?: string
  limit?: number
  cursor?: string | null
}

export function listLeaveMessages(
  query: LeaveMessageQuery = {},
): Promise<LeaveMessageListResponse> {
  return api.get<LeaveMessageListResponse>('/v1/agent/leave-messages', {
    params: { ...query, cursor: query.cursor ?? undefined },
  })
}

export function handleLeaveMessage(leaveId: string): Promise<LeaveMessageInfo> {
  return api.post<LeaveMessageInfo>(`/v1/agent/leave-messages/${leaveId}/handle`)
}

export interface AgentNotificationHandlers {
  onReady?: () => void
  onEvent?: (event: RealtimeEvent) => void
  onError?: (error: AppErrorBody) => void
}

/**
 * 订阅坐席通知流。函数在流结束（服务端关闭/网络断开）后返回，
 * 由调用方决定是否重连；被 abort 时抛 AbortError。
 */
export async function streamAgentNotifications(
  handlers: AgentNotificationHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await fetch('/api/v1/agent/notifications/stream', {
    method: 'GET',
    headers: fetchAuthHeaders(),
    signal,
    credentials: 'same-origin',
  })

  if (!resp.ok) {
    // 原生 fetch 不走 axios 拦截器，401 要在这里补一次登录态失效处理
    if (resp.status === 401) handleUnauthorized()
    handlers.onError?.(await readErrorBody(resp))
    return
  }

  await readSseStream(resp, (event, payload) => {
    if (event === 'ready') {
      handlers.onReady?.()
    } else if (event === 'error') {
      handlers.onError?.(payload as AppErrorBody)
    } else {
      handlers.onEvent?.(payload as RealtimeEvent)
    }
  })
}

/** 用户端（ChatView）订阅自己会话的实时事件；结构与坐席端一致，频道不同 */
export interface ConversationEventHandlers {
  onReady?: () => void
  onEvent?: (event: RealtimeEvent) => void
  onError?: (error: AppErrorBody) => void
}

export async function streamConversationEvents(
  tenantId: string | null,
  conversationId: string,
  handlers: ConversationEventHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const headers = fetchAuthHeaders()
  // 访客的租户来自页面参数（localStorage 里没有）
  if (tenantId) headers['X-Tenant-Id'] = tenantId

  const resp = await fetch(`/api/v1/conversations/${conversationId}/events`, {
    method: 'GET',
    headers,
    signal,
    credentials: 'same-origin',
  })

  if (!resp.ok) {
    // 原生 fetch 不走 axios 拦截器，401 要在这里补一次登录态失效处理
    if (resp.status === 401) handleUnauthorized()
    handlers.onError?.(await readErrorBody(resp))
    return
  }

  await readSseStream(resp, (event, payload) => {
    if (event === 'ready') {
      handlers.onReady?.()
    } else if (event === 'error') {
      handlers.onError?.(payload as AppErrorBody)
    } else {
      handlers.onEvent?.(payload as RealtimeEvent)
    }
  })
}
