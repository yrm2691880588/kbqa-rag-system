// 组织（客户公司）相关的后端接口封装
//
// 背景（大白话）：系统现在同时服务多家客户，每家客户 = 一个「组织」。
// 各家的知识库、账号互相看不见。这里封装的是"组织"这一层的管理接口。
// 权限：只有平台超管（你自己，系统运营方）能建组织；组织管理员能看自己组织的成员和邀请码。
import http from './http'

/** 组织信息（注意：列表接口里**不含邀请码**，这是刻意的） */
export interface OrgItem {
  id: number
  name: string
  description: string | null
  is_active: boolean
  created_at: string
  member_count: number
  kb_count: number
  // ---- 用量与额度（P1-B）----
  monthly_questions: number                          // 本月已问多少次
  monthly_question_limit: number | null              // 本月上限（null=不限）
  question_limit_per_day: number | null              // 每日上限（null=不限）
  is_service_enabled: boolean                        // 服务是否在用（false=暂停）
}

/** 新建组织后返回的内容：**只有这一次**带邀请码，请立刻复制保存 */
export interface OrgCreated {
  id: number
  name: string
  description: string | null
  is_active: boolean
  created_at: string
  invite_code: string
}

/** 邀请码（重置后 / 主动查看自己组织时返回） */
export interface InviteCode {
  org_id: number
  invite_code: string
  message?: string
}

/** 组织成员（= 后端的 UserOut，绝不包含密码） */
export interface OrgMember {
  id: number
  username: string
  nickname: string
  role: string
  is_active: boolean
  phone?: string | null
  org_id?: number | null
  is_superadmin: boolean
  created_at: string
  last_login_at?: string | null
}

/** 查看自己所属的组织（任何登录用户都可用） */
export function getMyOrg() {
  return http.get<OrgItem>('/organizations/me').then((r) => r.data)
}

/** 查看自己组织的邀请码（平台超管或本组织管理员） */
export function getMyInviteCode() {
  return http.get<InviteCode>('/organizations/me/invite-code').then((r) => r.data)
}

/** 列出全部组织（平台超管） */
export function listOrgs() {
  return http.get<OrgItem[]>('/organizations').then((r) => r.data)
}

/** 新建组织（平台超管）。返回体里的邀请码只出现这一次。 */
export function createOrg(name: string, description?: string) {
  return http.post<OrgCreated>('/organizations', { name, description }).then((r) => r.data)
}

/** 修改组织：改名 / 改备注 / 启停（平台超管） */
export function updateOrg(
  orgId: number,
  payload: { name?: string; description?: string; is_active?: boolean },
) {
  return http.put<OrgItem>(`/organizations/${orgId}`, payload).then((r) => r.data)
}

/** 重置组织邀请码：旧码立即失效（平台超管） */
export function rotateInvite(orgId: number) {
  return http.post<InviteCode>(`/organizations/${orgId}/rotate-invite`).then((r) => r.data)
}

/** 查看组织成员：超管可看任意组织，组织管理员只能看自己组织 */
export function listMembers(orgId: number) {
  return http.get<OrgMember[]>(`/organizations/${orgId}/members`).then((r) => r.data)
}
