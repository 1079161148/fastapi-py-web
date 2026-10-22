<script setup lang="ts">
/**
 * 聊天页（设计文档 M2）。
 *
 * 租户解析：?tenant=<uuid> → 已登录用户的租户 → 本地保存的租户。
 * 生产环境应由子域名（acme.example.com）解析并在中间件层注入，
 * 本地演示保留查询参数与应用内填写两种入口。
 *
 * 访客模式：进入页面即 POST /guest/sessions 换取 HttpOnly cookie；
 * 登录后若检出未合并的访客对话，展示"合并到账号"提示（双凭证合并）。
 */
import { Microphone } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import { createOrResumeGuestSession, fetchCurrentGuestSession, mergeGuest } from '@/api/chat'
import { tokenStorage } from '@/api/client'
import type { GuestSessionCurrent, VoiceCapabilities } from '@/api/types'
import { fetchVoiceCapabilities } from '@/api/voice'
import { useChat, type ChatBubble } from '@/composables/useChat'
import { useRecorder } from '@/composables/useRecorder'
import { useAuthStore } from '@/stores/auth'
import { renderMarkdown, renderStreamingMarkdown } from '@/utils/markdown'

const auth = useAuthStore()
const route = useRoute()

const tenantId = ref<string | null>(null)
const tenantInput = ref('')
const booting = ref(false)
const guestMode = ref(false)
const guestInfo = ref<GuestSessionCurrent | null>(null)
const merging = ref(false)
const draft = ref('')
const drawerVisible = ref(false)
const listRef = ref<HTMLElement | null>(null)
/** 语音能力（null = 尚未探测完，按钮按不可用处理，避免误点） */
const voiceCaps = ref<VoiceCapabilities | null>(null)

const {
  conversations,
  activeId,
  bubbles,
  sending,
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
  dispose: disposeChat,
} = useChat(tenantId)

const {
  supported: recorderSupported,
  recording: recordingActive,
  elapsed: recordingSeconds,
  errorMessage: recorderError,
  start: startRecording,
  stop: stopRecording,
  cancel: cancelRecording,
} = useRecorder(() => voiceCaps.value?.max_seconds ?? 60)

const canRecord = computed(
  () => Boolean(voiceCaps.value?.transcription) && recorderSupported && !sending.value,
)
const canSpeak = computed(() => Boolean(voiceCaps.value?.tts))

/** 麦克风不可用时把原因说清楚，别让用户对着灰按钮猜 */
const recordHint = computed(() => {
  if (!recorderSupported) return '当前浏览器不支持录音，请改用文字输入'
  const missing = voiceCaps.value?.missing_transcription ?? []
  if (missing.length) return `语音转写未配置：需要 ${missing.join(' / ')}`
  return '点击开始录音'
})

/** 只有服务端已落库的回答才能请求合成（本地占位气泡没有真实 ID） */
function canSynthesize(bubble: ChatBubble): boolean {
  return (
    canSpeak.value &&
    bubble.role === 'assistant' &&
    !bubble.audioUrl &&
    !bubble.streaming &&
    !bubble.failed &&
    !bubble.id.startsWith('local-')
  )
}

/**
 * 只有已落库、已完成生成、非失败的客服回答才能评分。
 * 本地占位气泡没有真实 ID，流式中的回答还没写完，评了也没意义。
 */
function canRate(bubble: ChatBubble): boolean {
  return (
    bubble.role === 'assistant' &&
    !bubble.id.startsWith('local-') &&
    !bubble.streaming &&
    !bubble.failed
  )
}

/**
 * 欢迎语（M8）：只做本地展示，不进对话上下文。
 * 访客没有令牌，读不到 /tenants/current，只能从访客会话接口带回。
 */
const welcomeMessage = ref('')

const loginLink = computed(() => ({
  path: '/login',
  query: { redirect: `/chat?tenant=${tenantId.value ?? ''}` },
}))

const showMergeBanner = computed(
  () => auth.isLoggedIn && (guestInfo.value?.conversation_count ?? 0) > 0,
)

function resolveTenant(): string | null {
  const fromQuery = route.query.tenant
  if (typeof fromQuery === 'string' && fromQuery.trim()) return fromQuery.trim()
  if (auth.tenantId) return auth.tenantId
  return tokenStorage.getTenant()
}

