import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { expect, test } from '@playwright/test';

test('从学习报告的薄弱知识点做独立题组并保留正式安排', async ({ page }) => {
  test.setTimeout(90000);
  page.setDefaultTimeout(12000);
  const folder = await mkdtemp(join(tmpdir(), 'zhixue-weak-practice-'));
  const python = process.env.ZHIXUE_TEST_PYTHON ?? resolve(
    process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python',
  );
  const service = spawn(python, ['-m', 'backend.launch', '--port', '0', '--stdio', '--data', folder], {
    stdio: ['pipe', 'pipe', 'inherit'], windowsHide: true,
  });
  const stdout = createInterface({ input: service.stdout });
  try {
    const [address] = await Promise.race([
      once(stdout, 'line'), once(service, 'exit').then(([code]) => { throw new Error('样本服务提前退出 ' + code); }),
    ]);
    const url = new URL(address);
    const token = new URLSearchParams(url.hash.split('?')[1]).get('session');
    async function api(path: string, method = 'GET', payload?: object) {
      const response = await page.request.fetch(url.origin + '/api' + path, {
        method, data: payload, headers: { Authorization: 'Bearer ' + token },
      });
      expect(response.ok(), await response.text()).toBeTruthy();
      return response.json();
    }

    await page.goto(url.href);
    await page.getByRole('button', { name: '使用此位置', exact: true }).click();
    await page.getByRole('button', { name: '稍后配置模型', exact: true }).click();
    const project = await api('/projects', 'POST', {
      id: crypto.randomUUID(), topic: 'SQLite 入门', outcome: '从学习记录中区分行与列',
    });
    await expect.poll(async () => (await api('/projects/' + project.id)).task.status).toBe('ready');
    const lesson = '/lessons/' + project.lessons[0].id;
    await api(lesson + '/learned', 'PUT', { learned: true });
    await api('/demo/next-day', 'POST');
    const formal = lesson + '/reviews/rows-columns';
    await api(formal + '/submit', 'POST', { round: 1, answers: [], action: 'unknown' });
    const recorded = await api(formal);

    await page.getByRole('link', { name: '学习报告', exact: true }).click();
    await page.getByRole('button', { name: '月', exact: true }).click();
    const beforeUrl = page.url();
    await page.getByRole('link', { name: '专项练习 区分行与列', exact: true }).click();
    await expect(page.getByRole('heading', { name: '区分行与列', exact: true })).toBeVisible();
    await expect(page.getByText('正式复习日期沿用原安排。', { exact: false })).toBeVisible();
    await page.getByText('查看原讲解', { exact: true }).click();
    await expect(page.getByText('一行对应一条学习记录；一列对应所有记录共有的一类属性。', { exact: true })).toBeVisible();
    await page.getByRole('radio', { name: '在原有两列下增加一条记录', exact: true }).check();
    await page.getByRole('radio', { name: '各行的日期列', exact: true }).check();
    await page.getByRole('button', { name: '提交专项练习', exact: true }).click();
    await expect(page.getByRole('heading', { name: '专项练习已完成', exact: true })).toBeVisible();
    await expect(page.getByText('答对 2 / 2 题', { exact: true })).toBeVisible();
    await page.reload();
    await expect(page.getByRole('heading', { name: '专项练习已完成', exact: true })).toBeVisible();
    await page.getByRole('link', { name: '返回学习报告', exact: true }).click();
    await expect(page).toHaveURL(beforeUrl);
    await expect(page.getByRole('link', { name: '专项练习 区分行与列', exact: true })).toBeVisible();
    expect(await api(formal)).toEqual(recorded);
  } finally {
    stdout.close();
    if (service.exitCode === null && service.signalCode === null) {
      const closed = once(service, 'exit');
      service.stdin.end();
      await closed;
    }
    await rm(folder, { recursive: true, force: true, maxRetries: 4, retryDelay: 180 });
  }
});
