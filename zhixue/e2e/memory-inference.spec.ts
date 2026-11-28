import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, rm } from 'node:fs/promises';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { expect, test } from '@playwright/test';

test('AI 整理发送单条原话并保留共享确认和来源', async ({ page }) => {
  test.setTimeout(90000);
  page.setDefaultTimeout(12000);
  const data = await mkdtemp(join(tmpdir(), 'zhixue-memory-inference-'));
  const original = '我长期更喜欢先看单色试印，再自己练习。';
  const suggested = '先示范单色试印，再安排独立练习。';
  const calls: string[] = [];
  const provider = createServer(async (request, response) => {
    const parts: Uint8Array[] = [];
    for await (const part of request) parts.push(part);
    calls.push(JSON.parse(Buffer.concat(parts).toString('utf8')).messages.at(-1).content);
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({ choices: [{ message: { content: JSON.stringify({ text: suggested }) } }],
      usage: { prompt_tokens: 27, completion_tokens: 14 } }));
  });
  provider.listen(0, '127.0.0.1');
  await once(provider, 'listening');
  const bind = provider.address();
  if (!bind || typeof bind === 'string') throw new Error('本机模拟模型未启动');

  const python = process.env.ZHIXUE_TEST_PYTHON ?? resolve(
    process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python',
  );
  const service = spawn(python, ['-m', 'backend.launch', '--port', '0', '--stdio', '--data', data], {
    stdio: ['pipe', 'pipe', 'inherit'], windowsHide: true,
  });
  const stdout = createInterface({ input: service.stdout });
  try {
    const [address] = await Promise.race([
      once(stdout, 'line'), once(service, 'exit').then(([code]) => { throw new Error('本机服务提前退出 ' + code); }),
    ]);
    const launch = new URL(address);
    const token = new URLSearchParams(launch.hash.split('?')[1]).get('session');
    async function api(path: string, method: string, payload: object) {
      const response = await page.request.fetch(launch.origin + '/api' + path, {
        method, data: payload, headers: { Authorization: 'Bearer ' + token },
      });
      expect(response.ok(), await response.text()).toBeTruthy();
      return response.json();
    }
    await page.goto(address);
    await page.getByRole('button', { name: '使用此位置', exact: true }).click();
    await page.getByRole('button', { name: '稍后配置模型', exact: true }).click();
    const connection = await api('/models/connections', 'POST', {
      name: '记忆整理回环模型', endpoint: 'http://127.0.0.1:' + bind.port + '/v1',
      model: 'memory-fixture', provider: 'custom', protocol: 'chat-completions',
    });
    await api('/models/roles', 'PATCH', { main: connection.id });
    const thread = await api('/conversations', 'POST', { id: crypto.randomUUID(), message: { id: crypto.randomUUID(), text: original } });
    await page.goto(launch.origin + '/#/chat/' + thread.id);

    const open = page.getByRole('button', { name: 'AI 整理记忆', exact: true });
    const dialog = page.getByRole('dialog', { name: 'AI 整理记忆', exact: true });
    await open.click();
    await dialog.getByRole('button', { name: '预览发送范围', exact: true }).click();
    await expect(dialog.getByText(original, { exact: true })).toBeVisible();
    await expect(dialog.getByText('记忆整理回环模型 · memory-fixture', { exact: true })).toBeVisible();
    await dialog.getByRole('button', { name: '关闭', exact: true }).click();
    expect(calls).toHaveLength(0);

    await open.click();
    await dialog.getByRole('button', { name: '预览发送范围', exact: true }).click();
    await dialog.getByRole('button', { name: '确认整理', exact: true }).click();
    await page.getByRole('link', { name: '查看整理记录', exact: true }).click();
    const tabs = page.getByRole('group', { name: '记忆范围' });
    await tabs.getByRole('button', { name: '待确认', exact: true }).click();
    await expect(page.getByText(suggested, { exact: true })).toBeVisible();
    await expect(page.getByText('AI 推断', { exact: true })).toBeVisible();
    await page.getByText('查看来源与范围', { exact: true }).click();
    await expect(page.getByText(original, { exact: true })).toBeVisible();
    expect(calls).toHaveLength(1);
    expect(JSON.parse(calls[0].split('原话 JSON\n')[1])).toEqual({ text: original });
    await tabs.getByRole('button', { name: '共享记忆', exact: true }).click();
    await expect(page.getByText(suggested, { exact: true })).toHaveCount(0);
  } finally {
    stdout.close();
    if (service.exitCode === null && service.signalCode === null) {
      const stopped = once(service, 'exit');
      service.stdin.end();
      await stopped;
    }
    await new Promise<void>(done => provider.close(() => done()));
    await rm(data, { recursive: true, force: true, maxRetries: 4, retryDelay: 180 });
  }
});