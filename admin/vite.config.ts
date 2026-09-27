import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// В разработке API и WebSocket проксируются на центр (python -m center).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/ws': { target: 'ws://127.0.0.1:8000', ws: true },
    },
  },
})
