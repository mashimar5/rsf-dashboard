import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Built assets are served by Flask out of static/app, so the base path must
// match where they land. The dev server proxies /api to Flask on 5001.
export default defineConfig({
  plugins: [react()],
  base: '/static/app/',
  build: {
    outDir: '../static/app',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': 'http://localhost:5001',
    },
  },
})
