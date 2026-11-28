import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { expect, test } from '@playwright/test';

const fixedClock = [
  'from datetime import UTC, datetime',
  'from backend import launch, library_metadata',
  'class FixedClock:',
  '    @classmethod',
  '    def now(cls, timezone):',
  '        return datetime(2026, 11, 21, 2, 38, 54, 443324, tzinfo=UTC)',
  'library_metadata.datetime = FixedClock',
  'raise SystemExit(launch.main())',
].join('\n');

test('最近访问在同一时间戳下按打开顺序排列', async ({ page }) => {
  test.setTimeout(60000);
  const workspace = await mkdtemp(join(tmpdir(), 'zhixue-library-recent-'));
  const python = process.env.ZHIXUE_TEST_PYTHON ?? resolve(
    process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python',
  );
  const service = spawn(python, ['-c', fixedClock, '--port', '0', '--stdio', '--data', workspace], {
    stdio: ['pipe', 'pipe', 'inherit'], windowsHide: true,
  });
  const stdout = createInterface({ input: service.stdout });
  try {
    const [address] = await Promise.race([
      once(stdout, 'line'),
      once(service, 'exit').then(([code]) => { throw new Error('本机服务结束 ' + code); }),
    ]);
    const url = new URL(address);
    const session = new URLSearchParams(url.hash.split('?')[1]).get('session');
    await page.goto(address);
    await page.getByRole('button', { name: '使用此位置', exact: true }).click();
    await page.getByRole('button', { name: '稍后配置模型', exact: true }).click();
    const phrase = '同钟关键词';
    const first = 'A-并列.md';
    const second = 'Z-并列.md';
    for (const [name, suffix] of [[first, '甲'], [second, '乙']]) {
      const response = await page.request.post(url.origin + '/api/files', {
        headers: { Authorization: 'Bearer ' + session }, data: { name, content: phrase + suffix },
      });
      expect(response.ok(), await response.text()).toBeTruthy();
    }

    const library = page.getByRole('link', { name: '知识库', exact: true });
    await library.click();
    const rows = page.locator('.library-list .library-file');
    await expect(rows.locator('strong')).toHaveText([first, second]);
    const times = [];
    for (const name of [first, second]) {
      const opened = page.waitForResponse(response => response.url().endsWith('/opened') && response.ok());
      await rows.getByText(name, { exact: true }).click();
      times.push((await (await opened).json()).lastViewedAt);
    }
    expect(times[0]).toBe(times[1]);
    await page.getByRole('combobox', { name: '文件视图', exact: true }).selectOption('recent');
    await expect(rows.locator('strong')).toHaveText([second, first]);

    await page.getByRole('searchbox', { name: '搜索知识库', exact: true }).fill(phrase);
    await expect(page.getByRole('heading', { name: '搜索结果', exact: true })).toBeVisible();
    await expect(rows.locator('strong')).toHaveText([second, first]);
  } finally {
    stdout.close();
    if (service.exitCode === null && service.signalCode === null) {
      const stopped = once(service, 'exit');
      service.stdin.end();
      await stopped;
    }
    await rm(workspace, { recursive: true, force: true, maxRetries: 4, retryDelay: 180 });
  }
});