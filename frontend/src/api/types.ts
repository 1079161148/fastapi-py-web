/**
 * 前后端契约类型。
 *
 * 服务端类型由 FastAPI OpenAPI schema 经 openapi-typescript 生成后
 * 落入本文件（设计文档第 7 章：契约单一来源）；当前为 M0 手写的最小集。
 */

/** 统一错误体，与后端 app/core/exceptions.py 对应 */
export interface AppErrorBody {
  code: string
  message: string
  detail?: Record<string, unknown> | null
}

/** 分页查询参数（cursor 分页前，先用 limit/page 过渡） */
export interface PageQuery {
  page: number
  pageSize: number
  keyword?: string
}

/** 分页结果 */
export interface PageResult<T> {
  list: T[]
  total: number
}

export interface HealthStatus {
  status: string
  app: string
}

/** 与后端 app/schemas/tenant.py 对应 */
export interface TenantInfo {
  id: string
  name: string
  slug: string
  status: string
  quota: Record<string, number>
  /** 租户运营配置（欢迎语/触发词/工作时间等），M8 */
  settings?: TenantSettings
}

/** 与后端 app/schemas/tenant.py TenantSettings 对应 */
export interface TenantSettings {
  welcome_message: string
  handoff_triggers: string[]
  /** leave_message=引导留言，continue_ai=继续由 AI 回答 */
  after_hours_action: 'leave_message' | 'continue_ai'
  work_hours_start: string
  work_hours_end: string
  /** 周一至周日，1=周一 …… 7=周日 */
  work_days: number[]
  fallback_reply: string
  /** 敏感词库；空数组 = 不过滤 */
  sensitive_words: string[]
  /** warn=仅记录 / handoff=转人工 / block=拒绝输入 */
  sensitive_action: 'warn' | 'handoff' | 'block'
  /** 自动抽检比例：0.1 = 每 10 个会话抽 1 个 */
  quality_sample_ratio: number
}

/** 质检（M9）：一条会话一条抽检记录 */
export interface QualityReviewInfo {
  id: string
  conversation_id: string
  conversation_title: string
  conversation_status: string
  channel: string
  conversation_updated_at: string | null
  /** pending / done */
  status: string
  score: number | null
  tags: string[]
  comment: string
  reviewer_id: string | null
  sampled_at: string | null
  reviewed_at: string | null
  created_at: string
}

export interface QualityOverview {
  window_days: number
  reviewed_count: number
  pending_count: number
  avg_score: number | null
  conversations_total: number
  coverage: number
  sensitive_hits: number
  tag_counts: Record<string, number>
}

export interface SensitiveHitInfo {
  id: string
  conversation_id: string
  conversation_title: string
  /** block 时消息未落库：为 null */
  message_id: string | null
  words: string[]
  action: string
  snippet: string
  created_at: string
}

/** 与后端 app/schemas/support.py StatsOverview 对应 */
export interface TrendPoint {
  date: string
  conversations: number
  messages: number
  ratings: number
}

export interface StatsOverview {
  total_conversations: number
  ai_resolved: number
  human_handled: number
  pending_handoff: number
  handoff_rate: number
  avg_first_reply_ms: number | null
  satisfaction_avg: number | null
  satisfaction_count: number
  new_leave_messages: number
  trend: TrendPoint[]
}

/** 与后端 app/schemas/quick_reply.py 对应 */
export interface QuickReplyInfo {
  id: string
  title: string
  content: string
  category: string
  sort_order: number
  created_at: string
}

/** 与后端 app/schemas/support.py CustomerInfo 对应 */
export interface CustomerInfo {
  owner_type: string
  owner_id: string
  display_name: string
  email: string | null
  conversation_count: number
  last_active_at: string | null
  last_conversation_status: string
  total_ratings: number
  avg_rating: number | null
}

export interface CustomerListResponse {
  items: CustomerInfo[]
  total: number
}

/** 与后端 app/schemas/audit_log.py 对应 */
export interface AuditLogInfo {
  id: string
  actor_id: string | null
  actor_name: string
  action: string
  target_type: string
  target_id: string
  detail: Record<string, unknown>
  created_at: string
}

/** 满意度评价请求体 */
export interface RatingRequest {
  score: number
}

/** 与后端 app/schemas/auth.py 对应 */
export interface TokenPair {
  access_token: string
  refresh_token: string
  token_type: string
}

export interface UserInfo {
  id: string
  email: string
  /** 未设置时为 null，表示该账号只能用邮箱登录 */
  username: string | null
  display_name: string
  /** 平台级角色（admin/agent/customer）：能不能登录后台，不决定租户内权限 */
  role: string
  /** 当前租户内的角色（owner/admin/agent/viewer）：导航与页面权限一律看这个 */
  tenant_role: string
  tenant_id: string
}

export interface AuthResponse {
  tokens: TokenPair
  user: UserInfo
}

export interface LoginPayload {
  /** 邮箱或用户名（后端按是否含 @ 分流，两者都能登录） */
  identifier: string
  /** 租户标识：用户名只在本租户内唯一，用用户名登录时必须提供；邮箱登录忽略 */
  tenant?: string
  password: string
}

