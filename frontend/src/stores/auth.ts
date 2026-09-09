// 用户登录状态管理（Pinia）
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import * as authApi from '@/api/auth'

// 本地存储键：把令牌持久化，刷新页面不丢登录
const TOKEN_KEY = 'kbqa_tokens'

export interface UserInfo {
  id: number
  username: string
  nickname: string
  role: 'admin' | 'user'
  is_active: boolean
  created_at: string
  last_login_at?: string | null
}

interface Tokens {
  access_token: string
  refresh_token: string
}

function loadTokens(): Tokens | null {
  const raw = localStorage.getItem(TOKEN_KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw) as Tokens
  } catch {
    return null
  }
}

export const useAuthStore = defineStore('auth', () => {
  const tokens = ref<Tokens | null>(loadTokens())
  const user = ref<UserInfo | null>(null)

  // 访问令牌（路由守卫判断"是否已登录"用；有 token 才等于已登录）
  // 【修复】之前路由守卫里用 auth.accessToken 但这里从未暴露过该属性，
  // 导致守卫永远认为"未登录"，登录后跳转 /chat 总被弹回 /login。
  const accessToken = computed(() => tokens.value?.access_token || '')

  function saveTokens(t: Tokens) {
    tokens.value = t
    localStorage.setItem(TOKEN_KEY, JSON.stringify(t))
  }

  // 登录成功后保存令牌 + 用户信息
  async function login(username: string, password: string) {
    const data = await authApi.login(username, password)
    saveTokens({ access_token: data.access_token, refresh_token: data.refresh_token })
    user.value = data.user
  }

  // 拉取当前用户信息（页面刷新后恢复登录态用）
  async function fetchMe() {
    const me = await authApi.getMe()
    user.value = me
    return me
  }

  // 用刷新令牌换新令牌（访问令牌过期后静默续期）
  async function refresh(): Promise<boolean> {
    if (!tokens.value?.refresh_token) return false
    try {
      const data = await authApi.refresh(tokens.value.refresh_token)
      saveTokens({ access_token: data.access_token, refresh_token: data.refresh_token })
      user.value = data.user
      return true
    } catch {
      return false
    }
  }

  // 退出登录：通知后端作废令牌并清空本地
  async function logout() {
    const rt = tokens.value?.refresh_token
    clear()
    if (rt) {
      try {
        await authApi.logout(rt)
      } catch {
        /* 忽略网络异常，本地已登出 */
      }
    }
  }

  // 修改密码（后端已验证旧密码）
  async function changePassword(oldPassword: string, newPassword: string) {
    return authApi.changePassword(oldPassword, newPassword)
  }

  // 清空本地登录态
  function clear() {
    tokens.value = null
    user.value = null
    localStorage.removeItem(TOKEN_KEY)
  }

  return {
    tokens,
    user,
    accessToken,
    login,
    fetchMe,
    refresh,
    logout,
    changePassword,
    clear,
  }
})
