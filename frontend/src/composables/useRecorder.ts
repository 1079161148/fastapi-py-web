/**
 * 录音（M5，设计文档 9.3）：MediaRecorder 封装。
 *
 * 设计取舍：
 * - 只做采集与计时，不转码：转码要 wasm/ffmpeg，成本远高于收益，
 *   服务端与 ASR 网关能直接吃 webm/ogg/mp4
 * - 时长由这里计时上报：ogg/webm 的容器时长需要解析索引，
 *   服务端只对 wav 做精确校验（见 backend/app/speech/audio.py）
 * - 到达上限自动停止并照常返回结果，不丢用户已说的话
 *
 * 生命周期：组件卸载时自动释放麦克风，避免"录音中离开页面"后指示灯常亮。
 */

import { onBeforeUnmount, ref } from 'vue'

export interface RecordingResult {
  blob: Blob
  /** 秒，保留一位小数 */
  duration: number
  mimeType: string
}

// 按兼容性从好到差排列：Chrome/Firefox 走 opus，Safari 回落到 mp4
const MIME_CANDIDATES = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/ogg;codecs=opus',
  'audio/mp4',
]

const TIMER_INTERVAL_MS = 100

export function useRecorder(maxSeconds: () => number) {
  const supported =
    typeof navigator !== 'undefined' &&
    typeof MediaRecorder !== 'undefined' &&
    typeof navigator.mediaDevices?.getUserMedia === 'function'

  const recording = ref(false)
  const elapsed = ref(0)
  const errorMessage = ref('')

  let recorder: MediaRecorder | null = null
  let stream: MediaStream | null = null
  let chunks: Blob[] = []
  let startedAt = 0
  let timer: number | null = null
  let discarded = false
  let settle: ((result: RecordingResult | null) => void) | null = null

  function pickMimeType(): string {
    for (const candidate of MIME_CANDIDATES) {
      if (MediaRecorder.isTypeSupported(candidate)) return candidate
    }
    return ''
  }

  function releaseStream(): void {
    stream?.getTracks().forEach((track) => track.stop())
    stream = null
  }

  function clearTimer(): void {
    if (timer !== null) {
      window.clearInterval(timer)
      timer = null
    }
  }

  function finish(result: RecordingResult | null): void {
    clearTimer()
    releaseStream()
    recording.value = false
    const callback = settle
    settle = null
    callback?.(result)
  }

  async function start(): Promise<boolean> {
    if (!supported || recording.value) return false
    errorMessage.value = ''
    chunks = []
    discarded = false

    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    } catch {
      // 权限被拒 / 无设备：必须说清原因，用户才知道去改浏览器设置
      errorMessage.value = '无法访问麦克风，请检查浏览器权限或改用文字输入'
      return false
    }

    const mimeType = pickMimeType()
    try {
      recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream)
    } catch {
      errorMessage.value = '当前浏览器不支持录音，请改用文字输入'
      releaseStream()
      return false
    }

    recorder.ondataavailable = (event) => {
      if (event.data.size > 0) chunks.push(event.data)
    }
    recorder.onstop = () => {
      const blob = new Blob(chunks, { type: recorder?.mimeType || mimeType || 'audio/webm' })
      chunks = []
      recorder = null
      const duration = Math.round(((performance.now() - startedAt) / 1000) * 10) / 10
      finish(discarded || blob.size === 0 ? null : { blob, duration, mimeType: blob.type })
    }

    startedAt = performance.now()
    elapsed.value = 0
    // 分片采集：长录音不会在内存里攒成一整块（也便于将来边录边传）
    recorder.start(500)
    recording.value = true

    timer = window.setInterval(() => {
      elapsed.value = Math.round(((performance.now() - startedAt) / 1000) * 10) / 10
      if (elapsed.value >= maxSeconds()) {
        // 到上限自动停：保留已录内容，前端提示可发送或重录
        void stop()
      }
    }, TIMER_INTERVAL_MS)

    return true
  }

  async function stop(): Promise<RecordingResult | null> {
    if (!recorder || !recording.value) return null
    if (recorder.state === 'inactive') return null
    return new Promise<RecordingResult | null>((resolve) => {
      settle = resolve
      recorder?.stop()
    })
  }

  /** 取消录制：丢弃数据，不产生"空语音"消息。 */
  function cancel(): void {
    if (!recorder) return
    discarded = true
    if (recorder.state !== 'inactive') recorder.stop()
  }

  onBeforeUnmount(() => {
    discarded = true
    if (recorder?.state === 'recording') recorder.stop()
    clearTimer()
    releaseStream()
  })

  return { supported, recording, elapsed, errorMessage, start, stop, cancel }
}
