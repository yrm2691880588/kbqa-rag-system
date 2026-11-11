/**
 * 401 分类逻辑的单元测试。
 *
 * 为什么必须有这些测试：
 *   这段逻辑已经出过**三次**同类 bug，每次都是"某个路径没被正确分类"：
 *     1) /users/me        的 401 触发了续期+跳转  -> 打开页面白屏
 *     2) /auth/login      的 401 触发了递归续期    -> 登录一直转圈
 *     3) /users/me/password 被当成探测请求         -> 点确认没反应、也不报错
 *   前端此前完全没有测试设施，所以第三次漏检没人发现。
 *
 * 运行：npm test（或 node --test src/api/authRetry.test.ts）
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  isAuthEntryRequest,
  isProbeRequest,
  shouldRefreshOn401,
  shouldShowErrorOn401,
} from './authRetry.ts'

test('探测请求只认精确的 /users/me', () => {
  assert.equal(isProbeRequest('/users/me'), true)
  assert.equal(isProbeRequest('/users/me?x=1'), true)

  // 这三个是踩过的坑：不能因为前缀相同就算探测请求
  assert.equal(isProbeRequest('/users/me/password'), false, '/users/me/password 被误判为探测请求')
  assert.equal(isProbeRequest('/users/me/bind-phone'), false, '/users/me/bind-phone 被误判为探测请求')
  assert.equal(isProbeRequest('/users'), false)
})

test('鉴权入口请求应被识别', () => {
  for (const p of [
    '/auth/login',
    '/auth/register',
    '/auth/refresh',
    '/auth/logout',
    '/auth/sms/login',
    '/auth/password/forgot',
    '/auth/password/reset',
  ]) {
    assert.equal(isAuthEntryRequest(p), true, `${p} 应被识别为鉴权入口`)
  }
})

test('普通业务接口不是鉴权入口', () => {
  for (const p of ['/users/me', '/knowledge-bases', '/chat', '/sessions', '/query-logs']) {
    assert.equal(isAuthEntryRequest(p), false, `${p} 不应被识别为鉴权入口`)
  }
})

test('探测请求的 401 不触发续期', () => {
  assert.equal(shouldRefreshOn401('/users/me'), false)
})

test('鉴权入口的 401 不触发续期（否则递归死循环）', () => {
  assert.equal(shouldRefreshOn401('/auth/login'), false)
  assert.equal(shouldRefreshOn401('/auth/refresh'), false)
})

test('刷新请求自身的 401 绝不再次触发刷新', () => {
  assert.equal(shouldRefreshOn401('/auth/refresh', { isRefreshCall: true }), false)
})

test('普通业务接口的 401 应该触发续期重试', () => {
  assert.equal(shouldRefreshOn401('/knowledge-bases'), true)
  assert.equal(shouldRefreshOn401('/chat'), true)
  assert.equal(shouldRefreshOn401('/users/me/password'), true, '这个接口过期时应正常续期')
})

test('探测请求的 401 不弹错误提示（用户还没登录，不是错误）', () => {
  assert.equal(shouldShowErrorOn401('/users/me'), false)
})

test('用户主动操作的 401 必须弹提示', () => {
  assert.equal(shouldShowErrorOn401('/users/me/password'), true, '改密码失败必须告诉用户')
  assert.equal(shouldShowErrorOn401('/users/me/bind-phone'), true, '绑手机失败必须告诉用户')
  assert.equal(shouldShowErrorOn401('/auth/login'), true, '密码错了必须告诉用户')
})
