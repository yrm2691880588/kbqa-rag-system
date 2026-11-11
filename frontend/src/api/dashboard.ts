// 仪表盘 API：一次拿到所有总览数据
import http from './http'

export interface TrendPoint {
  date: string
  count: number
}

export interface RecentActivity {
  question: string
  answered: boolean
  created_at: string | null
  username: string
}

export interface DashboardData {
  user_count: number
  kb_count: number
  doc_count: number
  processing_docs: number
  failed_docs: number
  today_questions: number
  period_questions: number
  /** null 表示"还没有数据可算"，与 0（全没答上）不同 */
  answer_rate: number | null
  feedback_total: number
  feedback_likes: number
  feedback_dislikes: number
  satisfaction_rate: number | null
  trend: TrendPoint[]
  recent: RecentActivity[]
}

export function getDashboard(days = 7) {
  return http.get<DashboardData>('/dashboard', { params: { days } }).then((r) => r.data)
}
