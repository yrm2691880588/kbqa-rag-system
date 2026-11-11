// 额度与用量 API：查自己组织的用量、超管设置额度
import http from './http'

/** 一个时间口径（今天 / 本月）的用量与上限 */
export interface UsageBucket {
  questions: number
  tokens_in: number
  tokens_out: number
  embedding_chunks: number
  question_limit: number | null
  token_limit: number | null
}

export interface DailyBucket extends UsageBucket {
  day: string
}

export interface UsageSummary {
  org_id: number
  is_service_enabled: boolean
  daily: DailyBucket
  monthly: UsageBucket
}

/** 额度配置（null = 不限） */
export interface QuotaConfig {
  org_id: number
  daily_question_limit: number | null
  monthly_question_limit: number | null
  daily_token_limit: number | null
  monthly_token_limit: number | null
  is_service_enabled: boolean
  note: string
  updated_at?: string | null
}

/** 查看自己组织的用量 */
export function getMyUsage() {
  return http.get<UsageSummary>('/quotas/me').then((r) => r.data)
}

/** 查看指定组织的用量（平台超管） */
export function getOrgUsage(orgId: number) {
  return http.get<UsageSummary>(`/quotas/${orgId}`).then((r) => r.data)
}

/** 查看指定组织的额度配置 */
export function getOrgQuota(orgId: number) {
  return http.get<QuotaConfig>(`/quotas/${orgId}/config`).then((r) => r.data)
}

/**
 * 设置额度（仅平台超管）。
 *
 * 注意：**没传的字段保持原样，显式传 null 才是"改成不限"**。
 * 所以调用时只放真正要改的字段进去，别把整个对象铺开传——
 * 否则会把没打算动的额度顺手清成"不限"。
 */
export function updateOrgQuota(orgId: number, patch: Partial<Omit<QuotaConfig, 'org_id'>>) {
  return http.put<QuotaConfig>(`/quotas/${orgId}`, patch).then((r) => r.data)
}
