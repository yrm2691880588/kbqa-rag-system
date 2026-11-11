// HTTP 客户端：统一加令牌、统一处理 401 自动续期、统一报错提示
import axios, { AxiosError, type InternalAxiosRequestConfig } from 'axios'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { shouldRefreshOn401, shouldShowErrorOn401 } from './authRetry'

// 创建 axios 实例：所有请求走 /api 前缀（Vite 已配置转发到后端）
const http = axios.create({
  baseURL: '/api/v1',
  timeout: 60000, // 长请求（如上传/问答）60 秒超时
})

// 【安全改造】不再手工加 Authorization 头。
// 令牌在 HttpOnly Cookie 里，浏览器会自动随请求发送（同源即自动带上）。
// withCredentials 打开是为了兼容"前后端不同源部署"的场景
// （此时需要后端 CORS 允许凭据，项目已配置 allow_credentials=True）。
http.defaults.withCredentials = true

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

    const url = original?.url || ''

    // ── 401 的分类判定 ────────────────────────────────────────
    // 具体规则（哪些该续期、哪些该静默）已抽到 ./authRetry 纯函数里，
    // 那里有单元测试锁着 —— 这段逻辑出过三次同类 bug，必须能测。
    const status = error.response?.status
    const canRetry = status === 401 && original && !original._retried
    const allowRefresh =
      canRetry &&
      shouldRefreshOn401(url, {
        isRefreshCall:
          (original as { _isRefreshCall?: boolean } | undefined)?._isRefreshCall === true,
      })

    if (allowRefresh) {
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
      // 不再用 window.location.href 硬跳转。
      // 原因：那会打断正在进行的渲染与导航，页面直接变空白。
      // 现在只清掉本地状态，让 Promise reject 下去；
      // 该去哪由路由守卫根据 isLoggedIn 判断，跳转更自然。
    }

    // 业务错误信息展示（后端返回的 detail 是中文提示）。
    // 探测请求（精确的 /users/me）不弹窗：它代表"还没登录"，是正常状态。
    // 但登录/注册、以及 /users/me/password 这类**用户主动操作**必须弹。
    if (!(status === 401 && !shouldShowErrorOn401(url))) {
      const detail = (error.response?.data as { detail?: string } | undefined)?.detail
      if (detail) {
        ElMessage.error(detail)
      } else if (error.code === 'ECONNABORTED') {
        ElMessage.error('请求超时，请稍后重试')
      }
    }
    return Promise.reject(error)
  },
)

export default http
