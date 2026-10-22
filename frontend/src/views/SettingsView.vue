<script setup lang="ts">
/**
 * 系统设置（M8）：租户级运营配置。
 *
 * 只暴露"运营要天天调"的项：欢迎语、转人工触发词、工作时间与
 * 非工作时间策略、AI 兜底回复。模型密钥等部署配置仍走 .env——
 * 在后台网页里改密钥等于把密钥暴露给所有能登录的人。
 */
import { ElMessage } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'

import { updateTenantSettings } from '@/api/support'
import type { TenantSettings } from '@/api/types'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const saving = ref(false)
const triggerInput = ref('')
const wordInput = ref('')

const WEEKDAY_LABELS = ['', '周一', '周二', '周三', '周四', '周五', '周六', '周日']

const form = reactive<TenantSettings>({
  welcome_message: '',
  handoff_triggers: [],
  after_hours_action: 'leave_message',
  work_hours_start: '09:00',
  work_hours_end: '18:00',
  work_days: [1, 2, 3, 4, 5],
  fallback_reply: '',
  sensitive_words: [],
  sensitive_action: 'warn',
  quality_sample_ratio: 0,
})

/** 从当前登录态拿配置：auth.tenant 是进页面时 /tenants/current 的结果 */
function hydrate(): void {
  const settings = auth.tenant?.settings
  if (!settings) return
  Object.assign(form, {
    welcome_message: settings.welcome_message ?? '',
    handoff_triggers: [...(settings.handoff_triggers ?? [])],
    after_hours_action: settings.after_hours_action ?? 'leave_message',
    work_hours_start: settings.work_hours_start ?? '09:00',
    work_hours_end: settings.work_hours_end ?? '18:00',
    work_days: [...(settings.work_days ?? [1, 2, 3, 4, 5])],
    fallback_reply: settings.fallback_reply ?? '',
    sensitive_words: [...(settings.sensitive_words ?? [])],
    sensitive_action: settings.sensitive_action ?? 'warn',
    quality_sample_ratio: settings.quality_sample_ratio ?? 0,
  })
}

function addWord(): void {
  const word = wordInput.value.trim()
  if (!word) return
  if (form.sensitive_words.includes(word)) {
    ElMessage.warning('该词已在词库中')
    return
  }
  if (form.sensitive_words.length >= 200) {
    ElMessage.warning('最多 200 个敏感词')
    return
  }
  form.sensitive_words.push(word)
  wordInput.value = ''
}

function removeWord(word: string): void {
  form.sensitive_words = form.sensitive_words.filter((item) => item !== word)
}

function addTrigger(): void {
  const word = triggerInput.value.trim()
  if (!word) return
  if (form.handoff_triggers.includes(word)) {
    ElMessage.warning('该触发词已存在')
    return
  }
  if (form.handoff_triggers.length >= 20) {
    ElMessage.warning('最多 20 个触发词')
    return
  }
  form.handoff_triggers.push(word)
  triggerInput.value = ''
}

function removeTrigger(word: string): void {
  form.handoff_triggers = form.handoff_triggers.filter((item) => item !== word)
}

function toggleDay(day: number): void {
  form.work_days = form.work_days.includes(day)
    ? form.work_days.filter((item) => item !== day)
    : [...form.work_days, day].sort()
}

async function onSave(): Promise<void> {
  if (form.work_days.length === 0) {
    ElMessage.warning('至少选择一个工作日')
    return
  }
  if (form.work_hours_start >= form.work_hours_end) {
    ElMessage.warning('工作开始时间必须早于结束时间')
    return
  }
  saving.value = true
  try {
    await updateTenantSettings({ ...form, handoff_triggers: [...form.handoff_triggers] })
    // 本地状态同步：否则欢迎语等配置刷新页面后才生效，用户会以为没保存
    if (auth.tenant) auth.tenant.settings = { ...form }
    ElMessage.success('设置已保存')
  } catch {
    // 拦截器已提示
  } finally {
    saving.value = false
  }
}

onMounted(hydrate)
</script>

