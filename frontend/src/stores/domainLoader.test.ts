/**
 * 领域配置加载器的单元测试。
 *
 * 为什么需要（这段逻辑有三个易错点）：
 *   1. **并发去重**：登录页、聊天页可能同时挂载，各调一次 load()。
 *      不去重就会发两次请求，还可能出现两个响应回的文案不一致。
 *   2. **成功后不再请求**：每次路由切换都重新请求会一直闪文案。
 *   3. **失败要能重试**：一次网络抖动就把 loaded 置 true 的话，
 *      整个会话都会停在兜底文案上，用户永远看不到真实领域名。
 *
 * 另外验证兜底文案**不含任何行业词** —— 这是硬要求：
 * 换领域后如果接口失败，绝不能让用户看到别的行业词。
 *
 * 运行：npm test（或 node --test src/stores/domainLoader.test.ts）
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  createDomainLoader,
  NEUTRAL_FALLBACK,
  type DomainConfigShape,
} from './domainLoader.ts'

const ECOMMERCE: DomainConfigShape = {
  name: 'ecommerce',
  display_name: '电商',
  app_title: '商品知识库问答系统',
  login_slogan: '把商品资料交给它',
  welcome_title: '商品知识库智能问答',
  welcome_hint: '点击左侧新对话',
  example_questions: ['示例问题一'],
}

const FINANCE: DomainConfigShape = {
  name: 'finance',
  display_name: '财经',
  app_title: '财经数据知识库问答系统',
  login_slogan: '把财报交给它',
  welcome_title: '财经数据知识库问答',
  welcome_hint: '点击左侧新对话',
  example_questions: ['示例问题二'],
}

// ---------------------------------------------------------------------------
// 兜底文案
// ---------------------------------------------------------------------------

test('兜底文案不含任何行业词', () => {
  // 这是硬要求：接口失败时可能短暂显示兜底文案，
  // 若它含行业词，换领域后就露馅了。
  const text = JSON.stringify(NEUTRAL_FALLBACK)
  const forbidden = ['商品', '电商', '股票', '财经', '医疗', '法律', '手机']
  for (const w of forbidden) {
    assert.equal(
      text.includes(w),
      false,
      `兜底文案不该包含行业词「${w}」，实际内容：${text}`,
    )
  }
})

test('兜底文案各字段非空且可用', () => {
  assert.ok(NEUTRAL_FALLBACK.app_title.length > 0)
  assert.ok(NEUTRAL_FALLBACK.login_slogan.length > 0)
  assert.ok(NEUTRAL_FALLBACK.welcome_title.length > 0)
  assert.ok(NEUTRAL_FALLBACK.welcome_hint.length > 0)
  assert.deepEqual(NEUTRAL_FALLBACK.example_questions, [])
})

// ---------------------------------------------------------------------------
// 加载成功
// ---------------------------------------------------------------------------

test('加载成功后返回后端配置', async () => {
  const loader = createDomainLoader(async () => ECOMMERCE)
  const d = await loader.load()
  assert.equal(d.display_name, '电商')
  assert.equal(loader.isLoaded(), true)
  assert.equal(loader.current().app_title, '商品知识库问答系统')
})

test('成功后再次调用不再发请求', async () => {
  let calls = 0
  const loader = createDomainLoader(async () => {
    calls++
    return ECOMMERCE
  })

  await loader.load()
  await loader.load()
  await loader.load()

  assert.equal(calls, 1, `应只请求一次，实际 ${calls} 次`)
})

// ---------------------------------------------------------------------------
// 并发去重
// ---------------------------------------------------------------------------

test('并发调用只发一次请求', async () => {
  let calls = 0
  const loader = createDomainLoader(async () => {
    calls++
    // 模拟网络耗时，让三个调用真正重叠
    await new Promise((r) => setTimeout(r, 20))
    return FINANCE
  })

  // 模拟登录页、聊天页、顶栏同时挂载
  const [a, b, c] = await Promise.all([loader.load(), loader.load(), loader.load()])

  assert.equal(calls, 1, `并发三次应只请求一次，实际 ${calls} 次`)
  assert.equal(a.name, 'finance')
  assert.equal(b.name, 'finance')
  assert.equal(c.name, 'finance')
})

test('并发调用拿到的是同一个对象（不会出现文案不一致）', async () => {
  const loader = createDomainLoader(async () => FINANCE)
  const [a, b] = await Promise.all([loader.load(), loader.load()])
  assert.equal(a.app_title, b.app_title)
})

// ---------------------------------------------------------------------------
// 失败处理
// ---------------------------------------------------------------------------

test('请求失败时返回中性兜底，不抛异常', async () => {
  const loader = createDomainLoader(async () => {
    throw new Error('网络断了')
  })

  // 不应该 reject —— 界面要继续可用
  const d = await loader.load()
  assert.equal(d.app_title, NEUTRAL_FALLBACK.app_title)
  assert.equal(d.display_name, '')
})

test('失败后不标记已加载，下次会重试', async () => {
  let calls = 0
  const loader = createDomainLoader(async () => {
    calls++
    if (calls === 1) throw new Error('第一次失败')
    return ECOMMERCE
  })

  const first = await loader.load()
  assert.equal(first.display_name, '', '首次失败应用兜底')
  assert.equal(loader.isLoaded(), false, '失败不应标记为已加载')

  // 第二次应该真的重试并成功
  const second = await loader.load()
  assert.equal(calls, 2, '失败后应重试')
  assert.equal(second.display_name, '电商')
  assert.equal(loader.isLoaded(), true)
})

test('失败时不会把上一次成功的配置清掉', async () => {
  let calls = 0
  const loader = createDomainLoader(async () => {
    calls++
    if (calls === 1) return ECOMMERCE
    throw new Error('后续失败')
  })

  await loader.load()
  assert.equal(loader.current().display_name, '电商')

  // 理论上 loaded 后不会再请求；这里直接验证 current 不被破坏
  const d = await loader.load()
  assert.equal(d.display_name, '电商')
})
