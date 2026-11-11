/**
 * 时间格式化（全站共用）。
 *
 * 为什么要有这个文件：
 *   原本 7 个组件各自写了一份 fmtTime，有 3 种不同行为：
 *     · ChatView      当天只显示 "14:30"，跨天显示 "10-09 14:30"
 *     · KnowledgeBase 完整日期，但没判空、没判无效日期
 *     · 其余 4 处     完整日期 + 判空 + 判无效日期
 *   于是同一个后端时间戳在不同页面显示格式不一样，
 *   某些页面遇到空值还会显示 "NaN-NaN-NaN"。
 *
 * 现在统一到这里，两个用途各一个函数。行为固定，不再各写各的。
 */

function pad2(n: number): string {
  return String(n).padStart(2, '0')
}

/** 把后端返回的时间字符串解析成 Date；无效或为空返回 null。 */
function parseDate(value?: string | null): Date | null {
  if (!value) return null
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return null
  return d
}

/**
 * 完整日期时间：2026-11-11 15:59
 * 空值或无效日期返回空字符串（不显示 NaN）。
 */
export function formatDateTime(value?: string | null): string {
  const d = parseDate(value)
  if (!d) return ''
  return (
    `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ` +
    `${pad2(d.getHours())}:${pad2(d.getMinutes())}`
  )
}

/**
 * 聊天场景的简洁时间：
 *   · 今天  -> 15:59
 *   · 其他  -> 11-11 15:59
 */
export function formatChatTime(value?: string | null): string {
  const d = parseDate(value)
  if (!d) return ''
  const hm = `${pad2(d.getHours())}:${pad2(d.getMinutes())}`
  const now = new Date()
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate()
  if (sameDay) return hm
  return `${d.getMonth() + 1}-${d.getDate()} ${hm}`
}
