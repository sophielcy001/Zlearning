import type { AddressInfo } from 'node:net';
import { expect, test } from '@playwright/test';
import { createServer } from 'vite';

test('a replacement pairing gets its own reminder check and actions', async ({ page }) => {
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
      let checks = 0;
      const calls: { op: string; room?: string }[] = [];
      Reflect.set(window, 'reminderCalls', calls);
      Reflect.set(window, 'reminderFixture', async (command: { op: string }, room?: string) => {
        calls.push({ op: command.op, room });
        if (command.op === 'reminder-action') return {};
        const number = ++checks;
        if (number === 1) await new Promise<void>(resolve => {
          window.addEventListener('release-original-reminders', () => resolve(), { once: true });
          document.documentElement.dataset.heldReminder = 'yes';
        });
        return {
          day: '2026-11-20', nextCheckSeconds: null,
          reminders: [{
            kind: 'lesson', id: 'appointment-' + number, version: 1, lessonId: 'lesson',
            title: number === 1 ? '旧配对预约' : '新配对预约',
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
    await page.evaluate(() => {
      Reflect.get(window, 'renderReminderPair')('replacement-room');
      window.dispatchEvent(new Event('release-original-reminders'));
    });
    const replacement = page.getByRole('region', { name: '固定课程提醒 新配对预约' });
    await expect(page.getByText(/旧配对预约/)).toHaveCount(0);
    await expect(replacement).toBeVisible();
    await replacement.getByRole('button', { name: '30 分钟后' }).click();
    const calls = await page.evaluate(() => Reflect.get(window, 'reminderCalls'));
    expect(calls[0]).toEqual({ op: 'reminder-check', room: 'original-room' });
    expect(calls).toContainEqual({ op: 'reminder-check', room: 'replacement-room' });
    expect(calls).toContainEqual({ op: 'reminder-action', room: 'replacement-room' });
  } finally {
    await vite.close();
  }
});
