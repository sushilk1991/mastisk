import { defineConfig } from '@playwright/test';

// Exercise real SVG/canvas layout in Chromium without touching the live wiki.
const TEST_PORT = 4174;

export default defineConfig({
  testDir: './tests',
  outputDir: './test-results',
  fullyParallel: false,
  workers: 1,
  use: { baseURL: `http://127.0.0.1:${TEST_PORT}`, headless: true },
  webServer: {
    command: `npm run dev -- --host 127.0.0.1 --port ${TEST_PORT} --strictPort`,
    url: `http://127.0.0.1:${TEST_PORT}/tests/renderer.html`,
    reuseExistingServer: false,
  },
});
