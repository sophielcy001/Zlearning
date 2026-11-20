import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: 'e2e',
  use: { channel: 'chrome', viewport: { width: 1440, height: 940 } },
});
