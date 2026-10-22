/**
 * AI 效果洞察 API：命中率 + 未命中问题看板。
 *
 * 与数据报表分开：报表回答"服务怎么样"，这里回答"AI 哪里不行、
 * 该补什么知识"——调用方是知识库运营，不是管理者。
 */

import { api } from './client'
import type { InsightOverview } from './types'

export function getInsightOverview(limit = 50): Promise<InsightOverview> {
  return api.get<InsightOverview>('/v1/insights/overview', { params: { limit } })
}
