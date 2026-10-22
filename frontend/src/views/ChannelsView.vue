<script setup lang="ts">
/**
 * 渠道接入（M4，设计文档 9.1）。
 *
 * 这个页面管的是"接入状态"而不是"填凭证"，原因在后端 channels/factory.py：
 * 敏感凭证（AppSecret、邮箱密码）只从环境变量读，channel_configs.credentials
 * 只存非敏感展示信息。库/env 各存一份会导致"用 A 的 app_id 配 B 的 secret"
 * 这种静默失败，所以这里不给假输入框——缺失项直接列出环境变量名。
 *
 * 页面可编辑：启用开关、平台账号标识、限流、回调验签密钥。
 * 门禁在后端 require_tenant_admin（owner/admin），导航里对其他角色隐藏。
 */
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'

import { listChannels, updateChannel } from '@/api/channels'
import type { ChannelConfigInfo, ChannelConfigUpdatePayload } from '@/api/types'
import { useAuthStore } from '@/stores/auth'

interface ChannelDraft {
  enabled: boolean
  external_account_id: string
  rate_limit: number
  /** 回调验签密钥：后端不回显，留空表示保持不变 */
  webhook_secret: string
}

const auth = useAuthStore()

const channels = ref<ChannelConfigInfo[]>([])
const drafts = ref<Record<string, ChannelDraft>>({})
const loading = ref(false)
const saving = ref<Record<string, boolean>>({})

const manageable = computed(() => ['owner', 'admin'].includes(auth.user?.tenant_role ?? ''))

const CHANNEL_META: Record<string, { name: string; desc: string; account: string }> = {
  web: {
    name: '网页',
    desc: '内置渠道，站点内的在线客服直接使用，无需外部凭证',
    account: '站点标识',
  },
  email: {
    name: '邮件',
    desc: 'IMAP 轮询收信，SMTP 回信；需要 SMTP_HOST / SMTP_USERNAME / SMTP_PASSWORD',
    account: '收件邮箱',
  },
  wechat: {
    name: '微信公众号',
    desc: '需要 WECHAT_APP_ID / WECHAT_APP_SECRET / WECHAT_TOKEN',
    account: '原始 ID（gh_xxx）',
  },
  dingtalk: {
    name: '钉钉机器人',
    desc: '需要 DINGTALK_APP_KEY / DINGTALK_APP_SECRET',
    account: 'robotCode',
  },
}

function webhookUrl(path: string): string {
  return `${window.location.origin}${path}`
}

async function loadChannels(): Promise<void> {
  loading.value = true
  try {
    channels.value = await listChannels()
    for (const item of channels.value) {
      drafts.value[item.channel] = {
        enabled: item.enabled,
        external_account_id: item.external_account_id,
        rate_limit: item.rate_limit,
        webhook_secret: '',
      }
    }
  } catch {
    // 拦截器已提示
  } finally {
    loading.value = false
  }
}

async function onSave(channel: string): Promise<void> {
  const draft = drafts.value[channel]
  if (!draft) return
  saving.value = { ...saving.value, [channel]: true }
  try {
    const payload: ChannelConfigUpdatePayload = {
      enabled: draft.enabled,
      external_account_id: draft.external_account_id,
      rate_limit: draft.rate_limit,
    }
    // 留空表示不改：后端把 undefined 当作"保持原值"
    if (draft.webhook_secret) payload.webhook_secret = draft.webhook_secret

    const updated = await updateChannel(channel, payload)
    channels.value = channels.value.map((item) => (item.channel === channel ? updated : item))
    drafts.value[channel] = {
      enabled: updated.enabled,
      external_account_id: updated.external_account_id,
      rate_limit: updated.rate_limit,
      webhook_secret: '',
    }
    ElMessage.success('已保存')
  } catch {
    // 同上
  } finally {
    saving.value = { ...saving.value, [channel]: false }
  }
}

async function onCopy(path: string): Promise<void> {
  const url = webhookUrl(path)
  try {
    await navigator.clipboard.writeText(url)
    ElMessage.success('回调地址已复制')
  } catch {
    // 非 HTTPS 或权限被拒：退回手动选择提示，不静默失败
    ElMessage.warning(`复制失败，请手动复制：${url}`)
  }
}

function statusOf(item: ChannelConfigInfo): { label: string; type: 'success' | 'warning' | 'info' } {
  if (!item.enabled) return { label: '已停用', type: 'info' }
  if (item.channel === 'web') return { label: '内置可用', type: 'success' }
  return item.configured
    ? { label: '已就绪', type: 'success' }
    : { label: '缺凭证', type: 'warning' }
}