<template>
  <div class="settings">
    <header class="settings__header">
      <div>
        <h2 class="settings__title">系统设置</h2>
        <p class="settings__hint">
          调整 AI 客服的接待行为。保存后立即对新会话生效，进行中的对话不受影响。
        </p>
      </div>
      <el-button type="primary" :loading="saving" @click="onSave">保存设置</el-button>
    </header>

    <el-card shadow="never" class="settings__card">
      <h3 class="settings__section">接待语</h3>
      <el-input
        v-model="form.welcome_message"
        type="textarea"
        :rows="2"
        maxlength="500"
        show-word-limit
        placeholder="例如：你好，我是智能客服，可以问我售后、发票、物流相关问题"
      />
      <p class="settings__note">
        访客打开会话时展示。留空则不展示（当前由前端本地渲染，不进入对话上下文）。
      </p>
    </el-card>

    <el-card shadow="never" class="settings__card">
      <h3 class="settings__section">转人工触发词</h3>
      <p class="settings__note">
        用户消息中包含任意一个词时自动转人工（不再由 AI 回答）。适合"人工""投诉"这类明确诉求。
      </p>
      <div class="settings__triggers">
        <el-tag
          v-for="word in form.handoff_triggers"
          :key="word"
          closable
          class="settings__trigger"
          @close="removeTrigger(word)"
        >
          {{ word }}
        </el-tag>
      </div>
      <div class="settings__trigger-input">
        <el-input
          v-model="triggerInput"
          placeholder="输入触发词后回车添加"
          maxlength="20"
          style="max-width: 280px"
          @keyup.enter="addTrigger"
        />
        <el-button @click="addTrigger">添加</el-button>
      </div>
    </el-card>

    <el-card shadow="never" class="settings__card">
      <h3 class="settings__section">工作时间</h3>
      <p class="settings__note">
        按 UTC 时间判断（部署在单时区环境）。非工作时间的行为见下一项。
      </p>
      <div class="settings__workhours">
        <el-time-picker
          v-model="form.work_hours_start"
          format="HH:mm"
          value-format="HH:mm"
          placeholder="开始时间"
        />
        <span class="settings__workhours-sep">至</span>
        <el-time-picker
          v-model="form.work_hours_end"
          format="HH:mm"
          value-format="HH:mm"
          placeholder="结束时间"
        />
      </div>
      <div class="settings__days">
        <el-button
          v-for="day in [1, 2, 3, 4, 5, 6, 7]"
          :key="day"
          size="small"
          :type="form.work_days.includes(day) ? 'primary' : 'default'"
          @click="toggleDay(day)"
        >
          {{ WEEKDAY_LABELS[day] }}
        </el-button>
      </div>
      <div class="settings__after-hours">
        <span class="settings__after-hours-label">非工作时间策略</span>
        <el-radio-group v-model="form.after_hours_action">
          <el-radio value="leave_message">引导留言（推荐）</el-radio>
          <el-radio value="continue_ai">继续由 AI 回答</el-radio>
        </el-radio-group>
      </div>
    </el-card>

    <el-card shadow="never" class="settings__card">
      <h3 class="settings__section">AI 兜底回复</h3>
      <p class="settings__note">
        AI 模型未配置或不可用时返回给用户的固定文案。留空则明确报错（便于排查）。
      </p>
      <el-input
        v-model="form.fallback_reply"
        type="textarea"
        :rows="2"
        maxlength="500"
        show-word-limit
        placeholder="例如：我这边临时开小差了，请留下联系方式，客服会尽快联系你"
      />
    </el-card>

    <el-card shadow="never" class="settings__card">
      <h3 class="settings__section">敏感词过滤</h3>
      <p class="settings__note">
        命中后按下方的策略处置，所有命中都会留痕（质检页可查）。
        词库只在服务端匹配，不下发到浏览器。
      </p>
      <div class="settings__triggers">
        <el-tag
          v-for="word in form.sensitive_words"
          :key="word"
          closable
          type="danger"
          effect="plain"
          class="settings__trigger"
          @close="removeWord(word)"
        >
          {{ word }}
        </el-tag>
      </div>
      <div class="settings__trigger-input">
        <el-input
          v-model="wordInput"
          placeholder="输入敏感词后回车添加"
          maxlength="50"
          style="max-width: 280px"
          @keyup.enter="addWord"
        />
        <el-button @click="addWord">添加</el-button>
      </div>
      <div class="settings__after-hours">
        <span class="settings__after-hours-label">命中处置</span>
        <el-radio-group v-model="form.sensitive_action">
          <el-radio value="warn">仅记录（推荐）</el-radio>
          <el-radio value="handoff">转人工</el-radio>
          <el-radio value="block">拒绝本次输入</el-radio>
        </el-radio-group>
      </div>
      <p class="settings__note">
        「拒绝」不会把用户输入存进会话历史；「转人工」会建工单并继续由 AI 作答。
      </p>
    </el-card>

    <el-card shadow="never" class="settings__card">
      <h3 class="settings__section">质检抽检比例</h3>
      <p class="settings__note">
        新建会话按该比例随机进入质检队列（0 = 不自动抽样，只在质检页手动抽样）。
        抽样失败不会影响会话创建。
      </p>
      <el-slider
        v-model="form.quality_sample_ratio"
        :min="0"
        :max="1"
        :step="0.05"
        :format-tooltip="(value: number) => `${Math.round(value * 100)}%`"
        style="max-width: 320px"
      />
    </el-card>
  </div>
</template>

<style scoped>
.settings__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.settings__title {
  margin: 0 0 4px;
  font-size: 16px;
  font-weight: 650;
}
.settings__hint {
  margin: 0;
  font-size: 12.5px;
  color: var(--el-text-color-secondary);
}
.settings__card {
  margin-bottom: 12px;
}
.settings__section {
  margin: 0 0 10px;
  font-size: 14px;
  font-weight: 650;
}
.settings__note {
  margin: 0 0 10px;
  font-size: 12.5px;
  color: var(--el-text-color-secondary);
  line-height: 1.7;
}
.settings__triggers {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 10px;
}
.settings__trigger-input {
  display: flex;
  gap: 8px;
}
.settings__workhours {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}
.settings__workhours-sep {
  color: var(--el-text-color-secondary);
}
.settings__days {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 14px;
}
.settings__after-hours {
  display: flex;
  align-items: center;
  gap: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--app-border);
}
.settings__after-hours-label {
  font-size: 13px;
  color: var(--el-text-color-regular);
}
</style>
