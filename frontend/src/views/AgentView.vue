<script setup lang="ts">
/**
 * 人工坐席工作台（M6，设计文档 5.3 / 5.4）。
 *
 * 左队列 / 右会话：接管 → 回复 → 结束，期间靠 SSE 收新工单与访客消息。
 *
 * 三条约束（与后端一一对应，前端只是如实反映）：
 * 1. 只有工单的认领者能回复（后端 409 HANDOFF_NOT_CLAIMED），未接管时输入框禁用
 * 2. 回复可能"落库成功但没送到渠道"（微信窗口关闭等），必须显式提示坐席
 * 3. 结束工单会把会话交还 AI，不是关闭会话——用户接着问不必重开窗口
 *
 * 通知流用 fetch + ReadableStream 而非 EventSource：后者无法带 Authorization。
 * 断线按指数退避重连；SSE 不可用时列表接口本身就是普通 REST，可退化为轮询。
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'

import {
  claimTicket,
  listAgentConversations,
  listAgentMessages,
  replyToConversation,
  resolveTicket,
  streamAgentNotifications,
} from '@/api/agent'
import { listQuickReplies } from '@/api/support'
import type { AgentConversationInfo, MessageInfo, QuickReplyInfo, RealtimeEvent } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { renderMarkdown } from '@/utils/markdown'

type Tab = 'pending' | 'mine' | 'all'

const auth = useAuthStore()

const tab = ref<Tab>('pending')
const conversations = ref<AgentConversationInfo[]>([])
const activeId = ref<string | null>(null)
const messages = ref<MessageInfo[]>([])
const draft = ref('')
/** 快捷回复（M8）：插入到草稿末尾而不是覆盖，坐席可以接着改 */
const quickReplies = ref<QuickReplyInfo[]>([])
const loadingList = ref(false)
const loadingMessages = ref(false)
const sending = ref(false)
const connected = ref(false)
const listRef = ref<HTMLElement | null>(null)

const TABS: Array<{ key: Tab; label: string }> = [
  { key: 'pending', label: '待接管' },
  { key: 'mine', label: '我处理中' },
  { key: 'all', label: '全部' },
]

const CHANNEL_LABELS: Record<string, string> = {
  web: '网页',
  email: '邮件',
  wechat: '微信',
  dingtalk: '钉钉',
}

const STATUS_LABELS: Record<string, string> = {
  pending_handoff: '等待接管',
  human: '人工中',
  ai: 'AI 服务中',
  closed: '已结束',
}

const activeConversation = computed(
  () => conversations.value.find((item) => item.id === activeId.value) ?? null,
)

/** 当前会话的进行中工单（没有则是会话已交还 AI 或还没建单） */
const activeTicket = computed(() => activeConversation.value?.ticket ?? null)

/** 只有认领者能回复：与后端 _require_claimed_ticket 保持一致 */
const isMine = computed(
  () => Boolean(activeTicket.value) && activeTicket.value?.agent_id === auth.user?.id,
)

const canReply = computed(() => isMine.value && !sending.value)

async function loadConversations(): Promise<void> {
  loadingList.value = true
  try {
    const status = tab.value === 'pending' ? 'pending_handoff' : 'pending_handoff,human'
    const data = await listAgentConversations({ status, limit: 50 })
    const items =
      tab.value === 'mine'
        ? data.items.filter(
            (item) => item.status === 'human' && item.ticket?.agent_id === auth.user?.id,
          )
        : data.items
    conversations.value = items
    // 选中的会话可能已被别人接管或已结束，从列表消失就清空右侧
    if (activeId.value && !items.some((item) => item.id === activeId.value)) {
      activeId.value = null
      messages.value = []
    }
  } catch {
    // 拦截器已提示
  } finally {
    loadingList.value = false
  }
}

async function loadMessages(): Promise<void> {
  if (!activeId.value) return
  loadingMessages.value = true
  try {
    const data = await listAgentMessages(activeId.value, 100)
    messages.value = data.items
    await scrollToBottom()
  } catch {
    // 同上
  } finally {
    loadingMessages.value = false
  }
}

async function changeTab(next: Tab): Promise<void> {
  if (tab.value === next) return
  tab.value = next
  await loadConversations()
}

async function selectConversation(item: AgentConversationInfo): Promise<void> {
  if (activeId.value === item.id) return
  activeId.value = item.id
  await loadMessages()
}

async function scrollToBottom(): Promise<void> {
  await nextTick()
  const el = listRef.value
  if (el) el.scrollTop = el.scrollHeight
}

