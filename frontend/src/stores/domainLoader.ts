/**
 * 领域配置加载的**纯逻辑**（不依赖 Vue，可单测）。
 *
 * ## 为什么从 stores/domain.ts 里抽出来
 *
 * store 里混了两件事：
 *   1. 加载逻辑（并发去重、失败兜底）—— 这是**有回归风险**的部分
 *   2. Vue 的 ref 状态管理 —— 这层很薄，出不了什么错
 *
 * 只有第 1 部分值得测，但它依赖 Vue 的 ref，没法直接跑 Node 测试。
 * 把它抽成纯函数后，行为可以完整验证：
 *   · 并发调用只发一次请求
 *   · 加载成功后不再重复请求
 *   · 失败时用中性文案兜底，且下次调用会重试
 */

/** 领域配置的字段（与后端 /api/v1/domain/config 对应）。 */
export interface DomainConfigShape {
  name: string
  display_name: string
  app_title: string
  login_slogan: string
  welcome_title: string
  welcome_hint: string
  example_questions: string[]
}

/**
 * 兜底文案：刻意保持中性，不含任何行业词。
 *
 * 为什么重要：如果兜底文案写着某个行业的名字，那么
 * 在另一个领域的项目里接口失败的瞬间，用户会看到不相干的行业词。
 * 宁可显示笼统的"知识库问答系统"，也不能露出别的行业词。
 */
export const NEUTRAL_FALLBACK: DomainConfigShape = {
  name: 'generic',
  display_name: '',
  app_title: '知识库问答系统',
  login_slogan: '把文档交给它，用自然语言提问，得到带出处的答案',
  welcome_title: '知识库智能问答',
  welcome_hint: '点击左侧「新对话」开始提问。AI 将依据知识库资料回答，并标注引用来源。',
  example_questions: [],
}

/** 加载器的状态与行为（可注入 fetch 函数，便于测试）。 */
export interface DomainLoader {
  /** 当前文案（加载成功后是真实配置，失败时是中性兜底）。 */
  current(): DomainConfigShape
  /** 是否已经从后端成功加载过。 */
  isLoaded(): boolean
  /** 加载（幂等；并发调用共享同一个请求）。 */
  load(): Promise<DomainConfigShape>
}

/**
 * 创建一个领域配置加载器。
 *
 * @param fetcher 实际的请求函数（测试时可注入假实现）
 */
export function createDomainLoader(
  fetcher: () => Promise<DomainConfigShape>,
): DomainLoader {
  let config: DomainConfigShape = { ...NEUTRAL_FALLBACK }
  let loaded = false
  let inflight: Promise<DomainConfigShape> | null = null

  return {
    current: () => config,
    isLoaded: () => loaded,

    async load() {
      // 已经成功加载过：直接返回，不再请求
      if (loaded) return config

      // 有请求在进行中：复用它（并发去重）
      if (inflight) return inflight

      inflight = fetcher()
        .then((d) => {
          config = d
          loaded = true
          return d
        })
        .catch(() => {
          // 失败不抛异常：界面应该继续可用，只是文案退化为中性版。
          // 注意不把 loaded 置 true —— 下次调用应该重试，
          // 否则一次网络抖动会让整个会话都停留在兜底文案上。
          return config
        })
        .finally(() => {
          inflight = null
        })

      return inflight
    },
  }
}
