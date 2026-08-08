import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// Vitest reads this file instead of vite.config.ts, so the React plugin is declared again rather
// than merged: the test run needs JSX, not the dev server or the build output settings.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/setup-tests.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
