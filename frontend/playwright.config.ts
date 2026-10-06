import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './tests',
  fullyParallel: false,
  workers: 1,
  use: { baseURL: process.env.E2E_BASE_URL || 'http://localhost:8080', headless: true, viewport: { width: 1440, height: 1000 } },
  reporter: 'list',
});
