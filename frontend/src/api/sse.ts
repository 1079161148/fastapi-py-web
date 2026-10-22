/**
 * SSE 传输层（设计文档 3.2 / 7）。
 *
 * 为什么不用 EventSource：它只支持 GET、无法自定义 Authorization 头，
 * 而对话是 POST + Bearer。这里只负责"把字节流切成帧"，
 * 事件语义由调用方（chat.ts）解释——语音消息与文本消息共用同一套解析。
 */

import type { AppErrorBody } from './types'

export interface ParsedFrame {
  event: string
  data: string
}

export function parseFrame(frame: string): ParsedFrame | null {
  let event = 'message'
  const dataLines: string[] = []
  for (const line of frame.split('\n')) {
    if (line.startsWith('event:')) {
      event = line.slice('event:'.length).trim()
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice('data:'.length).trimStart())
    }
  }
  if (dataLines.length === 0) return null
  return { event, data: dataLines.join('\n') }
}

/**
 * 读取整条 SSE 流并逐帧回调。
 *
 * 帧以空行分隔，但网络分片边界与帧边界无关，必须保留半帧到下一次 read。
 * 单帧 JSON 解析失败直接跳过：一条坏帧不该中断整轮回答（服务端同样容错）。
 */
export async function readSseStream(
  resp: Response,
  onFrame: (event: string, payload: unknown) => void,
): Promise<void> {
  if (!resp.body) {
    onFrame('error', {
      code: 'STREAM_UNAVAILABLE',
      message: '当前浏览器不支持流式响应',
    } satisfies AppErrorBody)
    return
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  const dispatch = (block: string): void => {
    const parsed = parseFrame(block)
    if (!parsed) return
    let payload: unknown
    try {
      payload = JSON.parse(parsed.data)
    } catch {
      return
    }
    onFrame(parsed.event, payload)
  }

  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    let boundary = buffer.indexOf('\n\n')
    while (boundary >= 0) {
      dispatch(buffer.slice(0, boundary))
      buffer = buffer.slice(boundary + 2)
      boundary = buffer.indexOf('\n\n')
    }
  }

  buffer += decoder.decode()
  if (buffer.trim()) dispatch(buffer)
}

/** 非 2xx 响应的错误体；非 JSON（网关错误页）落到统一兜底。 */
export async function readErrorBody(resp: Response): Promise<AppErrorBody> {
  try {
    const body = (await resp.json()) as AppErrorBody
    if (body.code) return body
  } catch {
    // 忽略：下面给统一文案
  }
  return {
    code: 'UNKNOWN_ERROR',
    message: `请求失败（HTTP ${resp.status}），请稍后重试`,
  }
}
