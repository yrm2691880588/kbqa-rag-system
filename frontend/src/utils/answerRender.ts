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
 *
 * 2026-11 变更：加入 Markdown 渲染。
 *   原因：模型回答里大量使用 `## 标题`、`| 表格 |`、`**粗体**`，
 *   而原实现只做转义 + 角标替换，用户看到的是一堆原始符号。
 */
import { marked } from 'marked'
import DOMPurify from 'dompurify'

/** HTML 转义。注意 & 必须最先替换，否则会二次转义。 */
export function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/**
 * 把 [1][2] 角标渲染成可点击的引用角标。
 *
 * 注意：这个函数**假定输入已经是 HTML**（Markdown 渲染之后的产物），
 * 所以在 Markdown 渲染**之后**调用。
 */
export function renderCitations(html: string): string {
  if (!html) return ''
  return html.replace(
    /\[(\d{1,2})\]/g,
    '<sup class="cite-sup" data-idx="$1">$1</sup>',
  )
}

/**
 * 把 AI 回答渲染成可安全插入 DOM 的 HTML。
 *
 * 处理流程（顺序很重要）：
 *   1. Markdown -> HTML（marked）
 *   2. 把 [n] 换成可点击角标
 *   3. **消毒**（DOMPurify）—— 必须最后做，清掉模型可能输出的危险标签
 *
 * 为什么必须消毒：
 *   模型输出是**不可信内容**。它在正常回答里混入 <script> 或
 *   <img onerror=...> 不是不可能（提示注入、模型被污染、上游被篡改）。
 *   直接 v-html 渲染等于把 XSS 入口交给外部输入。
 *
 * 为什么不自己写 Markdown 解析：
 *   表格、嵌套列表、代码块、转义规则……手写一定会漏，
 *   而漏的那部分恰好是最容易出安全问题的地方。
 */
export function renderAnswer(content: string): string {
  if (!content) return ''

  let html: string
  try {
    // breaks: true 让单个换行也变成 <br>，更符合聊天场景的阅读习惯
    // gfm: true 启用表格、删除线等 GitHub 风格扩展
    html = marked.parse(content, { breaks: true, gfm: true, async: false }) as string
  } catch {
    // Markdown 解析失败（极罕见）：退回纯文本 + 转义，保证不白屏
    return renderCitations(escapeHtml(content))
  }

  html = renderCitations(html)

  return DOMPurify.sanitize(html, {
    // 允许的标签：Markdown 常见产物 + 我们自己的角标
    ALLOWED_TAGS: [
      'p', 'br', 'strong', 'em', 'u', 's', 'del', 'code', 'pre',
      'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
      'ul', 'ol', 'li',
      'blockquote',
      'table', 'thead', 'tbody', 'tr', 'th', 'td',
      'hr', 'a', 'sup', 'span', 'div',
    ],
    ALLOWED_ATTR: ['class', 'data-idx', 'href', 'title', 'target', 'rel'],
    // 链接只允许安全协议，挡掉 javascript: 这类
    ALLOWED_URI_REGEXP: /^(?:https?:|mailto:|#)/i,
  })
}
