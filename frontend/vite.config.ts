import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发时前端 18200，API 代理到后端 18090，避免跨域与硬编码地址
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 18200,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:18090',
        changeOrigin: true,
        ws: true,
      },
    },
  },
  build: { outDir: 'dist', sourcemap: false },
})