async function onClaim(): Promise<void> {
  const ticket = activeTicket.value
  if (!ticket) return
  try {
    await claimTicket(ticket.id)
    ElMessage.success('已接管，现在可以直接回复')
    // 接管后会话状态变成 human，待接管列表里就没有它了；
    // 不切 tab 的话右侧会被清空，坐席刚接的单当场消失
    tab.value = 'mine'
    await loadConversations()
    await loadMessages()
  } catch {
    // 409 已被其他坐席抢走：拦截器会提示，这里刷新回真实状态
    await loadConversations()
  }
}

async function onResolve(): Promise<void> {
  const ticket = activeTicket.value
  if (!ticket) return
  try {
    await ElMessageBox.confirm(
      '结束后会话交还 AI，用户继续提问由 AI 回答。',
      '结束人工服务',
      { type: 'warning', confirmButtonText: '结束', cancelButtonText: '取消' },
    )
  } catch {
    return // 用户取消
  }
  try {
    await resolveTicket(ticket.id)
    ElMessage.success('已结束，会话交还 AI')
    await loadConversations()
  } catch {
    // 同上
  }
}

async function onReply(): Promise<void> {
  const text = draft.value.trim()
  if (!text || !activeId.value || !canReply.value) return
  sending.value = true
  try {
    const result = await replyToConversation(activeId.value, text)
    draft.value = ''
    appendMessage(result.message)
    if (!result.delivered) {
      // 消息已落库但没送达原渠道：坐席必须知道，否则以为用户看到了
      ElMessage.warning(`消息已保存，但未送达用户渠道：${result.delivery_error}`)
    }
    await scrollToBottom()
  } catch {
    // 拦截器已提示
  } finally {
    sending.value = false
  }
}

/** 按 id 去重：自己发出的消息会从 SSE 再回来一次 */
function appendMessage(message: MessageInfo): void {
  if (messages.value.some((item) => item.id === message.id)) return
  messages.value.push(message)
  void scrollToBottom()
}

/** 实时事件 payload → 消息结构（字段与 message_payload 一致） */
function toMessageInfo(data: Record<string, unknown>): MessageInfo | null {
  if (typeof data.id !== 'string' || !data.id) return null
  return {
    id: data.id,
    role: (typeof data.role === 'string' ? data.role : 'assistant') as MessageInfo['role'],
    content: typeof data.content === 'string' ? data.content : '',
    message_type: typeof data.message_type === 'string' ? data.message_type : 'text',
    source: typeof data.source === 'string' ? data.source : '',
    tokens: null,
    latency_ms: null,
    created_at: typeof data.created_at === 'string' ? data.created_at : new Date().toISOString(),
    sources: [],
    attachments: [],
    audio_url: typeof data.audio_url === 'string' ? data.audio_url : null,
    rating: typeof data.rating === 'number' ? data.rating : null,
  }
}

/** 事件可能成批到达，列表刷新合并到一次，避免一秒打十几个请求 */
let refreshTimer: number | null = null
function scheduleListRefresh(): void {
  if (refreshTimer !== null) return
  refreshTimer = window.setTimeout(() => {
    refreshTimer = null
    void loadConversations()
  }, 600)
}

function onEvent(event: RealtimeEvent): void {
  if (event.type === 'message.created') {
    const message = toMessageInfo(event.data)
    if (message && event.conversation_id === activeId.value) appendMessage(message)
    scheduleListRefresh()
    return
  }
  if (event.type === 'ticket.created') {
    ElMessage.info('有新的转人工请求')
    scheduleListRefresh()
    return
  }
  if (
    event.type === 'ticket.claimed' ||
    event.type === 'ticket.resolved' ||
    // 用户取消排队：这张单要从队列里消失，不能留给坐席去接
    event.type === 'ticket.cancelled' ||
    event.type === 'conversation.updated'
  ) {
    scheduleListRefresh()
  }
}

let eventsController: AbortController | null = null
let reconnectTimer: number | null = null
let retry = 0

async function connectNotifications(): Promise<void> {
  const current = new AbortController()
  eventsController = current
  try {
    await streamAgentNotifications(
      {
        onReady: () => {
          connected.value = true
          retry = 0
        },
        onEvent,
      },
      current.signal,
    )
  } catch {
    // 网络错误与主动中断同样处理：下面判断是否需要重连
  }
  connected.value = false
  if (current.signal.aborted) return
  const delay = Math.min(1000 * 2 ** retry, 15000)
  retry += 1
  reconnectTimer = window.setTimeout(() => void connectNotifications(), delay)
}

