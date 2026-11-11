/**
 * AI 回答的渲染工具（从 ChatView.vue 抽出）。
 *
 * 为什么单独抽出来：
 *   1) 它是**安全关键点**：回答内容来自 AI/用户，会被 v-html 渲染，
 *      转义一旦写错就是 XSS。单独成文件便于集中审查与测试。
 *   2) ChatView.vue 原本 975 行，混杂 7 种职责，改一处要读一大片。
 *      渲染逻辑最独立（纯函数、无组件状态），是最该先抽出来的。
 *
 * 历史坑（已修）：
 *   原实现末尾写的是 `return noHit ? replaced : replaced` —— 两个分支
 *   返回同一个值，noHit 参数完全没起作用。已删除该参数。
 */

/** HTML 转义。注意 & 必须最先替换，否则会二次转义。 */
export function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/**
 * 把回答里的 [1][2] 角标渲染成可点击的引用角标。
 *
 * 安全说明：先对整段文本做 HTML 转义，**之后**才插入我们自己的 <sup> 标签。
 * 顺序不能反 —— 先插标签再转义会把我们自己的标签也转义掉。
 */
export function renderAnswer(content: string): string {
  if (!content) return ''
  const escaped = escapeHtml(content)
  return escaped.replace(
    /\[(\d{1,2})\]/g,
    '<sup class="cite-sup" data-idx="$1">$1</sup>',
  )
}