export interface RegisterPayload {
  email: string
  /** 可选登录名：留空则只能用邮箱登录 */
  username?: string
  password: string
  display_name?: string
  tenant_name: string
}

/** 与后端 app/schemas/chat.py 对应（M2） */
export interface GuestSessionInfo {
  guest_id: string
  expires_at: string
  status: string
  /** 租户欢迎语；访客无令牌，读不到 /v1/tenants/current */
  welcome_message?: string
}

export interface GuestSessionCurrent {
  guest_id: string | null
  expires_at: string | null
  conversation_count: number
}

export type MergeGuestStatus = 'merged' | 'already_merged' | 'no_session'

export interface MergeGuestResult {
  merged_conversations: number
  status: MergeGuestStatus
}

export type ChatRole = 'user' | 'assistant' | 'system'

export interface ConversationInfo {
  id: string
  title: string
  channel: string
  owner_type: 'user' | 'guest'
  status: string
  /** 会话级语音回复开关（M5）：跟着会话走，新会话默认关闭 */
  voice_reply_enabled: boolean
  created_at: string
  updated_at: string
}

export interface ConversationListResponse {
  items: ConversationInfo[]
  next_cursor: string | null
}

/** 回答引用的知识片段（与后端 SourceRef 对应） */
export interface SourceRef {
  chunk_id: string
  document_id: string
  document_name: string
  content: string
  score: number
}

/** SSE sources 事件体 */
export interface SourcesPayload {
  items: SourceRef[]
  /** true = 本该检索但失败（如嵌入模型未配置），本次回答无知识库依据 */
  degraded: boolean
  reason: string
}

/** 消息附件（M5 起：语音）。object_key 不外泄，播放只走 audio_url */
export interface MessageAttachment {
  type: string
  mime_type: string
  duration_seconds: number | null
  size_bytes: number | null
  /** 图片的短时访问地址（带签名令牌）；语音走 MessageInfo.audio_url */
  url?: string | null
}

export interface MessageInfo {
  id: string
  role: ChatRole
  content: string
  message_type: string
  source: string
  tokens: number | null
  latency_ms: number | null
  created_at: string
  sources: SourceRef[]
  attachments: MessageAttachment[]
  /** 短时有效的播放地址；无语音时为 null */
  audio_url: string | null
  /** 满意度评分 1-5；未评价为 null */
  rating: number | null
}

/** SSE `audio` 事件：语音回复已就绪（永远晚于 done） */
export interface AudioPayload {
  message_id: string
  audio_url: string
  mime_type?: string
  duration_seconds?: number | null
}

/** SSE `audio_failed` 事件：语音不可用，文字回答仍然有效 */
export interface AudioFailedPayload {
  message_id: string
  code: string
  message: string
}

/** GET /api/v1/voice/capabilities（M5） */
export interface VoiceCapabilities {
  transcription: boolean
  tts: boolean
  max_seconds: number
  max_mb: number
  missing_transcription: string[]
  missing_tts: string[]
}

/** POST /api/v1/messages/{id}/tts（M5） */
export interface SpeechSynthesisResult {
  message_id: string
  audio_url: string
  mime_type: string
  duration_seconds: number | null
  /** true = 命中缓存，未重新计费 */
  cached: boolean
}

export interface MessageListResponse {
  items: MessageInfo[]
  next_cursor: string | null
}

/** 与后端 app/schemas/knowledge.py 对应（M3） */
export type ChunkStrategy = 'by_heading' | 'fixed_window'

export interface KnowledgeBaseInfo {
  id: string
  name: string
  description: string
  status: string
  chunk_strategy: ChunkStrategy
  document_count: number
  created_at: string
}

export interface KnowledgeBaseCreatePayload {
  name: string
  description?: string
  chunk_strategy?: ChunkStrategy
}

export type DocumentStatus = 'uploaded' | 'processing' | 'done' | 'failed'

export interface DocumentInfo {
  id: string
  kb_id: string
  filename: string
  content_type: string
  size_bytes: number
  status: DocumentStatus
  error_msg: string
  chunk_count: number
  created_at: string
  updated_at: string
}

export interface DocumentListResponse {
  items: DocumentInfo[]
}

export interface SearchHit {
  chunk_id: string
  document_id: string
  kb_id: string
  document_name: string
  content: string
  score: number
  vector_score: number | null
  keyword_score: number | null
  reranked: boolean
}

export interface SearchResponse {
  items: SearchHit[]
  degraded: boolean
  reason: string
}

/** 与后端 app/schemas/usage.py 对应（M7 用量看板） */
export interface UsageBucketInfo {
  day: string
  kind: string
  model: string
  requests: number
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  units: number
  /** 金额按供应商价格配置计算；未配置价格时为 0 */
  cost: number
}

export interface UsageQuotaInfo {
  tokens_limit: number
  tokens_used_today: number
  exceeded: boolean
}

