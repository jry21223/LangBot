import { defineConfig } from '@playwright/test';
// Synthetic, offline-only UI contract suite. Run from web/ in the isolated runner.
export default defineConfig({
  testDir: '.',
  testMatch: 'native-ingress.e2e.spec.ts',
  workers: 1,
  retries: 0,
  outputDir: '../../../../../../test-results/native-ingress',
  use: {
    baseURL: 'http://127.0.0.1:4173',
    headless: true,
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'node src/app/home/bots/components/native-ingress/native-ingress.vite.mjs',
    cwd: process.cwd(),
    url: 'http://127.0.0.1:4173',
    reuseExistingServer: false,
    timeout: 120000,
  },
});
