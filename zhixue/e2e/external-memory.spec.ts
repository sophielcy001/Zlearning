import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { expect, test } from '@playwright/test';

test('逐条外发记忆并在本地删除后重试外部同步', async ({ page }) => {
  test.setTimeout(90000);
  page.setDefaultTimeout(10000);
  const data = await mkdtemp(join(tmpdir(), 'zhixue-external-memory-'));
  const memory = '我想先完成自检，再看讲解。';
  const python = process.env.ZHIXUE_TEST_PYTHON ?? resolve(
    process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python',
  );
  const service = spawn(python, ['-m', 'e2e.external_memory_service', data], {
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
    const draft = await api('/memories', 'POST', { id: crypto.randomUUID(), text: memory });
    await api('/memories/review', 'POST', { action: 'accept', items: [{ id: draft.id, revision: draft.revision }] });
    await page.getByRole('link', { name: '管理中心', exact: true }).click();
    await page.getByRole('link', { name: '记忆', exact: true }).click();
    await expect(page.getByRole('button', { name: '发送到 Mem0', exact: true })).toHaveCount(0);
    await page.getByRole('button', { name: '配置外接记忆', exact: true }).click();
    const settings = page.getByRole('dialog', { name: '配置外接记忆', exact: true });
    await settings.getByLabel('Mem0 API 密钥', { exact: true }).fill('fixture-external-token');
    await settings.getByRole('checkbox', { name: '允许外发共享记忆', exact: true }).check();
    await settings.getByRole('button', { name: '确认保存外接设置', exact: true }).click();

    await page.getByRole('button', { name: '发送到 Mem0', exact: true }).click();
    const disclosure = page.getByRole('dialog', { name: '发送记忆到 Mem0', exact: true });
    await disclosure.getByRole('button', { name: '预览发送范围', exact: true }).click();
    await expect(disclosure.getByText('https://api.mem0.ai', { exact: true })).toBeVisible();
    await expect(disclosure.getByText(memory, { exact: true })).toBeVisible();
    await disclosure.getByRole('button', { name: '关闭', exact: true }).click();
    expect((await api('/external-memory')).copies).toEqual([]);

    await page.getByRole('button', { name: '发送到 Mem0', exact: true }).click();
    await disclosure.getByRole('button', { name: '预览发送范围', exact: true }).click();
    await disclosure.getByRole('button', { name: '确认发送记忆', exact: true }).click();
    await expect(page.getByText('已保存到 Mem0', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '删除', exact: true }).click();
    const deletion = page.getByRole('dialog', { name: '删除记忆', exact: true });
    await expect(deletion.getByText('同步删除外部副本', { exact: false })).toBeVisible();
    await deletion.getByRole('button', { name: '确认删除', exact: true }).click();
    await expect(page.getByText(memory, { exact: true })).toHaveCount(0);
    await expect(page.getByText('待同步删除', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '重试同步删除', exact: true }).click();
    await expect(page.getByText('外部副本已删除', { exact: true })).toBeVisible();
    expect((await api('/external-memory')).copies[0].state).toBe('removed');
  } finally {
    stdout.close();
    if (service.exitCode === null && service.signalCode === null) {
      const stopped = once(service, 'exit');
      service.stdin.end();
      await stopped;
    }
    await rm(data, { recursive: true, force: true, maxRetries: 4, retryDelay: 180 });
  }
});
