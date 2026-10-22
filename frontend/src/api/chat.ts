/**
 * 对话 API（设计文档 3.2 / 7）。
 *
 * 普通请求走 axios（自动带 token / 租户头、统一错误提示）；
 * 流式端点用原生 fetch + ReadableStream：EventSource 不支持 POST，
 * 也无法自定义 Authorization 头，这是选用 fetch 而非 EventSource 的硬原因。
 *
 * M5 起音频消息与文本消息共用同一套 SSE 事件（message/sources/delta/done），
 * 因此两条入口共用 `dispatchChatEvent`，差异只在请求体（JSON vs FormData）。
 *
 * 访客场景没有 localStorage 里的租户 ID，租户由页面显式传入
 * （生产环境应由子域名解析，见 ChatView 的说明）。
 */

import { api, fetchAuthHeaders, handleUnauthorized, tokenStorage } from './client'
import { readErrorBody, readSseStream } from './sse'
import type {
  AppErrorBody,
  AudioFailedPayload,
  AudioPayload,
  ChatRole,
  ConversationInfo,
  ConversationListResponse,
  GuestSessionCurrent,
  GuestSessionInfo,
  HandoffCancelResponse,
  HandoffResponse,
  LeaveMessageInfo,
  LeaveMessagePayload,
  MergeGuestResult,
  MessageAttachment,
  MessageInfo,
  MessageListResponse,
  SourcesPayload,
} from './types'

/** 流式事件里携带的消息（与后端 chat service 的 SSE data 对应） */
export interface ServerMessage {
  id: string
  role: ChatRole
  content: string
  created_at: string
  message_type?: string
  attachments?: MessageAttachment[]
  audio_url?: string | null
  tokens?: number | null
  latency_ms?: number | null
}

export interface ChatStreamHandlers {
  /** 语音消息：进入转写阶段（服务端已收到音频） */
  onTranscribing?: () => void
  /** 用户消息已落库，回传服务端 ID（语音消息的 content 是转写文本） */
  onUserMessage?: (message: ServerMessage) => void
  /** 检索到的引用（先于正文到达；degraded 表示本次没有知识库依据） */
  onSources?: (payload: SourcesPayload) => void
  /** 文本增量 */
  onDelta?: (delta: string) => void
  /** 回答完成（content 为服务端最终全文） */
  onDone?: (message: ServerMessage) => void
  /** 语音回复就绪（晚于 done；会话开启语音回复时才有） */
  onAudio?: (payload: AudioPayload) => void
  /** 语音合成不可用（文字回答仍然有效，只降级掉音频） */
  onAudioFailed?: (payload: AudioFailedPayload) => void
  /** 流中途失败（已发出 200，只能靠事件通知） */
  onError?: (error: AppErrorBody) => void
}

function tenantConfig(tenantId: string | null) {
  return tenantId ? { headers: { 'X-Tenant-Id': tenantId } } : undefined
}

export function createOrResumeGuestSession(tenantId: string | null): Promise<GuestSessionInfo> {
  return api.post<GuestSessionInfo>('/v1/guest/sessions', undefined, tenantConfig(tenantId))
}

export function fetchCurrentGuestSession(tenantId: string | null): Promise<GuestSessionCurrent> {
  return api.get<GuestSessionCurrent>('/v1/guest/sessions/current', tenantConfig(tenantId))
}

export function mergeGuest(tenantId: string | null): Promise<MergeGuestResult> {
  return api.post<MergeGuestResult>('/v1/auth/merge-guest', undefined, tenantConfig(tenantId))
}

export function listConversations(
  tenantId: string | null,
  limit = 20,
): Promise<ConversationListResponse> {
  return api.get<ConversationListResponse>('/v1/conversations', {
    ...tenantConfig(tenantId),
    params: { limit },
  })
}

export function createConversation(
  tenantId: string | null,
  title = '',
): Promise<ConversationInfo> {
  return api.post<ConversationInfo>('/v1/conversations', { title }, tenantConfig(tenantId))
}

export function updateConversation(
  tenantId: string | null,
  conversationId: string,
  payload: { title?: string; status?: string; voice_reply_enabled?: boolean },
): Promise<ConversationInfo> {
  return api.patch<ConversationInfo>(
    `/v1/conversations/${conversationId}`,
    payload,
    tenantConfig(tenantId),
  )
}

/**
 * 转人工（设计文档 5.3）：重复请求返回已有工单（created=false），不会重复建单。
 * 会话状态置 pending_handoff，但 AI 继续回答——坐席认领后才锁住 AI。
 */
export function requestHandoff(
  tenantId: string | null,
  conversationId: string,
  note = '',
): Promise<HandoffResponse> {
  return api.post<HandoffResponse>(
    `/v1/conversations/${conversationId}/handoff`,
    { note },
    tenantConfig(tenantId),
  )
}

/**
 * 上传图片（M8）：走 multipart，api 门面固定发 JSON 用不了。
 *
 * 不手动设 Content-Type：浏览器要自己补 multipart 的 boundary。
 * 失败时抛 Error 而不是走 axios 拦截器——这条是原生 fetch。
 */
