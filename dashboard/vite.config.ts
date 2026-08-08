import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: 'dist',
  },
  server: {
    // `npm run dev` serves the SPA on its own port; the API is the one in docker-compose.
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
})
