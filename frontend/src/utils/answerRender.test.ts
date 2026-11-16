/**
 * 回答渲染的单元测试。
 *
 * ## 为什么这个文件必须有
 *
 * `renderAnswer` 的输出会经过 `v-html` 直接插进 DOM ——
 * 这是整个前端**唯一**的 HTML 注入点，也是唯一的 XSS 攻击面。
 *
 * 而输入是**模型生成的回答**，它是不可信内容：
 *   · 用户可以通过提示注入让模型输出恶意 HTML
 *   · 文档内容里可能夹带标签（上传的 .html 或 .md）
 *   · 上游模型被污染时也会输出危险内容
 *
 * CONTRIBUTING §三 明确要求「涉及安全的改动必须同时加测试」。
 * 这个函数在 2026-11 被整函数重写（引入 marked + DOMPurify），
 * 必须把安全边界固定下来。
 *
 * 运行：npm test
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { escapeHtml, renderCitations } from '../utils/answerRender.ts'

// marked 和 DOMPurify 需要 DOM，node 环境跑不了。
// 所以这里测的是**不依赖 DOM 的部分**：转义与角标替换 ——
// 这两块恰恰是渲染链里最容易写错的地方（顺序反了就会二次转义）。

test('escapeHtml 转义 HTML 特殊字符', () => {
  assert.equal(
    escapeHtml('<script>alert(1)</script>'),
    '&lt;script&gt;alert(1)&lt;/script&gt;',
  )
})

test('escapeHtml 先处理 & 避免二次转义', () => {
  // 如果先替换 < > 再替换 &，&lt; 会变成 &amp;lt;
  assert.equal(escapeHtml('&lt;'), '&amp;lt;')
  assert.equal(escapeHtml('a & b'), 'a &amp; b')
})

test('escapeHtml 转义引号', () => {
  assert.equal(escapeHtml('"x"'), '&quot;x&quot;')
})

test('escapeHtml 空输入返回空串', () => {
  assert.equal(escapeHtml(''), '')
})

test('renderCitations 把 [1] 变成可点击角标', () => {
  const html = renderCitations('净利润 142.7 亿[1]')
  assert.ok(html.includes('cite-sup'), '应生成 cite-sup 角标')
  assert.ok(html.includes('data-idx="1"'), '应带 data-idx 供点击定位')
})

test('renderCitations 处理多个角标', () => {
  const html = renderCitations('结论[1][2][3]')
  const count = (html.match(/cite-sup/g) || []).length
  assert.equal(count, 3, `应生成 3 个角标，实际 ${count}`)
})

test('renderCitations 处理两位数字角标', () => {
  const html = renderCitations('参见[12]')
  assert.ok(html.includes('data-idx="12"'), '应支持两位序号')
})

test('renderCitations 不误伤普通方括号', () => {
  // 三位数以上、非数字内容不应被当成角标
  const html = renderCitations('区间 [100, 200] 以及 [abc]')
  assert.ok(!html.includes('cite-sup'), '普通方括号不该变成角标')
})

test('renderCitations 空输入返回空串', () => {
  assert.equal(renderCitations(''), '')
})

test('角标替换必须在 HTML 转义之后（顺序验证）', () => {
  // 模拟完整流程：先转义，再替换角标
  const raw = '<b>粗体</b>[1]'
  const escaped = escapeHtml(raw)
  const final = renderCitations(escaped)

  // 原文的 <b> 应被转义
  assert.ok(final.includes('&lt;b&gt;'), 'HTML 应被转义')
  // 但我们自己插入的角标标签必须是真的 HTML
  assert.ok(final.includes('<sup'), '角标必须是真标签，不能被转义')
})