onMounted(loadChannels)
</script>

<template>
  <div class="channels">
    <header class="channels__head">
      <div>
        <h2 class="channels__title">渠道接入</h2>
        <p class="channels__hint">
          敏感凭证（AppSecret、邮箱密码）只从环境变量读取，本页不保存它们——库里和环境里各存一份，
          很容易配成"用 A 的 app_id 配 B 的 secret"，出错时没有任何报错。
        </p>
      </div>
    </header>

    <el-alert
      v-if="!manageable"
      type="info"
      :closable="false"
      show-icon
      title="只有租户 owner / admin 可以修改渠道配置，当前账号为只读"
    />

    <div v-loading="loading" class="channels__grid">
      <el-card v-for="item in channels" :key="item.channel" shadow="never" class="channel">
        <template #header>
          <div class="channel__head">
            <span class="channel__name">{{ CHANNEL_META[item.channel]?.name ?? item.channel }}</span>
            <el-tag size="small" :type="statusOf(item).type">{{ statusOf(item).label }}</el-tag>
          </div>
        </template>

        <p class="channel__desc">{{ CHANNEL_META[item.channel]?.desc }}</p>

        <div v-if="item.missing.length" class="channel__missing">
          <span class="channel__missing-label">缺少环境变量：</span>
          <code v-for="name in item.missing" :key="name" class="channel__missing-item">{{ name }}</code>
        </div>

        <div class="channel__row">
          <span class="channel__label">回调地址</span>
          <div class="channel__webhook">
            <code class="channel__webhook-url">{{ webhookUrl(item.webhook_path) }}</code>
            <el-button link size="small" @click="onCopy(item.webhook_path)">复制</el-button>
          </div>
        </div>

        <el-form label-width="110px" size="small" class="channel__form">
          <el-form-item label="启用">
            <el-switch v-model="drafts[item.channel].enabled" :disabled="!manageable" />
          </el-form-item>
          <el-form-item :label="CHANNEL_META[item.channel]?.account ?? '账号标识'">
            <el-input
              v-model="drafts[item.channel].external_account_id"
              :disabled="!manageable"
              placeholder="平台侧账号标识，用于入站反查租户"
            />
          </el-form-item>
          <el-form-item label="限流">
            <el-input-number
              v-model="drafts[item.channel].rate_limit"
              :min="1"
              :max="6000"
              :disabled="!manageable"
              controls-position="right"
            />
            <span class="channel__unit">次 / 分钟</span>
          </el-form-item>
          <el-form-item label="回调密钥">
            <el-input
              v-model="drafts[item.channel].webhook_secret"
              :disabled="!manageable"
              placeholder="留空表示不修改（后端不回显已保存的值）"
              show-password
            />
          </el-form-item>
        </el-form>

        <div class="channel__actions">
          <el-button
            type="primary"
            size="small"
            :disabled="!manageable"
            :loading="saving[item.channel]"
            @click="onSave(item.channel)"
          >
            保存
          </el-button>
        </div>
      </el-card>
    </div>
  </div>
</template>

<style scoped>
.channels {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.channels__title {
  margin: 0 0 6px;
  font-size: 18px;
}
.channels__hint {
  margin: 0;
  max-width: 860px;
  font-size: 13px;
  line-height: 1.7;
  color: var(--el-text-color-secondary);
}
.channels__grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
  gap: 16px;
  min-height: 120px;
}
.channel__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.channel__name {
  font-size: 15px;
  font-weight: 600;
}
.channel__desc {
  margin: 0 0 12px;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}
.channel__missing {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  padding: 8px 10px;
  margin-bottom: 12px;
  border-radius: 6px;
  background: var(--el-color-warning-light-9);
}
.channel__missing-label {
  font-size: 12px;
  color: var(--el-color-warning);
}
.channel__missing-item {
  padding: 1px 6px;
  border-radius: 4px;
  background: rgba(0, 0, 0, 0.06);
  font-size: 11.5px;
}
.channel__row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}
.channel__label {
  flex: 0 0 96px;
  font-size: 12.5px;
  color: var(--el-text-color-regular);
}
.channel__webhook {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.channel__webhook-url {
  font-size: 11.5px;
  color: var(--el-text-color-regular);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.channel__form {
  margin-top: 4px;
}
.channel__unit {
  margin-left: 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.channel__actions {
  display: flex;
  justify-content: flex-end;
}
</style>
