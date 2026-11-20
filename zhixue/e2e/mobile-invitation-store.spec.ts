import type { AddressInfo } from 'node:net';
import { expect, test } from '@playwright/test';
import { createServer } from 'vite';

test('a delayed invitation cannot replace a pairing that another page already removed', async ({ page }) => {
  test.setTimeout(60000);
  const vite = await createServer({ configFile: false, server: { host: '127.0.0.1', port: 0, hmr: false } });
  try {
    await vite.listen();
    const { port } = vite.httpServer!.address() as AddressInfo;
    await page.goto('http://127.0.0.1:' + port + '/zhixue.png');
    const result = await page.evaluate(async () => {
      const modulePath = '/src/mobile-store.ts';
      const { phonePair, phonePairVersion, savePair } =
        await import(modulePath) as typeof import('../src/mobile-store');
      const key = await crypto.subtle.importKey('raw', new Uint8Array(32), 'HKDF', false, ['deriveKey']);
      const first = { relay: location.origin, room: 'first-room', key };
      const stale = { ...first, room: 'delayed-room' };
      const delayed = await phonePairVersion();
      await savePair(first);
      await savePair(undefined, first.room);
      const rejected = !await savePair(stale, delayed);
      const stillUnpaired = !await phonePair();
      const committed = await savePair(first, await phonePairVersion());
      return { rejected, stillUnpaired, committed, current: (await phonePair())?.room };
    });
    expect(result).toEqual({ rejected: true, stillUnpaired: true, committed: true, current: 'first-room' });
  } finally {
    await vite.close();
  }
});
