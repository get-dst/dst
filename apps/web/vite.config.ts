import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// The API this dev server proxies to. `dst dev` / `make dev` serve on 8000;
// point DST_DEV_API elsewhere to develop against another instance.
const API = process.env.DST_DEV_API ?? 'http://localhost:8000'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  // Same-origin in dev too, so the app's fetches are relative EVERYWHERE and there
  // is no build-time URL to get wrong. The alternative — a baked-in absolute base —
  // shipped a dashboard that silently asked the wrong port whenever `dst serve`
  // ran anywhere but 8000.
  server: {
    proxy: Object.fromEntries(
      ['/mgmt', '/auth', '/v1', '/health', '/ready', '/mcp'].map((p) => [
        p,
        { target: API, changeOrigin: true },
      ]),
    ),
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/test/setup.ts',
  },
})