export async function uploadImage(
  tenantId: string | null,
  conversationId: string,
  file: File,
): Promise<MessageInfo> {
  const form = new FormData()
  form.append('file', file)
  const headers = fetchAuthHeaders()
  if (tenantId) headers['X-Tenant-Id'] = tenantId

  const resp = await fetch(`/api/v1/conversations/${conversationId}/messages/image`, {
    method: 'POST',
    headers,
    body: form,
    credentials: 'same-origin',
  })
  if (!resp.ok) {
    // 原生 fetch 不走 axios 拦截器，401 要在这里补一次登录态失效处理
    if (resp.status === 401) handleUnauthorized()
    const body = await readErrorBody(resp)
    throw new Error(body.message || '图片上传失败，请重试')
  }
  return (await resp.json()) as MessageInfo
}

/** 离线留言（M8）：坐席不在线或用户不想排队时的兜底 */
export function leaveMessage(
  tenantId: string | null,
  conversationId: string,
  payload: LeaveMessagePayload,
): Promise<LeaveMessageInfo> {
  return api.post<LeaveMessageInfo>(
    `/v1/conversations/${conversationId}/leave-message`,
    payload,
    tenantConfig(tenantId),
  )
}

/**
 * 取消排队：工单 pending → cancelled，会话回到 ai。
 * 幂等（本来没排队时返回 cancelled=false）；坐席已认领时后端返回 409。
 */
export function cancelHandoff(
  tenantId: string | null,
  conversationId: string,
): Promise<HandoffCancelResponse> {
  return api.post<HandoffCancelResponse>(
    `/v1/conversations/${conversationId}/handoff/cancel`,
    {},
    tenantConfig(tenantId),
  )
}

export function listMessages(
  tenantId: string | null,
  conversationId: string,
  limit = 50,
): Promise<MessageListResponse> {
  return api.get<MessageListResponse>(`/v1/conversations/${conversationId}/messages`, {
    ...tenantConfig(tenantId),
    params: { limit },
  })
}

/**
 * 认证头（不含 Content-Type）。
 *
 * 语音上传是 multipart，Content-Type 必须由浏览器带着 boundary 自动生成；
 * 手动设成 application/json 会让后端直接 400。因此这里不预设，
 * 由各入口按需补。
 */
function authHeaders(tenantId: string | null): Record<string, string> {
  const headers: Record<string, string> = {}
  const token = tokenStorage.get()
  if (token) headers.Authorization = `Bearer ${token}`
  if (tenantId) headers['X-Tenant-Id'] = tenantId
  return headers
}

function dispatchChatEvent(
  event: string,
  payload: unknown,
  handlers: ChatStreamHandlers,
): void {
  if (event === 'transcribing') {
    handlers.onTranscribing?.()
  } else if (event === 'message') {
    handlers.onUserMessage?.(payload as ServerMessage)
  } else if (event === 'sources') {
    handlers.onSources?.(payload as SourcesPayload)
  } else if (event === 'delta') {
    const delta = (payload as { content?: string }).content ?? ''
    if (delta) handlers.onDelta?.(delta)
  } else if (event === 'done') {
    handlers.onDone?.(payload as ServerMessage)
  } else if (event === 'audio') {
    handlers.onAudio?.(payload as AudioPayload)
  } else if (event === 'audio_failed') {
    handlers.onAudioFailed?.(payload as AudioFailedPayload)
  } else if (event === 'error') {
    handlers.onError?.(payload as AppErrorBody)
  }
}

/** 发起一轮流式对话。正常结束返回；被 abort 时抛 AbortError，由调用方忽略。 */
export async function streamChat(
  tenantId: string | null,
  conversationId: string,
  content: string,
  handlers: ChatStreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await fetch(`/api/v1/conversations/${conversationId}/messages/stream`, {
    method: 'POST',
    headers: { ...authHeaders(tenantId), 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
    signal,
    // 访客凭证是 HttpOnly cookie，必须显式允许同源携带
    credentials: 'same-origin',
  })

  if (!resp.ok) {
    // 原生 fetch 不走 axios 拦截器，401 要在这里补一次登录态失效处理
    if (resp.status === 401) handleUnauthorized()
    handlers.onError?.(await readErrorBody(resp))
    return
  }
  await readSseStream(resp, (event, payload) => dispatchChatEvent(event, payload, handlers))
}

/**
 * 发送语音消息（M5）：上传音频 → 服务端转写 → 同一套生成链路。
 *
 * duration 由 MediaRecorder 计时上报：ogg/webm 的时长需要解析容器索引，
 * 服务端只对 wav 做精确校验，其余格式以大小与转写超时兜底。
 */
export async function streamAudioMessage(
  tenantId: string | null,
  conversationId: string,
  audio: Blob,
  durationSeconds: number,
  handlers: ChatStreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const form = new FormData()
  form.append('file', audio, fileNameFor(audio.type))
  form.append('duration_seconds', String(durationSeconds))

  const resp = await fetch(`/api/v1/conversations/${conversationId}/messages/audio`, {
    method: 'POST',
    headers: authHeaders(tenantId),
    body: form,
    signal,
    credentials: 'same-origin',
  })

  if (!resp.ok) {
    // 原生 fetch 不走 axios 拦截器，401 要在这里补一次登录态失效处理
    if (resp.status === 401) handleUnauthorized()
    handlers.onError?.(await readErrorBody(resp))
    return
  }
  await readSseStream(resp, (event, payload) => dispatchChatEvent(event, payload, handlers))
}

/** 文件名只用于 multipart 的 name 字段，服务端一律按魔数判定格式与 MIME。 */
function fileNameFor(mimeType: string): string {
  if (mimeType.includes('ogg')) return 'voice.ogg'
  if (mimeType.includes('mp4')) return 'voice.m4a'
  if (mimeType.includes('wav')) return 'voice.wav'
  return 'voice.webm'
}
