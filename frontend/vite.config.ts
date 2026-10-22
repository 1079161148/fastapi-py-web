import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      // 开发态 API 代理到本地 uvicorn；生产由 Nginx 同源反代。
      // target 必须写 127.0.0.1 而不是 localhost：Node 解析 localhost 可能优先
      // ::1，而 uvicorn 默认只监听 IPv4，会导致代理连不上（浏览器里表现为
      // status 0 / Failed，看起来像"登录接口挂了"）
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
