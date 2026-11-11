// HTTP 客户端：统一加令牌、统一处理 401 自动续期、统一报错提示
import axios, { AxiosError, type InternalAxiosRequestConfig } from 'axios'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'

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

    // ── 哪些 401 不该触发"续期"────────────────────────────────
    // 1) 探测型请求（/users/me）：它的 401 表示"当前未登录"，是页面初始化的
    //    正常状态。若也走续期+跳转，会在页面刚打开时打断渲染（白屏）。
    //    交给 store 的 restoreSession 自己 catch。
    const isProbe = url.includes('/users/me')

    // 2) 鉴权入口请求（登录/注册/刷新/登出/找回密码）：它们的 401 是**业务结果**
    //    （比如密码错了），不是"会话过期"。
    //    如果把登录失败的 401 也拿去续期，会形成死循环：
    //      登录401 -> 调refresh -> refresh也401 -> 又进这里 -> 再调refresh ...
    //    表现就是点了登录之后按钮一直转圈、也不报错。
    const isAuthEntry =
      url.includes('/auth/login') ||
      url.includes('/auth/register') ||
      url.includes('/auth/refresh') ||
      url.includes('/auth/logout') ||
      url.includes('/auth/sms/login') ||
      url.includes('/auth/password/')

    // 3) 刷新请求自身发起的调用，绝不能再次触发刷新（双保险，防递归）
    const flag = (original as { _isRefreshCall?: boolean } | undefined)?._isRefreshCall === true

    const skipRefresh = isProbe || isAuthEntry || flag

    // 只有"访问令牌过期"(401) 且该请求还没重试过、且不属于上面三类时，才续期。
    // 注意：这里不再判断"本地有没有 refresh_token"——令牌在 Cookie 里，
    // 前端读不到。刷新能否成功完全由后端判断（Cookie 里的刷新令牌是否有效）。
    if (error.response?.status === 401 && original && !original._retried && !skipRefresh) {
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
    // 探测型请求（isProbe）的 401 不弹窗：它代表"还没登录"，
    // 是页面初始化的正常状态，弹红框只会让用户以为出错了。
    // 但登录/注册这类**用户主动操作**必须弹——用户需要知道"密码错了"。
    if (!isProbe) {
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
