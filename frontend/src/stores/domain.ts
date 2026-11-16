/**
 * 领域配置（全局共享）。
 *
 * ## 为什么用 store 而不是每个组件各自请求
 *
 * 领域文案在多个地方用到：登录页的口号、聊天页的空状态标题、
 * 顶栏、浏览器标签……如果每个组件各发一次请求，
 * 同一个页面会闪好几次，而且刷新时机不一致会出现"标题是电商、
 * 欢迎语是财经"的错乱。
 *
 * 这里做两件事：
 *   1. 全局只请求一次（并发调用共享同一个 Promise）
 *   2. 失败时用**中性文案**兜底，绝不显示"商品"这类领域词 ——
 *      宁可显示"知识库问答系统"，也不能在财经项目里露出电商文案
 */
import { ref } from 'vue'
import { getDomainConfig, type DomainConfig } from '@/api/domain'

/** 兜底文案：刻意保持中性，不含任何行业词。 */
const FALLBACK: DomainConfig = {
  name: 'generic',
  display_name: '',
  app_title: '知识库问答系统',
  login_slogan: '把文档交给它，用自然语言提问，得到带出处的答案',
  welcome_title: '知识库智能问答',
  welcome_hint: '点击左侧「新对话」开始提问。AI 将依据知识库资料回答，并标注引用来源。',
  example_questions: [],
}

const config = ref<DomainConfig>({ ...FALLBACK })
const loaded = ref(false)

// 并发去重：多个组件同时挂载时只发一次请求
let inflight: Promise<DomainConfig> | null = null

/**
 * 加载领域配置（幂等）。
 *
 * 失败不抛异常：领域文案拿不到时应该退化为中性文案继续可用，
 * 而不是让整个页面打不开。
 */
export async function loadDomainConfig(): Promise<DomainConfig> {
  if (loaded.value) return config.value
  if (inflight) return inflight

  inflight = getDomainConfig()
    .then((d) => {
      config.value = d
      loaded.value = true
      return d
    })
    .catch(() => {
      // 静默失败：拦截器已经提示过网络问题；
      // 这里用中性兜底，保证界面还能用。
      return config.value
    })
    .finally(() => {
      inflight = null
    })

  return inflight
}

export function useDomain() {
  return { config, loaded, loadDomainConfig }
}
