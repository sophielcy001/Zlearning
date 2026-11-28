import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, rm } from 'node:fs/promises';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { expect, test } from '@playwright/test';

test('授权专业 Agent 后自动整理本轮原话并保存在专属范围', async ({ page }) => {
  test.setTimeout(90000);
  page.setDefaultTimeout(10000);
  const data = await mkdtemp(join(tmpdir(), 'zhixue-auto-memory-'));
  const original = '我希望之后先练活页装订，再练线装。';
  const answer = '我们先从活页装订开始。';
  const suggested = '用户希望先练活页装订，再练线装。';
  const calls: string[] = [];
  const provider = createServer(async (request, response) => {
    const parts: Uint8Array[] = [];
    for await (const part of request) parts.push(part);
    const prompt: string = JSON.parse(Buffer.concat(parts).toString('utf8')).messages.at(-1).content;
    calls.push(prompt);
    const content = prompt.includes('原话 JSON\n') ? JSON.stringify({ text: suggested }) : answer;
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({ choices: [{ message: { content } }],
      usage: { prompt_tokens: 20, completion_tokens: 13 } }));
  });
  provider.listen(0, '127.0.0.1');
  await once(provider, 'listening');
  const bind = provider.address();
  if (!bind || typeof bind === 'string') throw new Error('本机回环模型未启动');

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
    const connection = await api('/models/connections', 'POST', {
      name: '装订回环模型', endpoint: 'http://127.0.0.1:' + bind.port + '/v1',
      model: 'local-binding', provider: 'custom', protocol: 'chat-completions',
    });
    await api('/models/roles', 'PATCH', { main: connection.id });

    await page.getByRole('link', { name: '管理中心', exact: true }).click();
    await page.getByRole('link', { name: '专业 Agent', exact: true }).click();
    await page.getByRole('button', { name: '新建专业 Agent', exact: true }).click();
    await page.getByLabel('需求描述', { exact: true }).fill('讲解装订');
    await page.getByRole('button', { name: '编辑配置', exact: true }).click();
    await page.getByLabel('Agent 名称', { exact: true }).fill('装订导师');
    const grant = page.getByRole('checkbox', { name: '自动整理专属记忆', exact: true });
    await expect(grant).not.toBeChecked();
    await grant.check();
    await page.getByRole('button', { name: '预览配置', exact: true }).click();
    const setup = page.getByRole('dialog', { name: '新建专业 Agent' });
    await expect(setup.getByRole('definition').filter({ hasText: '装订回环模型 · local-binding' })).toBeVisible();
    await expect(setup.getByText('仅发送本轮用户原话', { exact: false })).toBeVisible();
    expect(calls).toHaveLength(0);
    await setup.getByRole('button', { name: '确认保存 Agent', exact: true }).click();
    await expect(page.getByRole('button', { name: '编辑 装订导师', exact: true })).toBeVisible();
    const [agent] = (await api('/agents')).items;

    await page.getByRole('link', { name: '聊天', exact: true }).click();
    await page.getByRole('combobox', { name: '聊天 Agent', exact: true }).selectOption({ label: '装订导师' });
    await page.getByLabel('对话消息', { exact: true }).fill(original);
    const send = page.getByRole('button', { name: '发送消息', exact: true });
    const dialog = page.getByRole('dialog', { name: '确认聊天发送范围' });
    await send.click();
    await expect(dialog.getByText('收到回复后', { exact: false })).toBeVisible();
    await dialog.getByRole('button', { name: '关闭', exact: true }).click();
    expect(calls).toHaveLength(0);
    await send.click();
    await dialog.getByRole('button', { name: '确认发送', exact: true }).click();
    await expect(page.getByText(answer, { exact: true })).toBeVisible();

    await page.getByRole('link', { name: '管理中心', exact: true }).click();
    await page.getByRole('link', { name: '记忆', exact: true }).click();
    await page.getByRole('group', { name: '记忆范围' }).getByRole('button', { name: 'Agent 专属', exact: true }).click();
    await expect(page.getByText(suggested, { exact: true })).toBeVisible();
    await expect(page.getByText('AI 推断', { exact: true })).toBeVisible();
    await expect(page.getByText('Agent 自动整理', { exact: true })).toBeVisible();
    await page.getByText('查看来源与范围', { exact: true }).click();
    await expect(page.getByText(original, { exact: true })).toBeVisible();
    expect(calls).toHaveLength(2);
    expect(JSON.parse(calls[1].split('原话 JSON\n')[1])).toEqual({ text: original });
    expect((await api('/memories?view=private')).items[0].agentId).toBe(agent.id);
    expect((await api('/memories?view=shared')).items).toEqual([]);
    expect((await api('/memories?view=pending')).items).toEqual([]);
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