/**
 * 对话状态与流式发送（设计文档 5.1 / M2）。M5 起支持语音消息与语音回复。
 *
 * 四条约定：
 * 1. 会话懒创建：进页面不产生空会话，首次发送才 POST /conversations
 * 2. 先渲染后校准：本地先插入用户气泡，服务端回传 ID 后替换；
 *    assistant 全程流式追加，done 时以服务端全文覆盖（防止丢帧）
 * 3. 中断可控：stop() 断开 fetch，已收到的内容保留、不落库半截回答
 * 4. 语音是增强：音频晚于文字到达，合不出来只在气泡里提示，不影响回答
 */

import { ElMessage } from 'element-plus'
import { computed, ref, watch, type Ref } from 'vue'

import { streamConversationEvents } from '@/api/agent'
import { rateMessage as rateMessageApi } from '@/api/support'
import {
  cancelHandoff as cancelHandoffApi,
  createConversation,
  leaveMessage as leaveMessageApi,
  listConversations,
  listMessages,
  requestHandoff as requestHandoffApi,
  streamAudioMessage,
  streamChat,
  updateConversation,
  uploadImage as uploadImageApi,
  type ChatStreamHandlers,
} from '@/api/chat'
import { synthesizeSpeech } from '@/api/voice'
import type {
  ChatRole,
  ConversationInfo,
  LeaveMessagePayload,
  MessageAttachment,
  RealtimeEvent,
  SourceRef,
} from '@/api/types'

export interface ChatBubble {
  id: string
  role: ChatRole
  content: string
  /** 正在流式接收（用于显示光标） */
  streaming?: boolean
  /** 本轮失败（内容可能为空） */
  failed?: boolean
  /** 该条回答引用的知识片段（M3） */
  sources?: SourceRef[]
  /** 检索降级提示（如"嵌入模型未配置，本次回答未引用知识库"） */
  sourcesNotice?: string
  /** text / audio（M5） */
  messageType?: string
  /** 语音播放地址（用户录音或客服语音回复） */
  audioUrl?: string | null
  /** 语音消息正在转写 */
  transcribing?: boolean
  /** 语音合成失败的原因（文字回答仍然有效） */
  audioError?: string
  /** 正在请求合成（防重复点击） */
  audioPending?: boolean
  /** 消息来源（ai / agent）：坐席回复要在气泡上区分出来 */
  source?: string
  /** 附件（图片等）；语音播放地址走 audioUrl，不在这里 */
  attachments?: MessageAttachment[]
  /** 满意度评分 1-5（M8）；未评价为 undefined */
  rating?: number | null
}

/** 转人工状态：none=AI 直答，pending_handoff=排队等坐席，human=坐席已接入 */
export type HandoffStatus = 'none' | 'pending_handoff' | 'human'

const DEFAULT_TITLE = '新对话'
const TRANSCRIBING_HINT = '语音识别中…'
const TRANSCRIBE_FAILED_HINT = '语音识别失败，请重试或改用文字'

/**
 * 打字机节奏器。
 *
 * 模型 SSE 增量的长度和到达间隔都不稳定，直接渲染会出现"一段一段蹦"。
 * 这里先把增量压进缓冲，再按固定节拍匀速吐出：网络快时排队，网络抖动时补上。
 *
 * 吐出速度跟随"上游到达速度"（指数滑动平均），而不是写死一个速率：
 * 固定低速会让积压越滚越大，流结束时只能一次刷完；固定高速又等于没做平滑。
 */
const PACE_TICK_MS = 33
/** 每拍至少 1 字：积压少时逐字出，节奏最细腻 */
const PACE_MIN_CHARS = 1
/** 估算不出到达速度时的兜底速率（字/秒） */
const PACE_BASE_RATE = 30
/** 到达速率平滑系数：越大越跟手，也越容易被单个大 chunk 带偏 */
const PACE_RATE_ALPHA = 0.35
/**
 * 到达速率上限（字/秒），约每拍 2 字。
 * 一是防止首个大 chunk 把瞬时速率算到上万、一口气刷完缓冲；
 * 二是给画面留出"打字节拍"——完全跟随模型速度（100 字/秒以上）时
 * 中文读起来更像滚动，看不出逐字感。
 */
const PACE_MAX_RATE = 60
/** 每拍字数兜底上限 */
const PACE_MAX_CHARS = 8

interface Typewriter {
  push(chunk: string): void
  finish(done: () => void): void
  dispose(): void
}