async function startChat(id: string): Promise<void> {
  tenantId.value = id
  booting.value = true
  try {
    if (!auth.isLoggedIn) {
      // 幂等：已有可用 cookie 时后端直接复用，刷新页面不丢历史
      const info = await createOrResumeGuestSession(id)
      guestMode.value = true
      welcomeMessage.value = info.welcome_message?.trim() ?? ''
    } else {
      guestInfo.value = await fetchCurrentGuestSession(id).catch(() => null)
      welcomeMessage.value = auth.tenant?.settings?.welcome_message?.trim() ?? ''
    }
    await refreshConversations()
    const latest = conversations.value[0]
    if (latest) await openConversation(latest.id)
  } finally {
    booting.value = false
  }
}

function onApplyTenant(): void {
  const id = tenantInput.value.trim()
  if (!id) return
  void startChat(id)
}

onMounted(() => {
  // 能力探测与租户无关，失败时静默（默认不显示麦克风，不影响文字对话）
  void fetchVoiceCapabilities()
    .then((caps) => {
      voiceCaps.value = caps
    })
    .catch(() => {
      voiceCaps.value = null
    })

  const id = resolveTenant()
  if (id) void startChat(id)
})

async function onMerge(): Promise<void> {
  merging.value = true
  try {
    const result = await mergeGuest(tenantId.value)
    if (result.status === 'merged') {
      ElMessage.success(`已合并 ${result.merged_conversations} 个访客会话`)
    } else if (result.status === 'already_merged') {
      ElMessage.info('访客会话已合并过，无需重复操作')
    } else {
      ElMessage.info('当前没有可合并的访客会话')
    }
    guestInfo.value = null
    await refreshConversations()
  } finally {
    merging.value = false
  }
}

function onKeydown(event: KeyboardEvent): void {
  // Enter 发送、Shift+Enter 换行；输入法组字期间的 Enter 必须放行
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return
  event.preventDefault()
  void onSend()
}

async function onSend(): Promise<void> {
  const text = draft.value.trim()
  if (!text || sending.value) return
  draft.value = ''
  await send(text)
}

async function onSelectConversation(id: string): Promise<void> {
  drawerVisible.value = false
  await openConversation(id)
}

/**
 * 打开历史抽屉时补拉一次列表。
 * 列表只在发送后刷新，别的标签页或后台新建的会话不会自动出现；
 * 先开抽屉再刷新，避免用户看到过期数据，也不会有空白等待。
 */
async function openHistory(): Promise<void> {
  drawerVisible.value = true
  await refreshConversations()
}

/**
 * 麦克风按钮：点击开始、再点"发送"结束。
 *
 * 不用"按住说话"：移动端长按与页面滚动冲突，桌面端也不符合"先说完再决定"的
 * 心理预期。录到上限会自动停（useRecorder 内），这里只负责发送。
 */
async function onMicToggle(): Promise<void> {
  if (recordingActive.value) {
    const result = await stopRecording()
    if (result) await sendAudio(result.blob, result.duration)
    return
  }
  const started = await startRecording()
  if (!started && recorderError.value) ElMessage.error(recorderError.value)
}

function onCancelRecording(): void {
  cancelRecording()
}

/** 气泡内容渲染：流式中间态要裁掉未闭合标记，避免末尾星号闪烁 */
function renderBubble(bubble: ChatBubble): string {
  return bubble.streaming ? renderStreamingMarkdown(bubble.content) : renderMarkdown(bubble.content)
}

/** 气泡署名：坐席回复要与 AI 回答区分，否则用户不知道现在是谁在说话 */
function roleLabel(bubble: ChatBubble): string {
  if (bubble.role === 'user') return '我'
  return bubble.source === 'agent' ? '人工客服' : 'AI 客服'
}

async function onRequestHandoff(): Promise<void> {
  await requestHandoff()
}

async function onCancelHandoff(): Promise<void> {
  await cancelHandoff()
}

/** 与后端 assets.py 的魔数白名单对齐：只放行这三种，选错在客户端就拦下 */
const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/webp']

const imageInputRef = ref<HTMLInputElement | null>(null)
const leaveDialog = ref(false)
const submittingLeave = ref(false)
const leaveForm = ref({ name: '', contact: '', content: '' })

function pickImage(): void {
  imageInputRef.value?.click()
}

async function onImagePicked(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  // 清空 value：否则连续选同一张图不会触发 change
  input.value = ''
  if (!file) return
  if (!IMAGE_TYPES.includes(file.type)) {
    ElMessage.warning('仅支持 PNG / JPEG / WebP 图片')
    return
  }
  await uploadImage(file)
}

