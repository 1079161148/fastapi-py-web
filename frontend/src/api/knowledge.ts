/**
 * 知识库 API（设计文档 7 / 9.4）。
 *
 * 上传用原生 fetch + FormData：axios 实例默认 Content-Type 是 application/json，
 * 手动覆盖 multipart 又容易丢掉 boundary；fetch 交给浏览器处理最省心。
 */

import { ApiError, api, handleUnauthorized, tokenStorage } from './client'
import type {
  AppErrorBody,
  DocumentInfo,
  DocumentListResponse,
  KnowledgeBaseCreatePayload,
  KnowledgeBaseInfo,
  SearchResponse,
} from './types'

export function listKnowledgeBases(): Promise<KnowledgeBaseInfo[]> {
  return api.get<KnowledgeBaseInfo[]>('/v1/knowledge/bases')
}

export function createKnowledgeBase(
  payload: KnowledgeBaseCreatePayload,
): Promise<KnowledgeBaseInfo> {
  return api.post<KnowledgeBaseInfo>('/v1/knowledge/bases', payload)
}

export function listDocuments(kbId: string, limit = 100): Promise<DocumentListResponse> {
  return api.get<DocumentListResponse>('/v1/knowledge/documents', {
    params: { kb_id: kbId, limit },
  })
}

export function deleteDocument(documentId: string): Promise<void> {
  return api.delete<void>(`/v1/knowledge/documents/${documentId}`)
}

export function reprocessDocument(documentId: string): Promise<DocumentInfo> {
  return api.post<DocumentInfo>(`/v1/knowledge/documents/${documentId}/reprocess`)
}

export function searchKnowledge(
  query: string,
  kbId?: string,
  topK = 5,
): Promise<SearchResponse> {
  return api.get<SearchResponse>('/v1/knowledge/search', {
    params: { q: query, kb_id: kbId, top_k: topK },
  })
}

export async function uploadDocument(kbId: string, file: File): Promise<DocumentInfo> {
  const form = new FormData()
  form.append('kb_id', kbId)
  form.append('file', file)

  const headers: Record<string, string> = {}
  const token = tokenStorage.get()
  if (token) headers.Authorization = `Bearer ${token}`
  const tenant = tokenStorage.getTenant()
  if (tenant) headers['X-Tenant-Id'] = tenant

  const resp = await fetch('/api/v1/knowledge/documents', {
    method: 'POST',
    body: form,
    headers,
    credentials: 'same-origin',
  })
  if (!resp.ok) {
    // 原生 fetch 不走 axios 拦截器，401 要在这里补一次登录态失效处理
    if (resp.status === 401) handleUnauthorized()
    let body: AppErrorBody
    try {
      body = (await resp.json()) as AppErrorBody
    } catch {
      body = {
        code: 'UPLOAD_FAILED',
        message: `上传失败（HTTP ${resp.status}），请稍后重试`,
      }
    }
    throw new ApiError(body, resp.status)
  }
  return (await resp.json()) as DocumentInfo
}