function createTypewriter(apply: (chunk: string) => void): Typewriter {
  let backlog = ''
  let frameId = 0
  let lastTs = 0
  let elapsed = 0
  /** 上游到达速率（字/秒），平滑后的估算值 */
  let rate = 0
  let lastPushTs = 0
  /** 上游已结束：剩余内容加速吐完，吐干净后再执行收尾回调 */
  let finishing = false
  let onFinish: (() => void) | null = null

  function schedule(): void {
    if (!frameId) frameId = requestAnimationFrame(tick)
  }

  function drainOnce(): void {
    if (!backlog) return
    let size: number
    if (finishing) {
      // 收尾封顶 6 字/拍（约 180 字/秒）：按比例收敛会让最后一大段瞬间刷出来
      size = Math.min(6, Math.max(2, Math.ceil(backlog.length / 10)))
    } else {
      const perTick = (Math.max(rate, PACE_BASE_RATE) * PACE_TICK_MS) / 1000
      // 积压明显超出预期时多给一点，避免落后越滚越大
      const boost = backlog.length > perTick * 20 ? 1.5 : 1
      size = Math.min(PACE_MAX_CHARS, Math.max(PACE_MIN_CHARS, Math.round(perTick * boost)))
    }
    apply(backlog.slice(0, size))
    backlog = backlog.slice(size)
  }

  function tick(ts: number): void {
    frameId = 0
    if (!lastTs) lastTs = ts
    elapsed += ts - lastTs
    lastTs = ts
    // 标签页切回来时 elapsed 可能是秒级，限制补帧次数，否则会一次性跳完整段
    let budget = 3
    while (elapsed >= PACE_TICK_MS && budget > 0) {
      elapsed -= PACE_TICK_MS
      budget -= 1
      drainOnce()
    }
    if (budget === 0) elapsed = 0
    if (backlog) {
      schedule()
      return
    }
    lastTs = 0
    elapsed = 0
    if (finishing) {
      const done = onFinish
      finishing = false
      onFinish = null
      done?.()
    }
  }

  return {
    push(chunk) {
      if (!chunk) return
      const now = performance.now()
      if (lastPushTs) {
        // 同一帧内连来多个 chunk 时 dt 会趋近 0，设下限避免速率估算爆掉
        const dt = Math.max(16, now - lastPushTs)
        const instant = (chunk.length * 1000) / dt
        const smoothed = rate ? rate * (1 - PACE_RATE_ALPHA) + instant * PACE_RATE_ALPHA : instant
        rate = Math.min(PACE_MAX_RATE, smoothed)
      }
      lastPushTs = now
      backlog += chunk
      schedule()
    },
    finish(done) {
      // 已经在收尾就别覆盖回调：onDone 挂的是"以服务端全文补齐"的收尾逻辑
      if (finishing) return
      if (!backlog) {
        done()
        return
      }
      finishing = true
      onFinish = done
      schedule()
    },
    dispose() {
      if (frameId) cancelAnimationFrame(frameId)
      frameId = 0
      backlog = ''
      lastTs = 0
      elapsed = 0
      rate = 0
      lastPushTs = 0
      finishing = false
      onFinish = null
    },
  }
}