/** 气泡里的图片附件（语音走 audio_url，不在这里） */
function imageAttachments(bubble: ChatBubble): Array<{ url: string }> {
  return (bubble.attachments ?? [])
    .filter((item) => item.type === 'image' && item.url)
    .map((item) => ({ url: item.url as string }))
}

async function submitLeave(): Promise<void> {
  const contact = leaveForm.value.contact.trim()
  const content = leaveForm.value.content.trim()
  if (contact.length < 3 || !content) {
    ElMessage.warning('请填写有效的联系方式和留言内容')
    return
  }
  submittingLeave.value = true
  try {
    const ok = await submitLeaveMessage({
      name: leaveForm.value.name.trim(),
      contact,
      content,
    })
    if (ok) {
      ElMessage.success('留言已提交，坐席会尽快联系你')
      leaveDialog.value = false
      leaveForm.value = { name: '', contact: '', content: '' }
    }
  } finally {
    submittingLeave.value = false
  }
}

async function onSpeak(bubble: ChatBubble): Promise<void> {
  await synthesize(bubble)
}

async function onVoiceReplyChange(value: string | number | boolean): Promise<void> {
  await setVoiceReply(Boolean(value))
}

/** 是否跟随到底部：逐字输出时每帧都滚会把用户正在看的段落拉走，上翻后先停跟随 */
const pinnedToBottom = ref(true)

function onListScroll(): void {
  const el = listRef.value
  if (!el) return
  pinnedToBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < 80
}

function scrollToBottom(force = false): void {
  void nextTick(() => {
    const el = listRef.value
    if (!el) return
    if (!force && !pinnedToBottom.value) return
    el.scrollTop = el.scrollHeight
    pinnedToBottom.value = true
  })
}

watch(bubbles, () => scrollToBottom(), { deep: true, flush: 'post' })
// 换会话是明确的"看最新"，直接跟到底
watch(activeId, () => scrollToBottom(true), { flush: 'post' })

onUnmounted(disposeChat)
</script>

