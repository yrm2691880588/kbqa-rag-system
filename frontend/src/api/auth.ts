// 认证相关的后端接口封装
import http from './http'
import type { UserInfo } from '@/stores/auth'

export interface TokenPair {
  access_token: string
  refresh_token: string
  token_type: string
  user: UserInfo
}

/** 图形验证码：注册 / 发短信前需要答对它 */
export interface CaptchaData {
  captcha_id: string
  image_base64: string
  expires_in: number
}

/** 短信验证码发送结果（dev_code 只在测试模式返回） */
export interface SmsSendResult {
  message: string
  dev_code?: string | null
  retry_after: number
}

export function getCaptcha() {
  return http.get<CaptchaData>('/auth/captcha').then((r) => r.data)
}

export function login(username: string, password: string) {
  return http.post<TokenPair>('/auth/login', { username, password }).then((r) => r.data)
}

export function register(username: string, password: string, nickname: string, captchaId: string, captchaCode: string) {
  return http
    .post<UserInfo>('/auth/register', {
      username,
      password,
      nickname,
      captcha_id: captchaId,
      captcha_code: captchaCode,
    })
    .then((r) => r.data)
}

/** 给手机号发短信验证码（须先答对图形验证码） */
export function sendSmsCode(phone: string, captchaId: string, captchaCode: string) {
  return http
    .post<SmsSendResult>('/auth/sms/send', { phone, captcha_id: captchaId, captcha_code: captchaCode })
    .then((r) => r.data)
}

/** 手机号 + 短信验证码 登录（没注册过会自动注册） */
export function smsLogin(phone: string, code: string) {
  return http.post<TokenPair>('/auth/sms/login', { phone, code }).then((r) => r.data)
}

/** 已登录用户绑定手机号（绑定后可手机号验证码登录同一账号） */
export function bindPhone(phone: string, code: string) {
  return http.post<{ message: string }>('/users/me/bind-phone', { phone, code }).then((r) => r.data)
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