function disconnect(): void {
  eventsController?.abort()
  eventsController = null
  if (reconnectTimer !== null) {
    window.clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
  if (refreshTimer !== null) {
    window.clearTimeout(refreshTimer)
    refreshTimer = null
  }
}

onMounted(async () => {
  await loadConversations()
  void loadQuickReplies()
  void connectNotifications()
})

/** 话术库拉取失败不阻塞工作台：坐席没有常用语照样能打字 */
async function loadQuickReplies(): Promise<void> {
  try {
    quickReplies.value = await listQuickReplies()
  } catch {
    quickReplies.value = []
  }
}

function insertQuickReply(reply: QuickReplyInfo): void {
  if (!isMine.value) {
    ElMessage.warning('先接管会话再插入话术')
    return
  }
  // 追加而非覆盖：坐席常在标准话术后补一句针对性内容
  draft.value = draft.value ? `${draft.value}\n${reply.content}` : reply.content
}

/** 下拉选中后插入并复位（el-select 选中值不变，不复位无法重复选同一条） */
function insertQuickReplyById(replyId: string): void {
  const reply = quickReplies.value.find((item) => item.id === replyId)
  if (reply) insertQuickReply(reply)
}

onUnmounted(disconnect)

function formatTime(value: string): string {
  const date = new Date(value)
  return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}
</script>

<template>
  <div class="desk">
    <aside class="desk__queue">
      <header class="desk__head">
        <div class="desk__tabs">
          <button
            v-for="item in TABS"
            :key="item.key"
            type="button"
            class="desk__tab"
            :class="{ 'desk__tab--active': tab === item.key }"
            @click="changeTab(item.key)"
          >
            {{ item.label }}
          </button>
        </div>
        <span class="desk__conn" :class="{ 'desk__conn--on': connected }">
          {{ connected ? '实时' : '重连中' }}
        </span>
      </header>

      <el-empty
        v-if="!loadingList && !conversations.length"
        description="暂无待处理会话"
        :image-size="72"
      />
      <ul v-else v-loading="loadingList" class="queue">
        <li
          v-for="item in conversations"
          :key="item.id"
          class="queue__item"
          :class="{ 'queue__item--active': item.id === activeId }"
          @click="selectConversation(item)"
        >
          <div class="queue__head">
            <span class="queue__title">{{ item.title || '未命名会话' }}</span>
            <span class="queue__status">{{ STATUS_LABELS[item.status] ?? item.status }}</span>
          </div>
          <p v-if="item.ticket?.summary" class="queue__summary">{{ item.ticket.summary }}</p>
          <div class="queue__meta">
            {{ CHANNEL_LABELS[item.channel] ?? item.channel }} ·
            {{ item.owner_type === 'guest' ? '访客' : '登录用户' }} ·
            {{ formatTime(item.updated_at) }}
          </div>
        </li>
      </ul>
    </aside>

    <section class="desk__main">
      <template v-if="activeConversation">
        <header class="desk__toolbar">
          <div class="desk__title">
            <strong>{{ activeConversation.title || '未命名会话' }}</strong>
            <el-tag size="small" :type="isMine ? 'success' : 'warning'">
              {{ isMine ? '我处理中' : (STATUS_LABELS[activeConversation.status] ?? activeConversation.status) }}
            </el-tag>
          </div>
          <div class="desk__actions">
            <el-button v-if="!isMine" type="primary" @click="onClaim">接管会话</el-button>
            <el-button v-else @click="onResolve">结束人工服务</el-button>
          </div>
        </header>

        <div ref="listRef" v-loading="loadingMessages" class="desk__messages">
          <el-empty
            v-if="!messages.length"
            description="这个会话还没有消息"
            :image-size="64"
          />
          <div
            v-for="message in messages"
            :key="message.id"
            class="msg"
            :class="[`msg--${message.role}`, { 'msg--agent': message.source === 'agent' }]"
          >
            <div class="msg__meta">
              {{ message.role === 'user' ? '客户' : message.source === 'agent' ? '我' : 'AI' }}
              · {{ formatTime(message.created_at) }}
            </div>
            <!-- 内容已由 utils/markdown 消毒 -->
            <!-- eslint-disable vue/no-v-html -->
            <div class="msg__body" v-html="renderMarkdown(message.content)" />
            <!-- eslint-enable vue/no-v-html -->
            <audio v-if="message.audio_url" class="msg__audio" :src="message.audio_url" controls />
          </div>
        </div>

        <footer class="desk__composer">
          <el-input
            v-model="draft"
            type="textarea"
            :rows="3"
            resize="none"
            maxlength="8000"
            :disabled="!isMine"
            :placeholder="isMine ? '输入回复，Ctrl+Enter 发送' : '先接管会话才能回复'"
            @keydown.ctrl.enter.prevent="onReply"
          />
          <div class="desk__composer-actions">
            <span v-if="!isMine" class="desk__hint">接管后即可回复，回复会同时送达用户渠道</span>
            <el-select
              v-else-if="quickReplies.length"
              :model-value="''"
              placeholder="插入常用话术"
              size="small"
              class="desk__quick-replies"
              @change="insertQuickReplyById"
            >
              <el-option
                v-for="reply in quickReplies"
                :key="reply.id"
                :label="reply.title"
                :value="reply.id"
              >
                <span class="desk__quick-title">{{ reply.title }}</span>
                <span class="desk__quick-content">{{ reply.content }}</span>
              </el-option>
            </el-select>
            <el-button type="primary" :loading="sending" :disabled="!canReply || !draft.trim()" @click="onReply">
              发送
            </el-button>
          </div>
        </footer>
      </template>

      <el-empty v-else description="从左侧选择一个会话开始处理" />
    </section>
  </div>
</template>

<style scoped>
.desk {
  display: flex;
  /* 视口矮时按可用高度收缩，min-height 只保底不撑破页面 */
  height: calc(100vh - 132px);
  min-height: 420px;
  overflow: hidden;
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius);
  box-shadow: var(--app-shadow-sm);
}
.desk__queue {
  display: flex;
  flex-direction: column;
  width: 320px;
  flex: 0 0 320px;
  border-right: 1px solid var(--app-border);
  overflow: hidden;
}
.desk__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--app-border);
}
.desk__tabs {
  display: flex;
  gap: 4px;
}
.desk__tab {
  padding: 4px 8px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--el-text-color-regular);
  font-size: 12.5px;
  cursor: pointer;
}
.desk__tab:hover {
  background: var(--el-fill-color-light);
}
.desk__tab--active {
  background: var(--el-color-primary-light-9);
  color: var(--app-primary);
  font-weight: 600;
}
.desk__conn {
  font-size: 12px;
  color: var(--el-text-color-placeholder);
  white-space: nowrap;
}
.desk__conn--on {
  color: var(--el-color-success);
}
.queue {
  list-style: none;
  margin: 0;
  padding: 8px;
  overflow-y: auto;
  flex: 1;
}
.queue__item {
  padding: 10px;
  border-radius: 8px;
  cursor: pointer;
}
.queue__item:hover {
  background: var(--el-fill-color-light);
}
.queue__item--active {
  background: var(--el-color-primary-light-9);
}
.queue__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.queue__title {
  font-size: 13.5px;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.queue__status {
  flex: 0 0 auto;
  font-size: 11px;
  color: var(--el-color-warning);
}
.queue__summary {
  margin: 4px 0 0;
  font-size: 12.5px;
  color: var(--el-text-color-regular);
  line-height: 1.5;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.queue__meta {
  margin-top: 4px;
  font-size: 11.5px;
  color: var(--el-text-color-secondary);
}
.desk__main {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
}
.desk__toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--app-border);
}
.desk__title {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.desk__title strong {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.desk__actions {
  display: flex;
  gap: 8px;
  flex: 0 0 auto;
}
.desk__messages {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  background: var(--app-surface-muted);
}
.msg {
  max-width: 74%;
  margin-bottom: 14px;
}
.msg--user {
  margin-left: auto;
}
.msg__meta {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-bottom: 4px;
}
.msg--user .msg__meta {
  text-align: right;
}
.msg__body {
  padding: 10px 13px;
  border-radius: 12px;
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  line-height: 1.7;
  word-break: break-word;
}
.msg--user .msg__body {
  background: var(--app-primary);
  border-color: var(--app-primary);
  color: #fff;
}
/* 坐席自己的回复：左侧主色边条，与 AI 区分 */
.msg--agent .msg__body {
  border-left: 3px solid var(--app-primary);
}
.msg__body :deep(p) {
  margin: 0 0 8px;
}
.msg__body :deep(> :last-child) {
  margin-bottom: 0;
}
.msg__body :deep(code) {
  padding: 1px 5px;
  border-radius: 4px;
  background: rgba(0, 0, 0, 0.06);
  font-size: 12.5px;
}
.msg__audio {
  display: block;
  width: 100%;
  max-width: 260px;
  height: 34px;
  margin-top: 6px;
}
.desk__composer {
  padding: 12px 16px;
  border-top: 1px solid var(--app-border);
}
.desk__composer-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 12px;
  margin-top: 8px;
}
/* 话术选择器给固定宽度，避免长话术标题把"发送"按钮挤出可视区 */
.desk__quick-replies {
  width: 180px;
}
.desk__quick-title {
  float: left;
  font-weight: 600;
}
.desk__quick-content {
  float: right;
  max-width: 60%;
  overflow: hidden;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.desk__hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