<template>
  <div class="chat">
    <el-card v-if="!tenantId" class="chat__setup" shadow="never">
      <h2 class="chat__setup-title">先确定租户</h2>
      <p class="chat__setup-desc">
        匿名访客的对话必须归属某个租户。生产环境由子域名（如 acme.example.com）解析，
        本地演示请填写租户 ID（可从注册返回结果或首页信息中获取）。
      </p>
      <el-input v-model="tenantInput" placeholder="租户 ID（UUID）" clearable />
      <el-button
        class="chat__setup-button"
        type="primary"
        :disabled="!tenantInput.trim()"
        @click="onApplyTenant"
      >
        开始对话
      </el-button>
    </el-card>

    <template v-else>
      <header class="chat__header">
        <div class="chat__title">
          <h1>智能客服</h1>
          <el-tag size="small" :type="guestMode ? 'warning' : 'success'">
            {{ guestMode ? '访客模式' : '已登录' }}
          </el-tag>
        </div>
        <div class="chat__actions">
          <el-tooltip
            v-if="canSpeak"
            :content="voiceReplyEnabled ? '语音回复：开（点击关闭）' : '语音回复：关（点击开启）'"
            placement="bottom"
          >
            <el-switch
              :model-value="voiceReplyEnabled"
              :disabled="sending"
              size="small"
              aria-label="语音回复开关"
              @change="onVoiceReplyChange"
            />
          </el-tooltip>
          <el-button link @click="openHistory">历史会话</el-button>
          <el-button link :disabled="sending" @click="newConversation()">新对话</el-button>
          <!-- 转人工：排队/人工中只显示状态，避免重复建单；排队允许反悔 -->
          <span v-if="handoffStatus === 'pending_handoff'" class="chat__handoff">
            <el-tag type="warning" size="small">等待坐席接入</el-tag>
            <el-button link size="small" :loading="requestingHandoff" @click="onCancelHandoff">
              取消排队
            </el-button>
          </span>
          <el-tag v-else-if="handoffStatus === 'human'" type="success" size="small">
            人工服务中
          </el-tag>
          <el-button v-else link :loading="requestingHandoff" @click="onRequestHandoff">
            转人工
          </el-button>
          <!-- 留言兜底：坐席全忙或已下班时，用户至少能留下联系方式 -->
          <el-button link @click="leaveDialog = true">留言</el-button>
          <router-link v-if="auth.isLoggedIn" to="/knowledge">
            <el-button link>知识库</el-button>
          </router-link>
          <router-link v-if="!auth.isLoggedIn" :to="loginLink">
            <el-button link type="primary">登录</el-button>
          </router-link>
        </div>
      </header>

      <el-alert
        v-if="showMergeBanner"
        class="chat__banner"
        type="warning"
        :closable="false"
        show-icon
        :title="`检测到 ${guestInfo?.conversation_count} 个未合并的访客会话`"
      >
        <template #default>
          <span class="chat__banner-text">合并后可跨设备查看这段对话。</span>
          <el-button size="small" type="warning" :loading="merging" @click="onMerge">
            合并到当前账号
          </el-button>
        </template>
      </el-alert>

      <el-alert
        v-else-if="guestMode"
        class="chat__banner"
        type="info"
        :closable="false"
        show-icon
        title="访客模式：对话仅保存在本浏览器 30 天，登录后可合并到账号"
      />

      <el-alert
        v-if="errorMessage"
        class="chat__banner"
        type="error"
        :closable="false"
        show-icon
        :title="errorMessage"
      />

      <!-- 欢迎语只在还没有任何消息时出现：它是开场白，不该一直挂在列表上方 -->
      <el-alert
        v-if="welcomeMessage && !bubbles.length"
        class="chat__banner"
        type="info"
        :closable="false"
        show-icon
        :title="welcomeMessage"
      />

      <section
        ref="listRef"
        v-loading="booting || loadingMessages"
        class="chat__messages"
        @scroll.passive="onListScroll"
      >
        <el-empty
          v-if="!bubbles.length"
          description="开始你的第一个问题，例如：你们的服务时间是？"
        />
        <div
          v-for="bubble in bubbles"
          :key="bubble.id"
          class="bubble"
          :class="[`bubble--${bubble.role}`, { 'bubble--agent': bubble.source === 'agent' }]"
        >
          <div class="bubble__meta">
            {{ roleLabel(bubble) }}
            <span v-if="bubble.failed" class="bubble__failed">· 回复失败</span>
          </div>
          <!-- 纯图片消息没有文字：不画空文本框，只留图片 -->
          <div
            v-if="bubble.streaming || bubble.content || !imageAttachments(bubble).length"
            class="bubble__content"
          >
            <!-- 首 token 到达前先给等待反馈，否则就是一个空白气泡 -->
            <div v-if="bubble.streaming && !bubble.content" class="bubble__waiting">
              <span></span><span></span><span></span>
            </div>
            <!-- 内容已由 utils/markdown 消毒，允许插入 HTML（多行元素无法用 next-line 禁用） -->
            <!-- eslint-disable vue/no-v-html -->
            <div
              v-else
              class="bubble__markdown"
              :class="{ 'bubble__markdown--streaming': bubble.streaming }"
              v-html="renderBubble(bubble)"
            />
            <!-- eslint-enable vue/no-v-html -->
          </div>
          <!-- 语音消息：转写前显示占位文案，转写后由 content 覆盖 -->
          <p v-if="bubble.transcribing" class="bubble__notice">正在转写，请稍候…</p>
          <audio
            v-if="bubble.audioUrl"
            class="bubble__audio"
            :src="bubble.audioUrl"
            controls
            preload="metadata"
          />
          <div v-if="imageAttachments(bubble).length" class="bubble__images">
            <el-image
              v-for="(img, index) in imageAttachments(bubble)"
              :key="`${bubble.id}-img-${index}`"
              class="bubble__image"
              :src="img.url"
              :preview-src-list="imageAttachments(bubble).map((item) => item.url)"
              :initial-index="index"
              preview-teleported
            />
          </div>
          <div v-if="canSynthesize(bubble)" class="bubble__tools">
            <el-button link size="small" :loading="bubble.audioPending" @click="onSpeak(bubble)">
              朗读
            </el-button>
          </div>
          <!-- 满意度评价（M8）：只对已落库的客服回答开放，评分可覆盖 -->
          <div v-if="canRate(bubble)" class="bubble__rating">
            <span class="bubble__rating-label">
              {{ bubble.rating ? '已评价，可点击修改' : '这条回答有帮助吗？' }}
            </span>
            <el-rate
              :model-value="bubble.rating ?? 0"
              size="small"
              @change="(score: number) => rateMessage(bubble, score)"
            />
          </div>
          <p v-if="bubble.audioError" class="bubble__notice">{{ bubble.audioError }}</p>
          <el-collapse v-if="bubble.sources?.length" class="bubble__sources">
            <el-collapse-item :title="`引用 ${bubble.sources.length} 条知识`">
              <div
                v-for="(source, index) in bubble.sources"
                :key="source.chunk_id"
                class="source"
              >
                <p class="source__head">[{{ index + 1 }}] {{ source.document_name }}</p>
                <p class="source__body">{{ source.content }}</p>
              </div>
            </el-collapse-item>
          </el-collapse>
          <p v-if="bubble.sourcesNotice" class="bubble__notice">{{ bubble.sourcesNotice }}</p>
        </div>
      </section>

      <footer class="chat__composer">
        <div v-if="recordingActive" class="chat__recording">
          <span class="chat__recording-dot" />
          <span class="chat__recording-text">
            正在录音 {{ recordingSeconds.toFixed(1) }}s /
            {{ voiceCaps?.max_seconds ?? 60 }}s
          </span>
          <div class="chat__recording-actions">
            <el-button size="small" @click="onCancelRecording">取消</el-button>
            <el-button size="small" type="primary" @click="onMicToggle">发送</el-button>
          </div>
        </div>
        <el-input
          v-else
          v-model="draft"
          type="textarea"
          :rows="3"
          resize="none"
          maxlength="8000"
          placeholder="输入问题，Enter 发送，Shift+Enter 换行"
          @keydown="onKeydown"
        />
        <div class="chat__composer-actions">
          <el-tooltip :disabled="canRecord" :content="recordHint" placement="top">
            <span>
              <el-button :disabled="!canRecord || recordingActive" @click="onMicToggle">
                <el-icon><Microphone /></el-icon>
                <span class="chat__mic-label">语音</span>
              </el-button>
            </span>
          </el-tooltip>
          <!-- 隐藏的文件输入：按钮只能程序化触发，原生 input 在各浏览器样式不可控 -->
          <input
            ref="imageInputRef"
            class="chat__file-input"
            type="file"
            accept="image/png,image/jpeg,image/webp"
            @change="onImagePicked"
          />
          <el-button
            :loading="uploadingImage"
            :disabled="sending || recordingActive"
            @click="pickImage"
          >
            图片
          </el-button>
          <el-button v-if="sending" @click="stop()">停止生成</el-button>
          <el-button type="primary" :loading="sending" :disabled="!draft.trim()" @click="onSend">
            发送
          </el-button>
        </div>
      </footer>

      <el-drawer v-model="drawerVisible" title="历史会话" size="320px">
        <el-empty v-if="!conversations.length" description="暂无会话" />
        <ul v-else class="history">
          <li
            v-for="item in conversations"
            :key="item.id"
            class="history__item"
            :class="{ 'history__item--active': item.id === activeId }"
            @click="onSelectConversation(item.id)"
          >
            <span class="history__title">{{ item.title || '新对话' }}</span>
            <span class="history__time">{{ new Date(item.updated_at).toLocaleString() }}</span>
          </li>
        </ul>
      </el-drawer>

      <el-dialog v-model="leaveDialog" title="留下联系方式" width="420px" append-to-body>
        <p class="chat__leave-hint">
          坐席暂时不在线时可以先留言，我们会按填写的联系方式回复你。
        </p>
        <el-form label-width="72px" @submit.prevent>
          <el-form-item label="称呼">
            <el-input v-model="leaveForm.name" maxlength="100" placeholder="选填" clearable />
          </el-form-item>
          <el-form-item label="联系方式">
            <el-input
              v-model="leaveForm.contact"
              maxlength="200"
              placeholder="手机号 / 邮箱 / 微信号"
              clearable
            />
          </el-form-item>
          <el-form-item label="留言">
            <el-input
              v-model="leaveForm.content"
              type="textarea"
              :rows="4"
              maxlength="2000"
              show-word-limit
              placeholder="描述你的问题，方便坐席提前准备"
            />
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="leaveDialog = false">取消</el-button>
          <el-button type="primary" :loading="submittingLeave" @click="submitLeave">
            提交
          </el-button>
        </template>
      </el-dialog>
    </template>
  </div>
