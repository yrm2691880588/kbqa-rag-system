/**
 * 401 的分类判定（从 http.ts 抽出的纯函数，便于单独测试）。
 *
 * 为什么单独抽出来：
 *   这段逻辑已经出过 **三次** 同类 bug：
 *     1) 打开页面白屏   —— /users/me 的 401 触发了续期+跳转，打断渲染
 *     2) 登录一直转圈   —— /auth/login 的 401 触发了递归续期
 *     3) 点确认没反应   —— /users/me/password 被当成探测请求，既不续期也不提示
 *   根因都是"用模糊匹配去判断一个本该精确区分的语义"。
 *   抽成纯函数后可以写单测，改一次就锁死一次。
 */

/** 鉴权入口：这些接口的 401 是**业务结果**（如密码错），不是会话过期。 */
const AUTH_ENTRY_PATHS = [
  '/auth/login',
  '/auth/register',
  '/auth/refresh',
  '/auth/logout',
  '/auth/sms/login',
  '/auth/password/',
]

/** 去掉 query 与 hash，只留路径部分。 */
function normalizePath(url: string): string {
  return url.split('?')[0].split('#')[0]
}

/**
 * 是否是"探测登录态"的请求。
 *
 * 只认**精确**的 /users/me。
 * 不能用 includes('/users/me') —— 那会把 /users/me/password、
 * /users/me/bind-phone 也算进来，它们 401 时不提示就成了"点了没反应"。
 */
export function isProbeRequest(url: string): boolean {
  return normalizePath(url) === '/users/me'
}

/** 是否是鉴权入口请求（登录/注册/刷新/登出/找回密码等）。 */
export function isAuthEntryRequest(url: string): boolean {
  const path = normalizePath(url)
  return AUTH_ENTRY_PATHS.some((p) => path.includes(p))
}

/**
 * 该 401 是否应该触发"刷新令牌并重试"。
 *
 * 三类情况不触发：
 *   · 探测请求（未登录是正常状态）
 *   · 鉴权入口（401 是业务结果，触发续期会递归）
 *   · 刷新请求自身（再触发一次就死循环）
 */
export function shouldRefreshOn401(
  url: string,
  opts: { isRefreshCall?: boolean } = {},
): boolean {
  if (opts.isRefreshCall) return false
  if (isProbeRequest(url)) return false
  if (isAuthEntryRequest(url)) return false
  return true
}

/** 该 401 是否应该给用户弹错误提示。探测请求不弹（用户还没登录，不是错误）。 */
export function shouldShowErrorOn401(url: string): boolean {
  return !isProbeRequest(url)
}
