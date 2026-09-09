// 应用入口：创建 Vue 实例、挂载路由/状态库/UI 组件库
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
import 'element-plus/dist/index.css'

import App from './App.vue'
import router from './router'
import './style.css'

const app = createApp(App)

// 全局注册 Element Plus 图标（菜单、按钮等处直接用 <el-icon><User /></el-icon>）
for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, component)
}

app.use(createPinia())
app.use(router)
app.use(ElementPlus, { locale: zhCn }) // 组件文案用中文
app.mount('#app')
