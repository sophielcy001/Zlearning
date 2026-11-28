import type { AddressInfo } from 'node:net';
import { expect, test } from '@playwright/test';
import { createServer } from 'vite';

test('a replacement pairing cannot recover a reminder delivery from the previous pairing and can retry its own', async ({ page }) => {
  test.setTimeout(60000);
  const fixture = '\0reminder-pair-fixture';
  const vite = await createServer({
    configFile: false,
    server: { host: '127.0.0.1', port: 0, hmr: false },
    plugins: [{
      name: 'reminder-pair-fixture',
      resolveId(id) { return id === '/reminder-pair-fixture.js' ? fixture : undefined; },
      load(id) {
        if (id !== fixture) return;
        return `
          import { createElement } from 'react';
          import { createRoot } from 'react-dom/client';
          import { PhoneReminders } from '/src/reminders.tsx';
          const root = createRoot(document.getElementById('fixture'));
          const navigation = { reviews() {}, lesson() {} };
          window.renderReminderPair = room => root.render(createElement(PhoneReminders, {
            workspace: 'same-workspace', room, connected: true, navigation,
          }));
          window.renderReminderPair('original-room');
        `;
      },
    }],
  });
  try {
    await vite.listen();
    const { port } = vite.httpServer!.address() as AddressInfo;
    await page.addInitScript(() => {
      let originalDelivery: string | undefined;
      let replacementChecks = 0;
      const calls: { op: string; room?: string; delivery?: string }[] = [];
      Reflect.set(window, 'reminderCalls', calls);
      Reflect.set(window, 'reminderFixture', async (command: { op: string; delivery?: string }, room?: string) => {
        calls.push({ op: command.op, room, delivery: command.delivery });
        if (command.op === 'reminder-action') return {};
        if (room === 'original-room') {
          originalDelivery = command.delivery;
          await new Promise<void>(resolve => {
            window.addEventListener('release-original-reminders', () => resolve(), { once: true });
            document.documentElement.dataset.heldReminder = 'yes';
          });
        } else if (++replacementChecks === 1) {
          // The server handled the first check but its response never reached the page.
          throw new Error('fixture lost the replacement response');
        }
        const previous = command.delivery === originalDelivery;
        return {
          day: '2026-11-20', nextCheckSeconds: null,
          reminders: [{
            kind: 'lesson', id: previous ? 'previous-appointment' : 'current-appointment', version: 1, lessonId: 'lesson',
            title: previous ? '旧配对预约' : '新配对预约',
            projectTopic: '测试课', slotDay: '2026-11-20', startsAt: '08:00',
          }],
        };
      });
    });
    await page.route('**/reminder-pair-fixture.html', route => route.fulfill({
      contentType: 'text/html',
      body: '<!doctype html><html><body><div id="fixture"></div><script type="module" src="/reminder-pair-fixture.js"></script></body></html>',
    }));
    await page.route('**/src/mobile-data.ts*', route => route.fulfill({
      contentType: 'application/javascript',
      body: 'export function mobileRequest(command, room) { return window.reminderFixture(command, room); }',
    }));
    await page.goto('http://127.0.0.1:' + port + '/reminder-pair-fixture.html');
    await expect(page.locator('html')).toHaveAttribute('data-held-reminder', 'yes');
    await page.evaluate(() => Reflect.get(window, 'renderReminderPair')('replacement-room'));
    const checks = () => page.evaluate(() => Reflect.get(window, 'reminderCalls').filter(
      (call: { op: string }) => call.op === 'reminder-check',
    ));
    await expect.poll(async () => (await checks()).length).toBeGreaterThanOrEqual(2);
    const [original, replacement] = await checks();
    expect(original.delivery).toEqual(expect.any(String));
    expect(replacement.delivery).toEqual(expect.any(String));
    expect(replacement.delivery).not.toBe(original.delivery);

    await page.evaluate(() => window.dispatchEvent(new Event('release-original-reminders')));
    await expect.poll(async () => (await checks()).length).toBeGreaterThanOrEqual(3);
    const retry = (await checks())[2];
    expect(retry).toEqual(replacement);

    const appointment = page.getByRole('region', { name: '固定课程提醒 新配对预约' });
    await expect(appointment).toBeVisible();
    await expect(page.getByText(/旧配对预约/)).toHaveCount(0);
    await appointment.getByRole('button', { name: '30 分钟后' }).click();
    const calls = await page.evaluate(() => Reflect.get(window, 'reminderCalls'));
    expect(calls).toContainEqual({ op: 'reminder-action', room: 'replacement-room', delivery: undefined });
  } finally {
    await vite.close();
  }
});