export interface UsageOverview {
  tenant_id: string
  today: UsageBucketInfo
  last_7_days: UsageBucketInfo
  last_30_days: UsageBucketInfo
  quota: UsageQuotaInfo
  daily: UsageBucketInfo[]
}

export interface UsageSummary {
  start: string
  end: string
  group_by: string
  total: UsageBucketInfo
  buckets: UsageBucketInfo[]
}

/** 与后端 app/schemas/agent.py 对应（M6 坐席工作台） */
export interface AgentConversationInfo {
  id: string
  title: string
  channel: string
  owner_type: string
  status: string
  updated_at: string
  /** 进行中的工单；没有则为 null */
  ticket: HandoffTicketInfo | null
}

export interface AgentConversationListResponse {
  items: AgentConversationInfo[]
  next_cursor: string | null
}

/** 与后端 app/schemas/tenant.py 对应（M7 成员管理） */
export type TenantRole = 'owner' | 'admin' | 'agent' | 'customer'

export interface TenantMemberInfo {
  user_id: string
  email: string
  username: string | null
  display_name: string
  role: TenantRole
  status: string
}

export interface TenantMemberCreatePayload {
  email: string
  /** 可选登录名：留空则该成员只能用邮箱登录 */
  username?: string
  password: string
  display_name?: string
  /** 不允许直接创建 owner：owner 变更走角色修改，受"最后一个 owner"保护 */
  role: Exclude<TenantRole, 'owner'>
}

/** 与后端 app/schemas/tenant.py 的 TenantQuotaUpdate 对应（M7 配额编辑） */
export interface TenantQuotaUpdatePayload {
  max_messages_per_day?: number
  max_concurrent?: number
  max_storage_gb?: number
  max_tokens_per_day?: number
}

/** 与后端 app/schemas/handoff.py 对应（M6 转人工） */
export interface HandoffTicketInfo {
  id: string
  conversation_id: string
  agent_id: string | null
  status: string
  reason: string
  requested_by: string
  note: string
  /** 访客最后一句话的摘要：坐席不用点开会话就知道大概 */
  summary: string
  claimed_at: string | null
  resolved_at: string | null
  created_at: string
}

export interface HandoffResponse {
  ticket: HandoffTicketInfo
  /** false 表示该会话已在队列中，本次只是回读 */
  created: boolean
  conversation_status: string
}

export interface HandoffCancelResponse {
  /** false 表示本来就不在队列里（幂等，不是错误） */
  cancelled: boolean
  conversation_status: string
}

/** AI 效果洞察（未命中问题看板，近 30 天） */
export interface UnansweredQuestion {
  question: string
  ask_count: number
  last_asked_at: string
  conversation_id: string
}

export interface InsightOverview {
  window_days: number
  /** AI 生成回答的条数（不含坐席回复与转人工固定文案） */
  total_answers: number
  cited_answers: number
  /** total=0 时为 null：还没有数据比 0% 更不容易误读 */
  hit_rate: number | null
  unanswered_total: number
  unanswered: UnansweredQuestion[]
  knowledge_documents: number
}

export interface AgentTicketListResponse {
  items: HandoffTicketInfo[]
  next_cursor: string | null
}

export interface AgentReplyResult {
  message: MessageInfo
  /** false 表示消息已落库但没送到原渠道（微信窗口关闭、SMTP 故障等） */
  delivered: boolean
  delivery_error: string
}

/** 实时事件信封（后端 app/core/pubsub.py）。type 即 SSE 的事件名 */
export interface RealtimeEvent {
  type: string
  tenant_id: string
  conversation_id: string | null
  ticket_id: string | null
  created_at: string
  data: Record<string, unknown>
}

/** 与后端 app/schemas/channel.py 对应（M4 多渠道接入） */
export interface ChannelConfigInfo {
  channel: string
  enabled: boolean
  external_account_id: string
  credentials: Record<string, unknown>
  rate_limit: number
  /** 环境变量凭证是否齐备：false 时该渠道实际不可用 */
  configured: boolean
  missing: string[]
  webhook_path: string
}

export interface ChannelConfigUpdatePayload {
  enabled?: boolean
  external_account_id?: string
  credentials?: Record<string, unknown>
  rate_limit?: number
  webhook_secret?: string
}

export interface ChannelMessageInfo {
  id: string
  channel: string
  external_msg_id: string
  conversation_id: string | null
  direction: string
  status: string
  external_user_id: string
  content: string
  error_msg: string
  created_at: string
}

export interface ChannelMessageListResponse {
  items: ChannelMessageInfo[]
}

/** 离线留言（M8）：坐席不在线时用户留下的联系方式与问题 */
export interface LeaveMessageInfo {
  id: string
  conversation_id: string | null
  owner_type: string
  name: string
  contact: string
  content: string
  /** new / handled */
  status: string
  handled_by: string | null
  handled_at: string | null
  created_at: string
}

export interface LeaveMessagePayload {
  name: string
  contact: string
  content: string
}

export interface LeaveMessageListResponse {
  items: LeaveMessageInfo[]
  next_cursor: string | null
}
