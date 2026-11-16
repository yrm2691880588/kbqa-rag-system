// 领域配置 API。
//
// 前端不写死任何领域文案（"商品知识库问答"之类），一律从后端取。
// 这样导出财经项目后，界面自动变成"财经数据知识库问答系统"，
// 不需要改前端代码、也不需要重新构建。
import http from './http'

export interface DomainConfig {
  name: string
  display_name: string
  app_title: string
  login_slogan: string
  welcome_title: string
  welcome_hint: string
  example_questions: string[]
}

/** 取当前领域的界面文案（无需登录）。 */
export function getDomainConfig() {
  return http.get<DomainConfig>('/domain/config').then((r) => r.data)
}
