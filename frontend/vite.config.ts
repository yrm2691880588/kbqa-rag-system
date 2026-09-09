import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// Vite 开发服务器配置
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      // 让代码里能用 @/ 快捷指向 src 目录
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    // 监听所有网卡地址（IPv4 + IPv6 都支持）：
    // 解决 Windows 上 vite 只绑 [::1](IPv6) 导致浏览器走 127.0.0.1(IPv4) 时"拒绝连接"的问题
    host: '0.0.0.0',
    // 前端请求 /api 开头的地址时，自动转发给本机后端(8000端口)，避免跨域问题
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