</template>

<style scoped>
/* 高度由外壳内容区决定（flex:1 + min-height:0），不再自己占满视口；
   否则嵌进外壳后总高会超过一屏，出现页面级双滚动条 */
.chat {
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-width: 880px;
  height: 100%;
  margin: 0 auto;
  box-sizing: border-box;
}
.chat__setup {
  margin-top: 64px;
}
.chat__setup-title {
  margin: 0 0 8px;
}
.chat__setup-desc {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.7;
}
.chat__setup-button {
  margin-top: 12px;
  width: 100%;
}
.chat__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 16px;
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius);
  box-shadow: var(--app-shadow-sm);
}
.chat__title {
  display: flex;
  align-items: center;
  gap: 8px;
}
.chat__title h1 {
  margin: 0;
  font-size: 16px;
  font-weight: 650;
}
.chat__actions {
  display: flex;
  align-items: center;
  gap: 4px;
}
.chat__banner {
  margin-top: 0;
}
.chat__banner-text {
  margin-right: 12px;
}
.chat__messages {
  flex: 1;
  overflow-y: auto;
  padding: 20px 20px 8px;
  margin-top: 0;
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius);
  box-shadow: var(--app-shadow-sm);
}
.bubble {
  max-width: 82%;
  margin-bottom: 16px;
}
.bubble--user {
  margin-left: auto;
}
.bubble__meta {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-bottom: 4px;
}
.bubble--user .bubble__meta {
  text-align: right;
}
.bubble__failed {
  color: var(--el-color-danger);
}
.bubble__content {
  padding: 11px 14px;
  border-radius: 14px;
  background: var(--app-surface-muted);
  border: 1px solid var(--app-border);
  line-height: 1.75;
  word-break: break-word;
}
/* Markdown 渲染结果：块级元素自带外边距，收紧首尾避免气泡上下留白过大 */
.bubble__markdown > :first-child {
  margin-top: 0;
}
.bubble__markdown > :last-child {
  margin-bottom: 0;
}
.bubble__markdown p,
.bubble__markdown ul,
.bubble__markdown ol,
.bubble__markdown pre,
.bubble__markdown blockquote,
.bubble__markdown table {
  margin: 0 0 8px;
}
.bubble__markdown ul,
.bubble__markdown ol {
  padding-left: 20px;
}
.bubble__markdown li + li {
  margin-top: 3px;
}
.bubble__markdown blockquote {
  padding-left: 10px;
  border-left: 3px solid var(--app-border);
  color: var(--el-text-color-secondary);
}
.bubble__markdown code {
  padding: 1px 5px;
  border-radius: 4px;
  background: rgba(0, 0, 0, 0.06);
  font-size: 12.5px;
}
.bubble__markdown pre {
  padding: 10px 12px;
  border-radius: 8px;
  background: rgba(0, 0, 0, 0.06);
  overflow-x: auto;
}
.bubble__markdown pre code {
  padding: 0;
  background: none;
}
/* 用户气泡是深底白字，代码块底色需要反过来 */
.bubble--user .bubble__markdown code,
.bubble--user .bubble__markdown pre {
  background: rgba(255, 255, 255, 0.18);
}
/* 靠近发送方的一角收紧，形成"对话气泡"的方向感 */
.bubble--assistant .bubble__content {
  border-bottom-left-radius: 4px;
}
/* 坐席回复：左侧主色边条，扫一眼就能分清谁在说话 */
.bubble--agent .bubble__content {
  border-left: 3px solid var(--app-primary);
}
/* 排队状态与"取消排队"并排，别让工具栏被撑高 */
.chat__handoff {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.bubble--user .bubble__content {
  border-color: var(--app-primary);
  border-bottom-right-radius: 4px;
  background: var(--app-primary);
  color: #fff;
}
.bubble__audio {
  display: block;
  width: 260px;
  max-width: 100%;
  height: 36px;
  margin-top: 8px;
}
.bubble__tools {
  margin-top: 2px;
}
/* 评分条：星星本身不大，和提示文字同高才不会错位 */
.bubble__rating {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 4px;
}
.bubble__rating-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
/* 默认星星偏大，挤在气泡下方会撑高不少 */
.bubble__rating :deep(.el-rate__icon) {
  font-size: 16px;
  margin-right: 2px;
}
/* 图片附件：多图并排折行，单图不撑破气泡 */
.bubble__images {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}
/* 图片按原始比例显示：只限制最大宽高，不裁内容；长截图被高度上限挡住，不会把气泡撑爆 */
.bubble__image {
  max-width: 200px;
  max-height: 220px;
  border-radius: 10px;
  overflow: hidden;
  cursor: zoom-in;
  background: var(--app-surface-muted);
}
/* el-image 内部 img 默认 width/height:100%，容器尺寸由内容决定时会形成循环，
   这里改成 auto + 上限，让浏览器按原比例算出最终尺寸 */
.bubble__image :deep(.el-image__inner) {
  display: block;
  width: auto;
  height: auto;
  max-width: 200px;
  max-height: 220px;
}
/* 用户消息靠右：图片贴气泡右边界，与文字气泡、"我" 的右边缘落在同一条线上；
   AI/坐席图片保持默认左对齐，与左侧文字气泡同列 */
.bubble--user .bubble__images {
  justify-content: flex-end;
}
.bubble__sources {
  margin-top: 6px;
  font-size: 12px;
}
.bubble__notice {
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--el-color-warning);
}
.source + .source {
  margin-top: 8px;
}
.source__head {
  margin: 0;
  font-weight: 600;
  color: var(--el-text-color-regular);
}
.source__body {
  margin: 2px 0 0;
  color: var(--el-text-color-secondary);
  line-height: 1.6;
}
/* 流式光标挂到最后一段末尾，插入块级元素之后会另起一行 */
.bubble__markdown--streaming > :last-child::after {
  content: '▍';
  animation: blink 1s steps(2, start) infinite;
}
/* 首 token 到达前的三点等待，高度固定，避免出现内容时气泡跳一下 */
.bubble__waiting {
  display: flex;
  align-items: center;
  gap: 4px;
  height: 20px;
}
.bubble__waiting span {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--el-text-color-placeholder);
  animation: waiting-bounce 1.2s ease-in-out infinite;
}
.bubble__waiting span:nth-child(2) {
  animation-delay: 0.15s;
}
.bubble__waiting span:nth-child(3) {
  animation-delay: 0.3s;
}
@keyframes waiting-bounce {
  0%,
  60%,
  100% {
    opacity: 0.35;
    transform: translateY(0);
  }
  30% {
    opacity: 1;
    transform: translateY(-4px);
  }
}
@keyframes blink {
  to {
    visibility: hidden;
  }
}
.chat__composer {
  padding: 12px 14px;
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius);
  box-shadow: var(--app-shadow-sm);
}

