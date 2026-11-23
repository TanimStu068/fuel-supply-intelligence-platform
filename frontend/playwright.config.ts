import { defineConfig, devices } from '@playwright/test'

/**
 * The end-to-end configuration.
 *
 * The e2e tests run against the *real* stack: the freshly built frontend
 * served by the preview server, and the real backend behind it. The
 * preview server is what the tests drive — the Docker container on 5173
 * may be serving an older build.
 */
export default defineConfig({
  testDir: './e2e',
  timeout: 45_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:4173',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } },
    },
  ],
  webServer: {
    command: 'npm run preview:e2e',
    url: 'http://localhost:4173',
    reuseExistingServer: true,
    timeout: 120_000,
  },
})
