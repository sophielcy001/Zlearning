import type { AddressInfo } from 'node:net';
import { expect, test } from '@playwright/test';
import { createServer } from 'vite';

test('reconnection finishes a push removal that started offline', async ({ page }) => {
  test.setTimeout(90000);
  const fixture = '\0push-removal-fixture';
  const vite = await createServer({
    configFile: false,
    server: { host: '127.0.0.1', port: 0, hmr: false },
    plugins: [{
      name: 'push-removal-fixture',
      resolveId(id) { return id === '/push-removal-fixture.js' ? fixture : undefined; },
      load(id) {
        if (id !== fixture) return;
        return [
          "import { synchronizePushRemoval } from '/src/mobile-push.tsx';",
          "import { localDatabase, phonePushState, queuePushRemoval, savePair } from '/src/mobile-store.ts';",
          "window.pushFixture = { synchronizePushRemoval, localDatabase, phonePushState, queuePushRemoval, savePair };",
        ].join('\n');
      },
    }],
  });
  try {
    await vite.listen();
    const { port } = vite.httpServer!.address() as AddressInfo;
    await page.addInitScript(() => {
      const calls: unknown[] = [];
      const endpoint = 'https://fcm.googleapis.com/fcm/send/isolated-removal-fixture';
      let subscribed = true;
      let release!: () => void;
      const gate = new Promise<void>(resolve => { release = resolve; });
      let removals = 0;
      const subscription = {
        endpoint,
        async unsubscribe() {
          removals++;
          document.documentElement.dataset.removing = 'yes';
          await gate;
          subscribed = false;
          return true;
        },
      };
      const registration = { pushManager: { getSubscription: async () => subscribed ? subscription : null } };
      Object.defineProperty(navigator, 'serviceWorker', {
        value: { getRegistration: async () => registration },
      });
      Reflect.set(window, 'releasePushRemoval', release);
      Reflect.set(window, 'pushTestState', () => ({ calls, removals }));
      Reflect.set(window, 'pushRelayFixture', (command: object, room: string) => {
        calls.push({ command, room });
        return { registered: true, endpointId: 'c'.repeat(64) };
      });
    });
    await page.route('**/push-removal-fixture.html', route => route.fulfill({
      contentType: 'text/html',
      body: '<!doctype html><html><script type="module" src="/push-removal-fixture.js"></script></html>',
    }));
    await page.route('**/src/mobile-data.ts*', route => route.fulfill({
      contentType: 'application/javascript',
      body: 'export function mobileRequest(command, room) { return window.pushRelayFixture(command, room); } export function studyKey() { throw new Error("The removal fixture does not access course storage."); }',
    }));
    await page.goto('http://127.0.0.1:' + port + '/push-removal-fixture.html');
    await page.waitForFunction(() => !!Reflect.get(window, 'pushFixture'), undefined, { timeout: 10000 });
    const endpointId = await page.evaluate(async () => {
      const fixture = Reflect.get(window, 'pushFixture');
      const endpoint = 'https://fcm.googleapis.com/fcm/send/isolated-removal-fixture';
      const key = new TextEncoder().encode(endpoint);
      const digest = await crypto.subtle.digest('SHA-256', key);
      const id = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
      if (!await fixture.savePair({ room: 'original-room' })) throw new Error('Unable to save the fixture pairing');
      // Simulate the format written by an installed older phone build.
      const db = await fixture.localDatabase();
      await new Promise<void>((resolve, reject) => {
        const tx = db.transaction('device', 'readwrite');
        tx.objectStore('device').put(id, 'push-removal:original-room');
        tx.oncomplete = () => resolve();
        tx.onabort = () => reject(tx.error);
      });
      Reflect.set(window, 'offlineRemoval', fixture.synchronizePushRemoval('original-room'));
      return id;
    });
    await expect(page.locator('html')).toHaveAttribute('data-removing', 'yes');
    const result = await page.evaluate(async () => {
      const fixture = Reflect.get(window, 'pushFixture');
      // Connection and the open settings panel can both request the same synchronization.
      const reconnect = fixture.synchronizePushRemoval('original-room', 'same-workspace');
      const settings = fixture.synchronizePushRemoval('original-room', 'same-workspace');
      Reflect.get(window, 'releasePushRemoval')();
      await Promise.all([Reflect.get(window, 'offlineRemoval'), reconnect, settings]);
      return { ...Reflect.get(window, 'pushTestState')(), saved: await fixture.phonePushState('original-room') };
    });
    expect(result.calls).toEqual([{
      command: { op: 'push-unsubscribe', workspace: 'same-workspace', endpointId },
      room: 'original-room',
    }]);
    expect(result.removals).toBe(1);
    expect(result.saved).toEqual({ endpointId: 'c'.repeat(64), pending: [] });
    const partial = await page.evaluate(async () => {
      const fixture = Reflect.get(window, 'pushFixture');
      const room = 'original-room';
      const localId = 'b'.repeat(64), previous = 'c'.repeat(64), newer = 'd'.repeat(64);
      const calls: string[] = [];
      let replyLost = false;
      Reflect.set(window, 'pushRelayFixture', (request: { endpointId: string }) => {
        calls.push(request.endpointId);
        if (request.endpointId === previous && !replyLost) {
          replyLost = true;
          throw new Error('simulated lost removal response');
        }
        return { registered: true, endpointId: request.endpointId === localId ? previous : newer };
      });
      if (!await fixture.queuePushRemoval(room, localId)) throw new Error('Unable to queue a dual removal');
      try {
        await fixture.synchronizePushRemoval(room, 'same-workspace');
        throw new Error('Expected the lost response');
      } catch (error) {
        if (!(error instanceof Error) || error.message !== 'simulated lost removal response') throw error;
      }
      const afterLoss = await fixture.phonePushState(room);
      await fixture.synchronizePushRemoval(room, 'same-workspace');
      return { calls, afterLoss, saved: await fixture.phonePushState(room) };
    });
    expect(partial.calls).toEqual(['b'.repeat(64), 'c'.repeat(64), 'c'.repeat(64)]);
    expect(partial.afterLoss).toEqual({ endpointId: 'c'.repeat(64), pending: ['c'.repeat(64)] });
    expect(partial.saved).toEqual({ endpointId: 'd'.repeat(64), pending: [] });
  } finally {
    await vite.close();
  }
});

