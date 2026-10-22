/**
 * 质检抽检与敏感词命中 API（M9）。
 *
 * 抽样返回的是"新建了几条"而不是列表：同一会话只抽检一次，
 * 实际新建数可能小于请求的 limit（可抽的会话不够）。
 */

import { api } from './client'
import type { QualityOverview, QualityReviewInfo, SensitiveHitInfo } from './types'

export function getQualityOverview(): Promise<QualityOverview> {
  return api.get<QualityOverview>('/v1/quality/overview')
}

export function sampleQualityReviews(limit = 10): Promise<number> {
  return api.post<number>('/v1/quality/reviews/sample', undefined, { params: { limit } })
}

export function listQualityReviews(params: {
  status?: string
  mine?: boolean
  limit?: number
} = {}): Promise<QualityReviewInfo[]> {
  return api.get<QualityReviewInfo[]>('/v1/quality/reviews', { params })
}

export function submitQualityReview(
  reviewId: string,
  payload: { score: number; tags: string[]; comment: string },
): Promise<QualityReviewInfo> {
  return api.post<QualityReviewInfo>(`/v1/quality/reviews/${reviewId}`, payload)
}

export function listSensitiveHits(limit = 50): Promise<SensitiveHitInfo[]> {
  return api.get<SensitiveHitInfo[]>('/v1/quality/sensitive-hits', { params: { limit } })
}
