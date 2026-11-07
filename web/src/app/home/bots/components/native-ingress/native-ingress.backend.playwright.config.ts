import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: '.',
  testMatch: 'native-ingress.backend.e2e.spec.ts',
  workers: 1,
  retries: 0,
  timeout: 60000,
  outputDir: '../../../../../../test-results/native-ingress-backend',
  use: {
    baseURL: 'http://127.0.0.1:4173',
    headless: true,
    trace: 'retain-on-failure',
  },
  webServer: {
    command:
      'node src/app/home/bots/components/native-ingress/native-ingress.vite.mjs',
    cwd: process.cwd(),
    url: 'http://127.0.0.1:4173',
    reuseExistingServer: false,
    timeout: 120000,
  },
});
