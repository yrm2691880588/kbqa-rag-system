/// <reference types="vite/client" />

// Vue 单文件组件的类型声明，让 TypeScript 认识 .vue 文件
declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<{}, {}, any>
  export default component
}
