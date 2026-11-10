// 用户登录状态管理（Pinia）
//
// 【安全改造】令牌不再存 localStorage
// ------------------------------------------------
// 原来：登录后把 access_token / refresh_token 存进 localStorage。
//   问题：localStorage 任何 JS 都读得到。页面上只要有一处 XSS
//   （比如某个 v-html 渲染了用户可控内容），攻击者一行代码就能把令牌偷走，
//   然后拿你的账号去提问、烧额度，直到令牌过期。
//
// 现在：令牌由后端写进 **HttpOnly Cookie**。
//   - 浏览器每次请求自动携带，但 JS 完全读不到（document.cookie 里没有）；
//   - 即使有 XSS，也偷不走令牌原文。
//
// 因此前端这边**不再保存令牌**，只保存"当前用户信息"用于界面显示。
// "是否已登录"的判定也变了：不再看本地有没有 token，而是看
// 能不能成功请求到 /users/me（后端用 Cookie 判定）。
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import * as authApi from '@/api/auth'

export interface UserInfo {
  id: number
  username: string
  nickname: string
  role: 'admin' | 'user'
  is_active: boolean
  phone?: string | null
  /** 所属组织（客户公司）id；平台超管可能为空 */
  org_id?: number | null
  /** 是否平台超管（系统运营方，能建/管所有组织） */
  is_superadmin?: boolean
  created_at: string
  last_login_at?: string | null
}

export const useAuthStore = defineStore('auth', () => {
  const user = ref<UserInfo | null>(null)
  /** 是否已经尝试过恢复登录态（避免路由守卫在恢复完成前就判定"未登录"） */
  const restored = ref(false)

  // 是否已登录：以"后端认可的用户信息"为准（Cookie 由后端校验）
  const isLoggedIn = computed(() => !!user.value)
  // 兼容旧代码里对 accessToken 的判断（现在没有明文令牌可读，只用来说明"已登录"）
  const accessToken = computed(() => (user.value ? 'cookie-based' : ''))

  // 是否平台超管（能建组织、能看所有组织）——决定侧边栏要不要出现"组织管理"
  const isSuperadmin = computed(() => !!user.value?.is_superadmin)
  // 是否是管理员（含平台超管）：能进知识库管理页
  const isAdmin = computed(() => user.value?.role === 'admin' || isSuperadmin.value)

  // 登录成功后只需保存用户信息（令牌已在 Cookie 里，前端拿不到也不需要）
  function applyLogin(data: authApi.TokenPair) {
    user.value = data.user
    restored.value = true
  }

  async function login(username: string, password: string) {
    const data = await authApi.login(username, password)
    applyLogin(data)
  }

  // 手机号 + 短信验证码 登录（没注册过的号码会自动创建账号）
  async function loginBySms(phone: string, code: string) {
    const data = await authApi.smsLogin(phone, code)
    applyLogin(data)
  }

  // 是否已绑定手机号（绑定后可用手机号验证码登录）
  function isPhoneBound() {
    return !!user.value?.phone
  }

  // 拉取当前用户信息（页面刷新后恢复登录态用）
  async function fetchMe() {
    const me = await authApi.getMe()
    user.value = me
    return me
  }

  /**
   * 页面刷新后恢复登录态。
   * 有 Cookie 就能成功拿到用户信息；没有则视为未登录（不抛错）。
   */
  async function restoreSession(): Promise<boolean> {
    try {
      await fetchMe()
      return true
    } catch {
      user.value = null
      return false
    } finally {
      restored.value = true
    }
  }

  /**
   * 用刷新令牌换新令牌。
   * 刷新令牌在 HttpOnly Cookie 里，前端读不到，所以这里不传参数，
   * 由浏览器自动带上 Cookie；后端换完会重新种 Cookie。
   */
  async function refresh(): Promise<boolean> {
    try {
      const data = await authApi.refresh()
      user.value = data.user
      return true
    } catch {
      return false
    }
  }

  // 退出登录：通知后端作废令牌并清 Cookie，然后清空本地用户信息
  async function logout() {
    try {
      await authApi.logout()
    } catch {
      /* 忽略网络异常，本地仍然要登出 */
    }
    clear()
  }

  // 修改密码（后端已验证旧密码）
  async function changePassword(oldPassword: string, newPassword: string) {
    return authApi.changePassword(oldPassword, newPassword)
  }

  // 清空本地登录态（Cookie 由后端清除）
  function clear() {
    user.value = null
  }

  return {
    user,
    restored,
    accessToken,
    isLoggedIn,
    isSuperadmin,
    isAdmin,
    login,
    loginBySms,
    applyLogin,
    isPhoneBound,
    fetchMe,
    restoreSession,
    refresh,
    logout,
    changePassword,
    clear,
  }
})
