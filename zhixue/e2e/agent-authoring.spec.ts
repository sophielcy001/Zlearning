import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, rm } from 'node:fs/promises';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { expect, test } from '@playwright/test';

test('AI 起草经发送确认并进入可编辑 Agent 配置', async ({ page }) => {
  test.setTimeout(90000);
  page.setDefaultTimeout(12000);
  const data = await mkdtemp(join(tmpdir(), 'zhixue-agent-e2e-'));
  const source = [
    '---', 'name: print-checklist', 'description: 检查试印前准备。', 'allowed-tools: Read',
    '---', '# 检查版框', '先记录版框是否平整。', '',
  ].join('\n');
  const calls: string[] = [];
  const provider = createServer(async (request, response) => {
    const received: Uint8Array[] = [];
    for await (const chunk of request) received.push(chunk);
    const prompt = JSON.parse(Buffer.concat(received).toString('utf8')).messages.at(-1).content;
    calls.push(prompt);
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({ choices: [{ message: { content: prompt.includes('需求描述 JSON') ? JSON.stringify({
          name: '试印指导', responsibility: '按准备、检查及反馈组织练习。',
        }) : '试印前先检查版框。' } }],
      usage: { prompt_tokens: 20, completion_tokens: 10 } }));
  });
  provider.listen(0, '127.0.0.1');
  await once(provider, 'listening');
  const bind = provider.address();
  if (!bind || typeof bind === 'string') throw new Error('Expected localhost provider');

  const python = process.env.ZHIXUE_TEST_PYTHON ?? resolve(
    process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python',
  );
  const service = spawn(python, ['-m', 'backend.launch', '--port', '0', '--stdio', '--data', data], {
    stdio: ['pipe', 'pipe', 'inherit'], windowsHide: true,
  });
  const output = createInterface({ input: service.stdout });
  try {
    const [address] = await Promise.race([
      once(output, 'line'),
      once(service, 'exit').then(([code]) => { throw new Error('本机服务提前退出 ' + code); }),
    ]);
    const url = new URL(address);
    const session = new URLSearchParams(url.hash.split('?')[1]).get('session');
    const headers: Record<string, string> = { Authorization: 'Bearer ' + session };
    const request = async (path: string, method: string, payload: object | string, extra = {}) => {
      const result = await page.request.fetch(url.origin + '/api' + path, {
        headers: { ...headers, ...extra }, method, data: payload,
      });
      expect(result.ok(), await result.text()).toBeTruthy();
      return result.json();
    };

    await page.goto(url.href);
    await page.getByRole('button', { name: '使用此位置', exact: true }).click();
    await page.getByRole('button', { name: '稍后配置模型', exact: true }).click();
    const connection = await request('/models/connections', 'POST', {
      name: '回环练习模型', provider: 'custom', protocol: 'chat-completions',
      endpoint: 'http://127.0.0.1:' + bind.port + '/v1', model: 'local-tutor',
    });
    await request('/models/roles', 'PATCH', { main: connection.id });
    const parsed = await request('/skills/preview', 'POST', source, { 'Content-Type': 'text/markdown' });
    await request('/skills', 'POST', source, { 'Content-Type': 'text/markdown', 'X-Skill-Checksum': parsed.checksum });

    await page.getByRole('link', { name: '管理中心', exact: true }).click();
    await expect(page.getByRole('link', { name: '专业 Agent', exact: true })).toBeVisible();
    await page.getByRole('link', { name: '专业 Agent', exact: true }).click();
    await page.getByRole('button', { name: '新建专业 Agent', exact: true }).click();
    await page.getByLabel('需求描述', { exact: true }).fill('帮助我准备试印');
    await expect(page.getByRole('button', { name: '预览 AI 起草', exact: true })).toBeVisible();
    await page.getByRole('button', { name: '预览 AI 起草', exact: true }).click();
    await expect(page.getByRole('dialog').getByText('帮助我准备试印', { exact: true })).toBeVisible();
    expect(calls).toHaveLength(0);
    await page.getByRole('button', { name: '确认生成草稿', exact: true }).click();
    await page.getByRole('button', { name: '使用草稿', exact: true }).waitFor({ timeout: 20000 });
    await page.reload();
    await page.getByRole('button', { name: '使用草稿', exact: true }).click();
    await expect(page.getByRole('textbox', { name: 'Agent 名称', exact: true })).toHaveValue('试印指导');
    await expect(page.getByRole('textbox', { name: '职责', exact: true })).toHaveValue('按准备、检查及反馈组织练习。');
    expect(calls).toHaveLength(1);
    expect(JSON.parse(calls[0].split('需求描述 JSON' + '\n')[1])).toEqual({ purpose: '帮助我准备试印' });
    await expect(page.getByRole('checkbox', { name: 'print-checklist', exact: true })).not.toBeChecked();
    await page.getByLabel('Agent 名称', { exact: true }).fill('印刷导师');
    await page.getByRole('textbox', { name: '职责', exact: true }).fill('每次先提示检查版框。');
    await page.getByRole('checkbox', { name: 'print-checklist', exact: true }).check();
    await page.getByRole('button', { name: '预览配置', exact: true }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog.getByText('print-checklist', { exact: false })).toBeVisible();
    expect(calls).toHaveLength(1);
    await dialog.getByRole('button', { name: '确认保存 Agent', exact: true }).click();
    await expect(page.getByText('印刷导师', { exact: true })).toBeVisible();

    await page.getByRole('link', { name: '聊天', exact: true }).click();
    await page.getByRole('combobox', { name: '聊天 Agent', exact: true }).selectOption({ label: '印刷导师' });
    await page.getByRole('combobox', { name: '本次使用的 Skill', exact: true }).selectOption('print-checklist');
    await page.getByLabel('对话消息', { exact: true }).fill('试印先做哪一步？');
    await page.getByRole('button', { name: '发送消息', exact: true }).click();
    const share = page.getByRole('dialog', { name: '确认聊天发送范围' });
    await expect(share.getByText('每次先提示检查版框。', { exact: true })).toBeVisible();
    await share.getByText('查看本次发送的 Skill 原文', { exact: true }).click();
    await expect(share.locator('pre')).toContainText('先记录版框是否平整。');
    expect(calls).toHaveLength(1);
    await share.getByRole('button', { name: '确认发送', exact: true }).click();
    await expect(page.getByText('试印前先检查版框。', { exact: true })).toBeVisible({ timeout: 20000 });
    await expect(page.getByText('Skill print-checklist · 第 1 版', { exact: true })).toBeVisible();
    expect(calls).toHaveLength(2);
    expect(calls[1]).toContain(parsed.checksum);
    expect(calls[1]).toContain('每次先提示检查版框。');

    await page.getByRole('combobox', { name: '聊天 Agent', exact: true }).selectOption('');
    await expect(page.getByText('试印前先检查版框。', { exact: true })).toBeVisible();
    await expect(page.getByRole('combobox', { name: '本次使用的 Skill', exact: true })).toHaveCount(0);
  } finally {
    output.close();
    if (service.exitCode === null && service.signalCode === null) {
      const stopped = once(service, 'exit');
      service.stdin.end();
      await stopped;
    }
    await new Promise<void>(done => provider.close(() => done()));
    await rm(data, { recursive: true, force: true, maxRetries: 4, retryDelay: 180 });
  }
});
