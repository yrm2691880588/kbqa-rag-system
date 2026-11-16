/**
 * 领域配置（全局共享）。
 *
 * ## 为什么用 store 而不是每个组件各自请求
 *
 * 领域文案在多个地方用到：登录页的口号、聊天页的空状态标题、
 * 顶栏、浏览器标签……如果每个组件各发一次请求，
 * 同一个页面会闪好几次，而且刷新时机不一致会出现"标题是某领域、
 * 欢迎语是另一领域"的错乱。
 *
 * ## 加载逻辑在哪
 *
 * 并发去重、失败兜底这些**有回归风险**的逻辑在 `domainLoader.ts`，
 * 那是个不依赖 Vue 的纯模块，有单元测试覆盖。
 * 本文件只负责把它的状态接到 Vue 的响应式系统上。
 */
import { ref } from 'vue'
import { getDomainConfig } from '@/api/domain'
import {
  createDomainLoader,
  NEUTRAL_FALLBACK,
  type DomainConfigShape,
} from './domainLoader'

// 兼容原类型名：组件里用的是 DomainConfig
export type DomainConfig = DomainConfigShape

const loader = createDomainLoader(() => getDomainConfig())

/** 当前文案（响应式）。初值是中性兜底，加载成功后替换。 */
const config = ref<DomainConfigShape>({ ...NEUTRAL_FALLBACK })

/** 是否已成功从后端加载。 */
const loaded = ref(false)

/**
 * 加载领域配置（幂等）。
 *
 * 失败不抛异常：领域文案拿不到时应该退化为中性文案继续可用，
 * 而不是让整个页面打不开。
 */
export async function loadDomainConfig(): Promise<DomainConfigShape> {
  const d = await loader.load()
  config.value = d
  loaded.value = loader.isLoaded()
  return d
}

export function useDomain() {
  return { config, loaded, loadDomainConfig }
}
