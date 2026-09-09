// HTTP 客户端：统一加令牌、统一处理 401 自动续期、统一报错提示
import axios, { AxiosError, type InternalAxiosRequestConfig } from 'axios'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'

// 创建 axios 实例：所有请求走 /api 前缀（Vite 已配置转发到后端）
const http = axios.create({
  baseURL: '/api/v1',
  timeout: 60000, // 长请求（如上传/问答）60 秒超时
})

// 请求拦截：有令牌就自动带上 Authorization 头
http.interceptors.request.use((config) => {
  const auth = useAuthStore()
  if (auth.tokens?.access_token) {
    config.headers.Authorization = `Bearer ${auth.tokens.access_token}`
  }
  return config
})

// 是否正在刷新令牌（防止多个 401 同时触发多次刷新）
let refreshing = false
// 等待刷新期间排队的请求
let pendingQueue: Array<() => void> = []

function flushQueue() {
  pendingQueue.forEach((cb) => cb())
  pendingQueue = []
}

// 响应拦截：401 时尝试用 refresh 换新令牌后重放原请求
http.interceptors.response.use(
  (resp) => resp,
  async (error: AxiosError) => {
    const auth = useAuthStore()
    const original = error.config as (InternalAxiosRequestConfig & { _retried?: boolean }) | undefined

    // 只有"访问令牌过期"(401) 且该请求还没重试过时，才尝试续期
    if (error.response?.status === 401 && original && !original._retried && auth.tokens?.refresh_token) {
      if (refreshing) {
        // 已有请求在刷新：排队等它完成再重放
        return new Promise((resolve, reject) => {
          pendingQueue.push(() => {
            http(original).then(resolve).catch(reject)
          })
        })
      }
      refreshing = true
      original._retried = true
      const ok = await auth.refresh()
      refreshing = false
      flushQueue()
      if (ok) {
        return http(original) // 换到新令牌后重试原请求
      }
      auth.clear()
      window.location.href = '/login' // 刷新也失败 → 回登录页
    }

    // 业务错误信息展示（后端返回的 detail 是中文提示）
    const detail = (error.response?.data as { detail?: string } | undefined)?.detail
    if (detail) {
      ElMessage.error(detail)
    } else if (error.code === 'ECONNABORTED') {
      ElMessage.error('请求超时，请稍后重试')
    }
    return Promise.reject(error)
  },
)

export default http
