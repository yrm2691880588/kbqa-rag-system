// 认证相关的后端接口封装
import http from './http'
import type { UserInfo } from '@/stores/auth'

export interface TokenPair {
  access_token: string
  refresh_token: string
  token_type: string
  user: UserInfo
}

export function login(username: string, password: string) {
  return http.post<TokenPair>('/auth/login', { username, password }).then((r) => r.data)
}

export function register(username: string, password: string, nickname: string) {
  return http.post<UserInfo>('/auth/register', { username, password, nickname }).then((r) => r.data)
}

export function refresh(refresh_token: string) {
  return http.post<TokenPair>('/auth/refresh', { refresh_token }).then((r) => r.data)
}

export function logout(refresh_token: string) {
  return http.post('/auth/logout', { refresh_token })
}

export function getMe() {
  return http.get<UserInfo>('/users/me').then((r) => r.data)
}

export function changePassword(old_password: string, new_password: string) {
  return http
    .put<{ message: string }>('/users/me/password', { old_password, new_password })
    .then((r) => r.data)
}