/* 输入框本身不画边框：外层卡片已经是边界，双重描边会显脏 */
.chat__composer :deep(.el-textarea__inner) {
  border: 0;
  padding: 4px 2px;
  background: transparent;
  box-shadow: none;
  font-size: 14px;
  line-height: 1.7;
}
.chat__composer-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 8px;
}
.chat__mic-label {
  margin-left: 4px;
}
.chat__file-input {
  display: none;
}
.chat__leave-hint {
  margin: 0 0 14px;
  font-size: 13px;
  line-height: 1.7;
  color: var(--el-text-color-secondary);
}
.chat__recording {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 14px;
  border: 1px dashed var(--el-color-danger);
  border-radius: 8px;
}
.chat__recording-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--el-color-danger);
  animation: recording-pulse 1s ease-in-out infinite;
}
.chat__recording-text {
  font-size: 13px;
  color: var(--el-text-color-regular);
  font-variant-numeric: tabular-nums;
}
.chat__recording-actions {
  display: flex;
  gap: 8px;
  margin-left: auto;
}
@keyframes recording-pulse {
  50% {
    opacity: 0.25;
  }
}
.history {
  list-style: none;
  margin: 0;
  padding: 0;
}
.history__item {
  position: relative;
  padding: 10px 8px 10px 14px;
  border-radius: 8px;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.history__item:hover {
  background: var(--el-fill-color-light);
}
/* 当前会话：只用浅灰底和白底几乎分不出，加主色底 + 左侧竖条 */
.history__item--active,
.history__item--active:hover {
  background: var(--el-color-primary-light-9);
}
.history__item--active::before {
  content: '';
  position: absolute;
  top: 10px;
  bottom: 10px;
  left: 4px;
  width: 3px;
  border-radius: 2px;
  background: var(--app-primary);
}
.history__item--active .history__title {
  font-weight: 600;
  color: var(--app-primary);
}
.history__title {
  font-size: 14px;
}
.history__time {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
