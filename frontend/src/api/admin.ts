// 管理后台 API（P1-C）：成员管理 + 问答日志
import http from './http'
import type { UserInfo } from '@/stores/auth'

/** 成员列表查询条件 */
export interface MemberQuery {
  orgId?: number
  keyword?: string
  limit?: number
}

/** 管理员修改成员：只填要改的那一项 */
export interface MemberPatch {
  role?: 'user' | 'admin'
  is_active?: boolean
}

/** 管理员重置密码的返回：临时密码只出现这一次 */
export interface ResetPasswordResult {
  message: string
  username: string
  temp_password: string
}

export function listMembers(params: MemberQuery = {}) {
  return http
    .get<UserInfo[]>('/users', {
      params: {
        org_id: params.orgId,
        keyword: params.keyword || undefined,
        limit: params.limit ?? 200,
      },
    })
    .then((r) => r.data)
}

export function patchMember(userId: number, patch: MemberPatch) {
  return http.patch<UserInfo>(`/users/${userId}`, patch).then((r) => r.data)
}

export function resetMemberPassword(userId: number) {
  return http.post<ResetPasswordResult>(`/users/${userId}/reset-password`).then((r) => r.data)
}

// ---------------- 问答日志 ----------------

export interface QueryLogItem {
  id: number
  user_id: number | null
  username: string | null
  session_id: number | null
  question: string
  kb_scope: number[] | null
  answered: boolean
  latency_ms: number | null
  created_at: string
  hit_count: number
}

export interface QueryLogDetail extends QueryLogItem {
  retrieval: Array<{ doc_name?: string; score?: number; text?: string }> | null
}

export interface QueryLogStats {
  total: number
  answered: number
  unanswered: number
  avg_latency_ms: number
}

export interface QueryLogQuery {
  answered?: boolean
  keyword?: string
  limit?: number
  offset?: number
}

export function listQueryLogs(params: QueryLogQuery = {}) {
  return http
    .get<QueryLogItem[]>('/query-logs', {
      params: {
        answered: params.answered,
        keyword: params.keyword || undefined,
        limit: params.limit ?? 50,
        offset: params.offset ?? 0,
      },
    })
    .then((r) => r.data)
}

export function getQueryLogStats() {
  return http.get<QueryLogStats>('/query-logs/stats').then((r) => r.data)
}

export function getQueryLogDetail(id: number) {
  return http.get<QueryLogDetail>(`/query-logs/${id}`).then((r) => r.data)
}
