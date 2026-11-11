import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'
import path from 'node:path'

// Vite 开发服务器配置
//
// 【端口为什么不写死】
//   开发机上可能同时跑着好几个项目，8000/5173 是最容易被占用的两个端口。
//   一旦撞车，vite 默认会"自动换一个端口"，但后端代理还指着 8000，
//   于是页面能开、接口全 404 —— 这个坑很难一眼看出来。
//   所以这里改成从「项目根目录的 .env」读取，与启动脚本用的是同一份配置。
//
// 【为什么读根目录的 .env 而不是 frontend/.env】
//   端口配置放在项目根目录一处，避免前后端各配一份、改了一处忘了另一处。
const ROOT = fileURLToPath(new URL('..', import.meta.url))

export default defineConfig(({ mode }) => {
  // loadEnv 的第三个参数传 '' 表示不限制前缀（默认只暴露 VITE_ 开头的）
  const rootEnv = loadEnv(mode, ROOT, '')

  const backendPort = rootEnv.BACKEND_PORT || process.env.BACKEND_PORT || '8000'
  const frontendPort = Number(
    rootEnv.FRONTEND_PORT || process.env.FRONTEND_PORT || 5173,
  )

  return {
    plugins: [vue()],
    resolve: {
      alias: {
        // 让代码里能用 @/ 快捷指向 src 目录
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    server: {
      port: frontendPort,
      // 端口被占用时直接报错退出，而不是悄悄换一个端口。
      // 悄悄换端口的后果是：后端代理还指着旧的，页面能开但接口全挂。
      // 真占用了就让用户去 .env 里改 FRONTEND_PORT，比"看起来能用其实坏了"好。
      strictPort: true,
      // 监听所有网卡地址（IPv4 + IPv6 都支持）：
      // 解决 Windows 上 vite 只绑 [::1](IPv6) 导致浏览器走 127.0.0.1(IPv4) 时"拒绝连接"的问题
      host: '0.0.0.0',
      // 前端请求 /api 开头的地址时，自动转发给本机后端，避免跨域问题
      proxy: {
        '/api': {
          target: `http://127.0.0.1:${backendPort}`,
          changeOrigin: true,
        },
      },
    },
  }
})
