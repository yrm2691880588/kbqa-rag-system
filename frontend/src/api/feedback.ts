// 反馈管理 API（管理员）：列表 / 统计 / 详情
import http from './http'

/** 一条反馈（含被评价的那轮问答） */
export interface FeedbackItem {
  message_id: number
  session_id: number
  /** 1=满意 / -1=不满意 */
  feedback: number | null
  /** 原因 key（如 inaccurate），跟后端 FEEDBACK_REASONS 对应 */
  reason: string | null
  /** 原因的中文标签，后端已经翻译好 */
  reason_label: string | null
  note: string | null
  feedback_at: string | null
  question: string
  answer: string
  username: string
  citations: Array<{
    index: number
    doc_name: string
    page: number | null
    text: string
    score: number
    kb_id: number
  }> | null
  created_at: string | null
}

/** 反馈总览 */
export interface FeedbackStats {
  total: number
  likes: number
  dislikes: number
  /** null 表示"还没有任何评价"，与 0（有评价但全是踩）不同 */
  satisfaction_rate: number | null
  /** [{reason, label, count}] 按 count 倒序 */
  reason_counts: Array<{ reason: string; label: string; count: number }>
}

export function listFeedbacks(params: {
  feedback?: number
  reason?: string
  limit?: number
  offset?: number
} = {}) {
  return http
    .get<FeedbackItem[]>('/feedbacks', {
      params: {
        feedback: params.feedback,
        reason: params.reason,
        limit: params.limit ?? 50,
        offset: params.offset ?? 0,
      },
    })
    .then((r) => r.data)
}

export function getFeedbackStats() {
  return http.get<FeedbackStats>('/feedbacks/stats').then((r) => r.data)
}

export function getFeedbackDetail(messageId: number) {
  return http.get<FeedbackItem>(`/feedbacks/${messageId}`).then((r) => r.data)
}
