// API Key 管理：给"程序对程序"的对接发一把长期钥匙。
//
// 大白话：企业要把它接进自家的客服系统/办公系统，程序没法像人一样登录，
// 所以发一把可随时吊销的长期钥匙（API Key）。这把钥匙只能开自己家的门。
import http from './http'

/** 列表项 —— 注意**不含明文**（明文只在创建那一刻返回一次） */
export interface ApiKeyItem {
  id: number
  org_id: number
  name: string
  key_prefix: string
  revoked: boolean
  created_by: number | null
  expires_at: string | null
  last_used_at: string | null
  use_count: number
  created_at: string
}

/** 创建时的返回：比列表项多一个**明文 key**（只出现这一次，丢了只能重建） */
export interface ApiKeyCreated extends ApiKeyItem {
  key: string
  message: string
}

/**
 * 创建一个 API Key。
 *
 * @param name        给这把钥匙起个名（如"客服系统正式环境"），便于日后认出来是谁在用
 * @param expiresDays 多少天后自动过期；不填=永不过期（建议填，方便定期换钥匙）
 * @param orgId       平台超管给某家客户开时才填；组织管理员不填（自动是自己的组织）
 */
export function createApiKey(body: { name?: string; expires_days?: number | null; org_id?: number }) {
  return http.post<ApiKeyCreated>('/api-keys', body).then((r) => r.data)
}

/** 列出某组织的 API Key（不含明文）。orgId 只有平台超管需要填。 */
export function listApiKeys(orgId?: number) {
  return http
    .get<ApiKeyItem[]>('/api-keys', { params: orgId ? { org_id: orgId } : {} })
    .then((r) => r.data)
}

/** 吊销（立即失效，但记录保留，便于事后追究"这把钥匙是谁开的、用过多少次"） */
export function revokeApiKey(keyId: number) {
  return http.delete(`/api-keys/${keyId}`).then((r) => r.data)
}
