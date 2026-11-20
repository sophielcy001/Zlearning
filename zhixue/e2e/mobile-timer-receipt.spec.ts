import type { AddressInfo } from 'node:net';
import { expect, test } from '@playwright/test';
import { createServer } from 'vite';

test('a timer acknowledgement from a removed pairing leaves the original outbox intact', async ({ page }) => {
  const vite = await createServer({
    configFile: false,
    server: { host: '127.0.0.1', port: 0, hmr: false },
  });
  try {
    await vite.listen();
    const { port } = vite.httpServer!.address() as AddressInfo;
    await page.route('**/src/mobile-data.ts*', route => route.fulfill({
      contentType: 'application/javascript',
      body: `
        export function studyKey(kind, workspace, id) { return [workspace, kind, id].join(':'); }
        export function mobileRequest(command, room) { return window.timerReceiptFixture(command, room); }
      `,
    }));
    await page.goto('http://127.0.0.1:' + port + '/zhixue.png');
    const result = await page.evaluate(async () => {
      const storePath = '/src/mobile-store.ts';
      const timerPath = '/src/mobile-timer.ts';
      const { PairingChanged, phonePair, savePair } = await import(storePath) as typeof import('../src/mobile-store');
      const { saveTimerAction, synchronizeTimers, timerLedger } = await import(timerPath) as typeof import('../src/mobile-timer');
      const key = await crypto.subtle.importKey('raw', new Uint8Array(32), 'HKDF', false, ['deriveKey']);
      const workspace = 'timer-receipt-workspace';
      const previous = { relay: location.origin, room: 'previous-room', key };
      const replacement = { ...previous, room: 'replacement-room' };
      await savePair(previous);
      const timer = await saveTimerAction({
        action: 'start', target: { workspace, lessonId: 'lesson', title: '本机计时', category: 'learning' },
      });
      await saveTimerAction({ action: 'finish', id: timer.active!.id });

      let receive = (id: string) => {};
      const received = new Promise<string>(resolve => { receive = resolve; });
      let reply = () => {};
      const held = new Promise<void>(resolve => { reply = resolve; });
      const calls: (string | undefined)[] = [];
      Reflect.set(window, 'timerReceiptFixture', async (command: import('../src/mobile-data').StudyCommand, room?: string) => {
        if (command.op === 'timer-state') {
          return { workspace, serverAt: new Date().toISOString(), dayShift: 0, active: null, pendingReview: 0 };
        }
        if (command.op !== 'timer-event') throw new Error(`Unexpected test command ${command.op}`);
        calls.push(room);
        receive(command.event.id);
        await held;
        return { id: command.event.id, state: 'saved', seconds: 0 };
      });
      const cycle = synchronizeTimers(workspace, previous.room).then(() => false, error => error instanceof PairingChanged);
      const operation = await received;
      const awaitingReceipt = (await timerLedger()).events;
      await savePair(undefined, previous.room);
      await savePair(replacement);
      reply();
      const stoppedAfterReplacement = await cycle;
      return {
        operation, awaitingReceipt, finalQueue: (await timerLedger()).events, stoppedAfterReplacement,
        currentRoom: (await phonePair())?.room, calls,
      };
    });
    expect(result.currentRoom).toBe('replacement-room');
    expect(result.stoppedAfterReplacement).toBe(true);
    expect(result.calls).toEqual(['previous-room']);
    expect(result.awaitingReceipt.map(event => event.action)).toEqual(['start', 'finish']);
    expect(result.awaitingReceipt[0].id).toBe(result.operation);
    expect(result.finalQueue).toEqual(result.awaitingReceipt);
  } finally {
    await vite.close();
  }
});
