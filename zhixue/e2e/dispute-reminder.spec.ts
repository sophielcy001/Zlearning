import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { expect, test, type Page } from '@playwright/test';

async function withReminderFixture(page: Page, phoneFirst: boolean, verify: () => Promise<void>) {
  const folder = await mkdtemp(join(tmpdir(), 'zhixue-dispute-reminder-'));
  const python = process.env.ZHIXUE_TEST_PYTHON ?? resolve(
    process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python',
  );
  const args = ['-m', 'e2e.dispute_reminder_service', folder];
  if (phoneFirst) args.push('--phone-first');
  const service = spawn(python, args, { stdio: ['pipe', 'pipe', 'inherit'], windowsHide: true });
  const stdout = createInterface({ input: service.stdout });
  try {
    const [address] = await Promise.race([
      once(stdout, 'line'), once(service, 'exit').then(([code]) => { throw new Error('提醒样本服务提前退出 ' + code); }),
    ]);
    await page.goto(address);
    await verify();
  } finally {
    stdout.close();
    if (service.exitCode === null && service.signalCode === null) {
      const closed = once(service, 'exit');
      service.stdin.end();
      await closed;
    }
    await rm(folder, { recursive: true, force: true, maxRetries: 4, retryDelay: 180 });
  }
}

test('手机先领到期复习后，电脑仍可处理课程争议', async ({ page }) => {
  await withReminderFixture(page, true, async () => {
    const summary = page.getByRole('region', { name: '每日学习提醒', exact: true });
    await expect(summary.getByRole('status')).toContainText('3 项争议待处理');
    await expect(summary.getByRole('link', { name: '查看到期复习' })).toHaveCount(0);
    await summary.getByRole('link', { name: '查看待处理争议' }).click();
    await expect(page.getByRole('heading', { name: '争议待处理', exact: true })).toBeVisible();
    await expect(page.getByText('原文只涉及版面检查。', { exact: true }).first()).toBeVisible();
  });
});

test('电脑合并显示复习与争议，一次稍后提醒保存两项', async ({ page }) => {
  await withReminderFixture(page, false, async () => {
    const summary = page.getByRole('region', { name: '每日学习提醒', exact: true });
    await expect(summary).toHaveCount(1);
    await expect(summary.getByRole('status')).toContainText('2 项复习到期 · 3 项争议待处理');
    const saved = page.waitForResponse(response => response.url().endsWith('/action') && response.request().method() === 'POST');
    await summary.getByRole('button', { name: '30 分钟后', exact: true }).click();
    const response = await saved;
    expect(response.ok()).toBeTruthy();
    expect(response.request().postDataJSON()).toHaveProperty('disputeReceipt');
    await expect(summary).toHaveCount(0);
    await page.reload();
    await expect(page.getByRole('heading', { name: '今天，想弄懂什么？', exact: true })).toBeVisible();
    const check = await page.evaluate(async () => {
      const token = sessionStorage.getItem('zhixue-session');
      const response = await fetch('/api/reminders/check', {
        method: 'POST', headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
        body: JSON.stringify({ trigger: 'foreground' }),
      });
      if (!response.ok) throw new Error('无法读取当前提醒');
      return response.json();
    });
    expect(check.reminders).toEqual([]);
    expect(check.nextCheckSeconds).toBeGreaterThan(1760);
  });
});
