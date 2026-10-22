/**
 * 语音能力 API（M5，设计文档 9.3）。
 *
 * 对话流本身（含语音消息上传）在 chat.ts：事件协议与文本消息完全一致，
 * 拆到两个文件只会让 dispatch 逻辑重复一份。
 */

import { api } from './client'
import type { SpeechSynthesisResult, VoiceCapabilities } from './types'

/**
 * 能力探测：进页面先问一次，未配置就禁用麦克风按钮，
 * 而不是让用户录完 30 秒再收到 503。
 */
export function fetchVoiceCapabilities(): Promise<VoiceCapabilities> {
  return api.get<VoiceCapabilities>('/v1/voice/capabilities')
}

/** 为指定回复合成语音（幂等：已合成过直接命中缓存，不重复计费）。 */
export function synthesizeSpeech(messageId: string): Promise<SpeechSynthesisResult> {
  return api.post<SpeechSynthesisResult>(`/v1/messages/${messageId}/tts`)
}
