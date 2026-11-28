import type { AddressInfo } from 'node:net';
import { expect, test } from '@playwright/test';
import { createServer } from 'vite';

const clockTest = test.extend<{ timerOrigin: string }>({
  timerOrigin: async ({ page }, use) => {
    const vite = await createServer({ configFile: false, server: { host: '127.0.0.1', port: 0, hmr: false } });
    try {
      await vite.listen();
      const { port } = vite.httpServer!.address() as AddressInfo;
      const origin = 'http://127.0.0.1:' + port;
      await page.route('**/src/mobile-data.ts*', route => route.fulfill({
        contentType: 'application/javascript',
        body: `
          export function studyKey(kind, workspace, id) { return [workspace, kind, id].join(':'); }
          export function mobileRequest(command, room) { return window.timerClockFixture(command, room); }
        `,
      }));
      await page.route(origin + '/clock-fixture.html', route => route.fulfill({
        contentType: 'text/html', body: '<!doctype html><title>Clock fixture</title>',
      }));
      await page.goto(origin + '/clock-fixture.html');
      await use(origin);
    } finally { await vite.close(); }
  },
});

for (const [scenario, title] of [
  ['recovered', 'a second clock reading rescues reliable offline spans after one slow reply'],
  ['slow', 'two slow clock readings still leave unverified spans for review'],
  ['changed', 'a fresh reading detects a real clock change and keeps the span for review'],
  ['unavailable', 'a failed resample leaves every unsent operation in the local queue'],
  ['replayed', 'an operation already sent keeps its original payload and bypasses resampling'],
] as const) {
  clockTest(title, async ({ page, timerOrigin }, info) => {
    test.setTimeout(65000);
    const outcome = await page.evaluate(async ({ scenario, origin }) => {
      let wall = Date.parse('2026-11-20T15:57:00Z');
      let monotonic = 0;
      const advance = (milliseconds: number) => { wall += milliseconds; monotonic += milliseconds; };
      Date.now = () => wall;
      Object.defineProperty(performance, 'now', { value: () => monotonic });

      const storePath = '/src/mobile-store.ts';
      const timerPath = '/src/mobile-timer.ts';
      const { savePair } = await import(storePath) as typeof import('../src/mobile-store');
      const { saveTimerAction, synchronizeTimers, timerLedger } = await import(timerPath) as typeof import('../src/mobile-timer');
      type Event = import('../src/mobile-timer').PhoneTimerEvent;
      type Command = import('../src/mobile-data').StudyCommand;
      const workspace = 'clock-calibration-workspace';
      const room = 'clock-fixture-room';
      const key = await crypto.subtle.importKey('raw', new Uint8Array(32), 'HKDF', false, ['deriveKey']);
      await savePair({ relay: origin, room, key });
      let offset = 800;
      let samples: number[] = [];
      let calls: string[] = [];
      let received: Event[] = [];
      let held = 0;
      let losePause = false;
      Reflect.set(window, 'timerClockFixture', async (command: Command, calledRoom?: string) => {
        if (calledRoom !== room) throw new Error('Clock fixture pairing changed');
        if (command.op === 'timer-state') {
          const delay = samples.shift() ?? 0;
          if (delay === -1) {
            calls.push('clock:failed');
            throw new Error('Fixture clock could not be read');
          }
          const midpoint = wall + delay / 2;
          advance(delay);
          calls.push('clock:' + delay);
          return { workspace, serverAt: new Date(midpoint + offset).toISOString(), dayShift: 0, active: null, pendingReview: held };
        }
        if (command.op !== 'timer-event') throw new Error('Unexpected clock fixture command: ' + command.op);
        const event = command.event;
        calls.push(event.action);
        received.push(event);
        if (event.action === 'pause' && losePause) throw new Error('Fixture pause receipt was lost');
        if (event.span && !event.span.reliable) {
          held += 1;
          return { id: event.id, state: 'held', seconds: 0 };
        }
        return { id: event.id, state: 'saved', seconds: 0 };
      });
      await synchronizeTimers(workspace, room);
      const timer = await saveTimerAction({ action: 'start', target: {
        workspace, lessonId: 'lesson', title: '离线复习计时', category: 'review', reviewKey: 'due-review',
      } });
      const timerId = timer.active!.id;
      advance(4000);
      await saveTimerAction({ action: 'pause', id: timerId });

      let previousRequest: Event | undefined;
      if (scenario === 'replayed') {
        losePause = true;
        const lost = await synchronizeTimers(workspace, room).then(() => false, error => {
          if (error.message !== 'Fixture pause receipt was lost') throw error;
          return true;
        });
        if (!lost) throw new Error('Expected only the pause receipt to be lost');
        const pending = (await timerLedger()).events;
        if (pending.length !== 1 || !pending[0].request?.span?.reliable) throw new Error('Pause payload was not persisted');
        previousRequest = pending[0].request;
        losePause = false;
        samples = [3250, 30];
      } else {
        advance(2600);
        await saveTimerAction({ action: 'resume', id: timerId });
        advance(3000);
        await saveTimerAction({ action: 'finish', id: timerId });
        if (scenario === 'recovered') samples = [2800, 32];
        if (scenario === 'slow') samples = [2800, 2900];
        if (scenario === 'unavailable') samples = [2800, -1];
        if (scenario === 'changed') { samples = [32]; offset += 6000; }
      }

      calls = [];
      received = [];
      const before = (await timerLedger()).events;
      const pendingReview = await synchronizeTimers(workspace, room).catch(error => {
        if (scenario !== 'unavailable' || error.message !== 'Fixture clock could not be read') throw error;
        return null;
      });
      return {
        calls, received, pendingReview, queue: (await timerLedger()).events, before, previousRequest,
      };
    }, { scenario, origin: timerOrigin });
    await info.attach('离线校时与操作回执', {
      body: JSON.stringify(outcome, null, 2),
      contentType: 'application/json',
    });
    const { calls, received, pendingReview, queue, previousRequest } = outcome;
    if (scenario === 'unavailable') {
      expect(calls).toEqual(['clock:2800', 'clock:failed']);
      expect(received).toEqual([]);
      expect(queue).toEqual(outcome.before);
      expect(pendingReview).toBeNull();
      return;
    }
    expect(queue).toEqual([]);
    if (scenario === 'replayed') {
      expect(calls).toEqual(['clock:3250', 'pause']);
      expect(received).toEqual([previousRequest]);
      expect(pendingReview).toBe(0);
      return;
    }
    expect(received.map(item => item.action)).toEqual(['start', 'pause', 'resume', 'finish']);
    const spans = received.flatMap(item => item.span ? [item.span] : []);
    expect(spans.map(span => Date.parse(span.endedAt) - Date.parse(span.beganAt))).toEqual([4000, 3000]);
    const expectedCalls = {
      recovered: ['clock:2800', 'clock:32', 'start', 'pause', 'resume', 'finish'],
      slow: ['clock:2800', 'clock:2900', 'start', 'pause', 'resume', 'finish', 'clock:0'],
      changed: ['clock:32', 'start', 'pause', 'resume', 'finish', 'clock:0'],
    };
    expect(calls, 'fresh clock checks must finish before an operation payload is sent').toEqual(expectedCalls[scenario]);
    expect(spans.map(span => span.reliable)).toEqual([scenario === 'recovered', scenario === 'recovered']);
    expect(pendingReview).toBe(scenario === 'recovered' ? 0 : 2);
  });
}
