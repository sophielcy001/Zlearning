import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, rm } from 'node:fs/promises';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { expect, test } from '@playwright/test';

test('专业 Agent 保存授权、自动匹配 Skill 并确认后聊天', async ({ page }) => {
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
    calls.push(JSON.parse(Buffer.concat(received).toString('utf8')).messages.at(-1).content);
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({ choices: [{ message: { content: '试印前先检查版框。' } }],
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
    const memoryText = '记忆样本：先做单色试印';
    const shared = await request('/memories', 'POST', { id: crypto.randomUUID(), text: memoryText });
    await request('/memories/review', 'POST', { action: 'accept', items: [{ id: shared.id, revision: shared.revision }] });
    await request('/memories', 'POST', { id: crypto.randomUUID(), text: '待确认的保密记忆' });
    for (const [folder, note] of [['试印资料', '版框要点：局部资料独有内容'], ['试印资料备份', '版框要点：备份保密内容']]) {
      const created = await request('/files/folders', 'POST', { parent: 'library', name: folder });
      const filename = folder + '.md';
      const record = await request('/files', 'POST', { name: filename, content: note });
      const change = { fileId: record.id, source: 'library/imports/' + record.id + '.md', parent: created.path, name: filename };
      const preview = await request('/files/relocation/preview', 'POST', change);
      await request('/files/relocation', 'PUT', { ...change, token: preview.token });
    }
    const parsed = await request('/skills/preview', 'POST', source, { 'Content-Type': 'text/markdown' });
    await request('/skills', 'POST', source, { 'Content-Type': 'text/markdown', 'X-Skill-Checksum': parsed.checksum });

    await page.getByRole('link', { name: '管理中心', exact: true }).click();
    await expect(page.getByRole('link', { name: '专业 Agent', exact: true })).toBeVisible();
    await page.getByRole('link', { name: '专业 Agent', exact: true }).click();
    await page.getByRole('button', { name: '新建专业 Agent', exact: true }).click();
    await page.getByLabel('需求描述', { exact: true }).fill('帮助我准备试印');
    await page.getByRole('button', { name: '编辑配置', exact: true }).click();
    await page.getByLabel('Agent 名称', { exact: true }).fill('印刷导师');
    await page.getByRole('textbox', { name: '职责', exact: true }).fill('每次先提示检查版框。');
    await page.getByRole('checkbox', { name: 'print-checklist', exact: true }).check();
    await page.getByRole('checkbox', { name: '读取共享记忆', exact: true }).check();
    await page.getByRole('combobox', { name: '添加授权文件夹', exact: true }).selectOption('library/试印资料');
    await page.getByRole('button', { name: '预览配置', exact: true }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog.getByText('print-checklist', { exact: false })).toBeVisible();
    await expect(dialog.getByText('library/试印资料', { exact: true })).toBeVisible();
    await expect(dialog.getByText('共享记忆', { exact: true })).toBeVisible();
    expect(calls).toHaveLength(0);
    await dialog.getByRole('button', { name: '确认保存 Agent', exact: true }).click();
    await expect(page.getByText('印刷导师', { exact: true })).toBeVisible();

    await page.getByRole('link', { name: '聊天', exact: true }).click();
    await page.getByRole('combobox', { name: '聊天 Agent', exact: true }).selectOption({ label: '印刷导师' });
    const skillMenu = page.getByRole('combobox', { name: '本次使用的 Skill', exact: true });
    await expect(skillMenu.locator('option:checked')).toHaveText('自动匹配');
    const text = page.getByLabel('对话消息', { exact: true });
    const send = page.getByRole('button', { name: '发送消息', exact: true });
    const share = page.getByRole('dialog', { name: '确认聊天发送范围' });
    const close = share.getByRole('button', { name: '关闭', exact: true });

    await text.fill('今天天气怎么样？');
    await send.click();
    await expect(share.getByText('未匹配到明确的已授权 Skill。', { exact: true })).toBeVisible();
    await close.click();

    await skillMenu.selectOption({ label: 'print-checklist' });
    await send.click();
    await expect(share.getByText('查看本次发送的 Skill 原文', { exact: true })).toBeVisible();
    await close.click();

    await skillMenu.selectOption({ label: '不附带 Skill' });
    await text.fill('试印前准备什么？');
    await send.click();
    await expect(share.getByRole('button', { name: '确认发送' })).toBeEnabled();
    await expect(share.getByText('查看本次发送的 Skill 原文', { exact: true })).toHaveCount(0);
    await close.click();

    await skillMenu.selectOption({ label: '自动匹配' });
    await send.click();
    await expect(share.getByText('自动选用', { exact: false })).toBeVisible();
    await expect(share.getByText('每次先提示检查版框。', { exact: true })).toBeVisible();
    await share.getByText('查看本次发送的 Skill 原文', { exact: true }).click();
    await expect(share.locator('pre')).toContainText('先记录版框是否平整。');
    await share.getByLabel('知识库检索关键词', { exact: true }).fill('版框要点');
    await share.getByRole('button', { name: '预览资料片段', exact: true }).click();
    await expect(share.getByText('版框要点：局部资料独有内容', { exact: true })).toBeVisible();
    await expect(share.getByText('备份保密内容', { exact: false })).toHaveCount(0);
    await share.getByText('选择本轮附带的记忆', { exact: true }).click();
    await expect(share.getByText('待确认的保密记忆', { exact: true })).toHaveCount(0);
    await share.getByRole('checkbox', { name: memoryText, exact: false }).check();
    await expect(share.getByRole('group', { name: '本次发送的记忆' })).toContainText(memoryText);
    expect(calls).toHaveLength(0);
    await share.getByRole('button', { name: '确认发送', exact: true }).click();
    await expect(page.getByText('试印前先检查版框。', { exact: true })).toBeVisible({ timeout: 20000 });
    await expect(page.getByText('Skill print-checklist · 第 1 版 · 自动选用', { exact: true })).toBeVisible();
    expect(calls).toHaveLength(1);
    expect(calls[0]).toContain(parsed.checksum);
    expect(calls[0]).toContain('每次先提示检查版框。');
    expect(calls[0]).toContain('局部资料独有内容');
    expect(calls[0]).not.toContain('备份保密内容');
    expect(calls[0]).not.toContain('library/试印资料');
    expect(calls[0]).toContain(memoryText);
    expect(calls[0]).not.toContain('待确认的保密记忆');
    expect(calls[0]).not.toContain(shared.id);
    await expect(page.getByText('已确认记忆 · 1 条', { exact: true })).toBeVisible();
    await expect(page.getByText('资料片段 · 1 条', { exact: true })).toBeVisible();

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