export function useChat(tenantId: Ref<string | null>) {
  const conversations = ref<ConversationInfo[]>([])
  const activeId = ref<string | null>(null)
  const bubbles = ref<ChatBubble[]>([])
  const sending = ref(false)
  const loadingList = ref(false)
  const loadingMessages = ref(false)
  const errorMessage = ref('')
  /** 会话级语音回复开关：无会话时先记在本地，建会话后落库 */
  const voiceReplyEnabled = ref(false)
  /** 转人工状态：决定工具栏显示"转人工/排队中/人工服务中" */
  const handoffStatus = ref<HandoffStatus>('none')
  const requestingHandoff = ref(false)
  /** 图片上传中：防重复点击，也让按钮转圈 */
  const uploadingImage = ref(false)

  let controller: AbortController | null = null
  /** 当前这条流对应的打字机；同一时刻只会有一条流 */
  let typewriter: Typewriter | null = null
  /** 用户端实时通道（接收坐席回复与会话状态变更） */
  let eventsController: AbortController | null = null
  let eventsTimer: number | null = null
  let eventsRetry = 0

  const activeConversation = computed(
    () => conversations.value.find((item) => item.id === activeId.value) ?? null,
  )
  const activeTitle = computed(() => activeConversation.value?.title || DEFAULT_TITLE)

  async function refreshConversations(): Promise<void> {
    if (!tenantId.value) return
    loadingList.value = true
    try {
      const data = await listConversations(tenantId.value)
      conversations.value = data.items
    } catch {
      // 错误已由 axios 拦截器提示，这里只需保持列表可用
    } finally {
      loadingList.value = false
    }
  }

  async function openConversation(conversationId: string): Promise<void> {
    if (!tenantId.value || conversationId === activeId.value) return
    discardStream()
    stop()
    loadingMessages.value = true
    errorMessage.value = ''
    try {
      const data = await listMessages(tenantId.value, conversationId)
      activeId.value = conversationId
      bubbles.value = data.items.map((item) => ({
        id: item.id,
        role: item.role,
        content: item.content,
        sources: item.sources,
        messageType: item.message_type,
        audioUrl: item.audio_url,
        source: item.source,
        attachments: item.attachments,
        rating: item.rating,
      }))
      const meta = conversations.value.find((item) => item.id === conversationId)
      voiceReplyEnabled.value = meta?.voice_reply_enabled ?? false
      // 历史会话可能正处于排队/人工中：状态要带过来，否则刷新后按钮又变成"转人工"
      handoffStatus.value =
        meta?.status === 'human'
          ? 'human'
          : meta?.status === 'pending_handoff'
            ? 'pending_handoff'
            : 'none'
    } catch {
      // 同上：拦截器已提示
    } finally {
      loadingMessages.value = false
    }
  }

  function newConversation(): void {
    discardStream()
    stop()
    activeId.value = null
    bubbles.value = []
    errorMessage.value = ''
    handoffStatus.value = 'none'
    // 语音偏好保留：用户刚开过语音回复，新会话继续沿用
  }

  /** 丢弃当前流的打字机与光标（切会话、离开页面时用；已收到的内容不再补吐） */
  function discardStream(): void {
    typewriter?.dispose()
    typewriter = null
    for (const bubble of bubbles.value) {
      if (bubble.streaming) bubble.streaming = false
    }
  }

  function stop(): void {
    controller?.abort()
    controller = null
    sending.value = false
    // 取最后一条：上一条回答可能还在吐字，用户点的停止针对的是当前这条
    const streaming = [...bubbles.value].reverse().find((bubble) => bubble.streaming)
    if (!streaming) return
    // 中断只停网络，已经收到的内容继续吐完：半截话突然静止比慢慢说完更让人困惑
    if (typewriter) {
      typewriter.finish(() => {
        streaming.streaming = false
      })
    } else {
      streaming.streaming = false
    }
  }

  /** 断开用户端实时通道（切会话、离开页面时） */
  function stopConversationEvents(): void {
    eventsController?.abort()
    eventsController = null
    if (eventsTimer !== null) {
      window.clearTimeout(eventsTimer)
      eventsTimer = null
    }
  }

  /**
   * 实时事件分发（设计文档 5.4 用户端）。
   *
   * 只接坐席回复：AI 回答与用户自己的消息已经在本地流式渲染过，
   * 事件流里再插一次就是重复气泡；按 id 去重是第二道保险。
   */
  function handleRealtimeEvent(event: RealtimeEvent): void {
    if (event.type === 'message.created') {
      const data = event.data as {
        id?: string
        role?: string
        source?: string
        content?: string
        message_type?: string
        audio_url?: string | null
      }
      if (data.role !== 'assistant' || data.source !== 'agent') return
      if (!data.id || bubbles.value.some((bubble) => bubble.id === data.id)) return
      bubbles.value.push({
        id: data.id,
        role: 'assistant',
        content: data.content ?? '',
        messageType: data.message_type,
        audioUrl: data.audio_url ?? null,
        source: 'agent',
      })
      return
    }

    if (event.type === 'conversation.updated' || event.type === 'ticket.created') {
      // conversation.updated 的 data.status 是会话状态；ticket.created 的是工单状态
      const status = typeof event.data.status === 'string' ? event.data.status : ''
      if (status === 'human') handoffStatus.value = 'human'
      else if (status === 'pending_handoff') handoffStatus.value = 'pending_handoff'
      else if (status === 'ai') handoffStatus.value = 'none'
    }
  }

  /**
   * 订阅当前会话的实时通道。坐席回复不经过用户的请求，
   * 没有这条通道用户只能刷新才知道"人来了"。
   * 断线按指数退避重连，10 次后放弃——后端真挂了，无限重试只会刷日志。
   */
  function startConversationEvents(conversationId: string): void {
    stopConversationEvents()
    eventsRetry = 0

    const run = async (): Promise<void> => {
      const current = new AbortController()
      eventsController = current
      try {
        await streamConversationEvents(
          tenantId.value,
          conversationId,
          { onEvent: handleRealtimeEvent },
          current.signal,
        )
      } catch {
        // 主动中断（AbortError）与网络错误一样走下面的退避判断
      }
      if (current.signal.aborted || eventsRetry >= 10) return
      eventsRetry += 1
      const delay = Math.min(1000 * 2 ** (eventsRetry - 1), 15000)
      eventsTimer = window.setTimeout(() => {
        eventsTimer = null
        void run()
      }, delay)
    }

    void run()
  }

  /** 转人工：会话是懒创建的，先确保有会话再建单 */
  async function requestHandoff(note = ''): Promise<void> {
    if (!tenantId.value || requestingHandoff.value) return
    requestingHandoff.value = true
    try {
      const conversationId = await ensureConversation()
      if (!conversationId) return
      const result = await requestHandoffApi(tenantId.value, conversationId, note)
      handoffStatus.value =
        result.conversation_status === 'human' ? 'human' : 'pending_handoff'
      startConversationEvents(conversationId)
      if (result.created) {
        ElMessage.success('已进入人工队列，坐席接入后会在这里回复')
      } else {
        ElMessage.info('该会话已在人工队列中，请稍候')
      }
    } catch {
      // 错误提示由 axios 拦截器统一给出
    } finally {
      requestingHandoff.value = false
    }
  }

  /** 取消排队：让用户能反悔，不必一直等一个可能没人接的单 */
  async function cancelHandoff(): Promise<void> {
    if (!tenantId.value || !activeId.value || requestingHandoff.value) return
    requestingHandoff.value = true
    try {
      const result = await cancelHandoffApi(tenantId.value, activeId.value)
      handoffStatus.value =
        result.conversation_status === 'human'
          ? 'human'
          : result.conversation_status === 'pending_handoff'
            ? 'pending_handoff'
            : 'none'
      if (result.cancelled) ElMessage.success('已取消排队，AI 继续为你服务')
      else ElMessage.info('当前没有排队中的请求')
    } catch {
      // 409：坐席刚好接入，取消失败是正确行为；以服务端状态为准回填
      await refreshConversations()
      const meta = conversations.value.find((item) => item.id === activeId.value)
      handoffStatus.value =
        meta?.status === 'human'
          ? 'human'
          : meta?.status === 'pending_handoff'
            ? 'pending_handoff'
            : 'none'
    } finally {
      requestingHandoff.value = false
    }
  }

  /**
   * 发送图片（M8）。
   *
   * 图片单独成一条 user 消息，不挂在文字消息上：前端拿到 id 就能立刻渲染，
   * 不必等整轮对话；下一轮提问时它会作为视觉输入带给模型。
   */
  async function uploadImage(file: File): Promise<void> {
    if (!tenantId.value || uploadingImage.value) return
    uploadingImage.value = true
    try {
      const conversationId = await ensureConversation()
      if (!conversationId) return
      const message = await uploadImageApi(tenantId.value, conversationId, file)
      bubbles.value.push({
        id: message.id,
        role: 'user',
        content: message.content,
        messageType: message.message_type,
        attachments: message.attachments,
      })
    } catch (error) {
      // 原生 fetch 不走 axios 拦截器，错误提示得自己做
      ElMessage.error(error instanceof Error ? error.message : '图片上传失败，请重试')
    } finally {
      uploadingImage.value = false
    }
  }

  /**
   * 给一条客服回复打满意度分（M8）。
   *
   * 只对已落库的 assistant 消息有效：本地占位气泡没有真实 ID，
   * 评分接口按消息 ID 定位，传 local-* 只会 404。
   */
  async function rateMessage(bubble: ChatBubble, score: number): Promise<void> {
    const conversationId = activeId.value
    if (!conversationId || bubble.role !== 'assistant') return
    if (bubble.id.startsWith('local-')) return
    try {
      await rateMessageApi(conversationId, bubble.id, score)
      bubble.rating = score
      ElMessage.success('感谢评价')
    } catch {
      // 拦截器已提示；本地 rating 不变，用户可重试
    }
  }

  /** 离线留言（M8）：提交后由坐席回访，不改变当前会话状态 */
  async function submitLeaveMessage(payload: LeaveMessagePayload): Promise<boolean> {
    if (!tenantId.value) return false
    const conversationId = await ensureConversation()
    if (!conversationId) return false
    try {
      await leaveMessageApi(tenantId.value, conversationId, payload)
      return true
    } catch {
      // 错误提示由拦截器给出
      return false
    }
  }

  // 会话一建立就订阅：访客与登录用户都要能收到坐席回复
  watch(activeId, (id) => {
    if (id) startConversationEvents(id)
    else stopConversationEvents()
  })

  async function setVoiceReply(enabled: boolean): Promise<void> {
    voiceReplyEnabled.value = enabled
    if (!activeId.value || !tenantId.value) return
    try {
      const updated = await updateConversation(tenantId.value, activeId.value, {
        voice_reply_enabled: enabled,
      })
      conversations.value = conversations.value.map((item) =>
        item.id === updated.id ? updated : item,
      )
    } catch {
      // 服务端没记住就不能显示"已开启"，否则用户会一直等一段不会来的音频
      voiceReplyEnabled.value = !enabled
    }
  }

  async function ensureConversation(): Promise<string | null> {
    if (activeId.value) return activeId.value
    if (!tenantId.value) return null
    try {
      const conversation = await createConversation(tenantId.value)
      activeId.value = conversation.id
      // 新会话排在最前，与服务端按 updated_at 倒序的排序一致
      conversations.value = [conversation, ...conversations.value]
      if (voiceReplyEnabled.value) {
        const patched = await updateConversation(tenantId.value, conversation.id, {
          voice_reply_enabled: true,
        })
        conversations.value = conversations.value.map((item) =>
          item.id === patched.id ? patched : item,
        )
      }
      return conversation.id
    } catch {
      return null
    }
  }

  /** 文本与语音共用的流事件处理；语音特有的"转写中"由调用方覆盖。 */
  function buildHandlers(
    assistant: ChatBubble,
    user: ChatBubble,
    options: { voice: boolean },
  ): ChatStreamHandlers {
    // 上一条还在吐字时不能 dispose：HTTP 已经结束但缓冲没吐完，丢弃会截断内容。
    // 旧节奏器会自己吐完并停掉 rAF，这里只把引用换成新的。
    typewriter = createTypewriter((chunk) => {
      assistant.content += chunk
    })
    return {
      onUserMessage: (message) => {
        user.id = message.id
        user.content = message.content
        user.messageType = message.message_type
        user.audioUrl = message.audio_url ?? null
        user.transcribing = false
      },
      onSources: (payload) => {
        assistant.sources = payload.items
        // 只有"本该检索却失败"才提示；没建知识库属于正常情况，不打扰用户
        assistant.sourcesNotice = payload.degraded
          ? payload.reason || '本次回答未引用知识库'
          : undefined
      },
      onDelta: (delta) => {
        typewriter?.push(delta)
      },
      onDone: (message) => {
        assistant.id = message.id
        assistant.messageType = message.message_type
        assistant.audioUrl = message.audio_url ?? null
        // 以服务端全文为准补齐丢帧，但要等缓冲吐完再覆盖，否则画面会突然跳一段
        const finalize = () => {
          assistant.content = message.content
          assistant.streaming = false
        }
        if (typewriter) typewriter.finish(finalize)
        else finalize()
      },
      onAudio: (payload) => {
        assistant.audioUrl = payload.audio_url
        assistant.audioError = undefined
      },
      onAudioFailed: (payload) => {
        // 只在气泡里标注，不弹 toast：文字回答已经是有效结果
        assistant.audioError = payload.message
      },
      onError: (error) => {
        if (options.voice && user.transcribing) {
          // 转写阶段失败：用户气泡要明确显示"这句话没送出去"
          user.transcribing = false
          user.failed = true
          user.content = TRANSCRIBE_FAILED_HINT
          discardStream()
          // 没有用户内容就没有回答，移除空气泡
          bubbles.value = bubbles.value.filter((bubble) => bubble.id !== assistant.id)
        } else {
          assistant.failed = true
          // 已经吐出来的内容保留，光标等缓冲清空后再收
          if (typewriter) typewriter.finish(() => (assistant.streaming = false))
          else assistant.streaming = false
        }
        errorMessage.value = error.message
        ElMessage.error(error.message)
      },
    }
  }

  async function runTurn(
    run: (conversationId: string, handlers: ChatStreamHandlers, signal: AbortSignal) => Promise<void>,
    assistant: ChatBubble,
    user: ChatBubble,
    options: { voice: boolean },
  ): Promise<void> {
    const conversationId = await ensureConversation()
    if (!conversationId) {
      // 会话创建失败：清掉本地占位气泡，避免留下无法续聊的假消息
      bubbles.value = bubbles.value.filter(
        (bubble) => bubble.id !== assistant.id && bubble.id !== user.id,
      )
      sending.value = false
      return
    }

    controller = new AbortController()
    try {
      await run(conversationId, buildHandlers(assistant, user, options), controller.signal)
    } catch (error) {
      if ((error as DOMException).name !== 'AbortError') {
        assistant.failed = true
        errorMessage.value = '网络中断，请检查网络后重试'
        ElMessage.error(errorMessage.value)
      }
      // 中断和故障同一收尾：先把已收到的内容吐完，再收光标
      const settle = () => {
        assistant.streaming = false
      }
      if (typewriter) typewriter.finish(settle)
      else settle()
    } finally {
      sending.value = false
      controller = null
      // 标题由首条消息生成、updated_at 也会变化，发送后同步一次列表
      void refreshConversations()
    }
  }

  async function send(content: string): Promise<void> {
    const text = content.trim()
    if (!text || sending.value || !tenantId.value) return

    errorMessage.value = ''
    sending.value = true

    const stamp = Date.now()
    bubbles.value.push({ id: `local-user-${stamp}`, role: 'user', content: text })
    const user = bubbles.value[bubbles.value.length - 1]
    bubbles.value.push({
      id: `local-assistant-${stamp}`,
      role: 'assistant',
      content: '',
      streaming: true,
    })
    const assistant = bubbles.value[bubbles.value.length - 1]

    await runTurn(
      (conversationId, handlers, signal) =>
        streamChat(tenantId.value, conversationId, text, handlers, signal),
      assistant,
      user,
      { voice: false },
    )
  }

  /**
   * 发送语音消息（M5）：上传 → 转写 → 生成。
   *
   * 本地先放一条"识别中"的用户气泡：转写要 1-3 秒，没有反馈用户会以为没点上。
   * 转写结果由服务端 `message` 事件回传，直接覆盖这条气泡。
   */
  async function sendAudio(blob: Blob, durationSeconds: number): Promise<void> {
    if (sending.value || !tenantId.value) return

    errorMessage.value = ''
    sending.value = true

    const stamp = Date.now()
    bubbles.value.push({
      id: `local-user-${stamp}`,
      role: 'user',
      content: TRANSCRIBING_HINT,
      messageType: 'audio',
      transcribing: true,
    })
    const user = bubbles.value[bubbles.value.length - 1]
    bubbles.value.push({
      id: `local-assistant-${stamp}`,
      role: 'assistant',
      content: '',
      streaming: true,
    })
    const assistant = bubbles.value[bubbles.value.length - 1]

    await runTurn(
      (conversationId, handlers, signal) =>
        streamAudioMessage(tenantId.value, conversationId, blob, durationSeconds, handlers, signal),
      assistant,
      user,
      { voice: true },
    )
  }

  /** 离开页面时清理：rAF 循环不停会一直写已卸载组件的状态 */
  function dispose(): void {
    controller?.abort()
    controller = null
    stopConversationEvents()
    discardStream()
  }

  /** 手动为某条回答合成语音（会话未开自动语音回复时用）。 */
  async function synthesize(bubble: ChatBubble): Promise<void> {
    if (bubble.audioUrl || bubble.audioPending) return
    bubble.audioPending = true
    try {
      const result = await synthesizeSpeech(bubble.id)
      bubble.audioUrl = result.audio_url
      bubble.audioError = undefined
    } catch (error) {
      bubble.audioError = (error as Error).message
    } finally {
      bubble.audioPending = false
    }
  }

  return {
    conversations,
    activeId,
    activeTitle,
    bubbles,
    sending,
    loadingList,
    loadingMessages,
    errorMessage,
    voiceReplyEnabled,
    handoffStatus,
    requestingHandoff,
    requestHandoff,
    cancelHandoff,
    uploadingImage,
    uploadImage,
    submitLeaveMessage,
    rateMessage,
    refreshConversations,
    openConversation,
    newConversation,
    send,
    sendAudio,
    synthesize,
    setVoiceReply,
    stop,
    dispose,
  }
}
