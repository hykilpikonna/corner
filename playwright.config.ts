import {defineConfig} from '@playwright/test'

export default defineConfig({
  testDir: './tests',
  use: {
    baseURL: 'http://127.0.0.1:4173',
    // Pixel/centering assertions in nuxt-smoke.spec.ts depend on this exact viewport
    viewport: {width: 1280, height: 720},
  },
  webServer: {
    command: 'bun run preview -- --listen 4173',
    url: 'http://127.0.0.1:4173',
    reuseExistingServer: true,
    timeout: 30_000,
  },
})
