import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { expect, test } from '@playwright/test';

test('配置只读 MCP 并在聊天预览后带入指定仓库片段', async ({ page }) => {
  test.setTimeout(90000);
  page.setDefaultTimeout(12000);
  const data = await mkdtemp(join(tmpdir(), 'zhixue-mcp-fixture-'));
  const text = '先核对规格再安装样品。';
  const python = process.env.ZHIXUE_TEST_PYTHON ?? resolve(
    process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python',
  );
  const service = spawn(python, ['-m', 'e2e.repository_mcp_service', data], {
    stdio: ['pipe', 'pipe', 'inherit'], windowsHide: true,
  });
  const stdout = createInterface({ input: service.stdout });
  try {
    const [address] = await Promise.race([
      once(stdout, 'line'), once(service, 'exit').then(([code]) => { throw new Error('MCP 样本服务提前退出 ' + code); }),
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
    const model = await api('/models/connections', 'POST', {
      name: '本机模型样本', endpoint: 'http://127.0.0.1:9561/v1', model: 'mcp-fixture',
      provider: 'custom', protocol: 'chat-completions',
    });
    await api('/models/roles', 'PATCH', { main: model.id });
    await page.getByRole('link', { name: '管理中心', exact: true }).click();
    await page.getByRole('link', { name: '能力库', exact: true }).click();
    await page.getByRole('button', { name: 'MCP', exact: true }).click();
    await expect(page.getByText('尚无已配置的 MCP 连接', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '配置 GitHub MCP', exact: true }).click();
    const setup = page.getByRole('dialog', { name: '配置 GitHub MCP', exact: true });
    await setup.getByLabel('MCP 访问令牌', { exact: true }).fill('isolated-mcp-credential');
    await setup.getByLabel('允许读取的仓库', { exact: true }).fill('example/training');
    await setup.getByRole('checkbox', { name: '启用只读连接', exact: true }).check();
    await setup.getByRole('button', { name: '验证并保存连接', exact: true }).click();
    await expect(page.getByRole('button', { name: '查看 GitHub MCP', exact: true })).toBeVisible();

    await page.getByRole('link', { name: '专业 Agent', exact: true }).click();
    await page.getByRole('button', { name: '新建专业 Agent', exact: true }).click();
    await page.getByLabel('需求描述', { exact: true }).fill('指导安装实验');
    await page.getByRole('button', { name: '编辑配置', exact: true }).click();
    await page.getByLabel('Agent 名称', { exact: true }).fill('实验导师');
    await page.getByRole('checkbox', { name: 'GitHub · example/training', exact: true }).check();
    await page.getByRole('button', { name: '预览配置', exact: true }).click();
    const agent = page.getByRole('dialog', { name: '新建专业 Agent', exact: true });
    await expect(agent.getByText('github:example/training', { exact: true })).toBeVisible();
    await agent.getByRole('button', { name: '确认保存 Agent', exact: true }).click();
    await expect(page.getByRole('button', { name: '编辑 实验导师', exact: true })).toBeVisible();

    await page.getByRole('link', { name: '聊天', exact: true }).click();
    await page.getByRole('combobox', { name: '聊天 Agent', exact: true }).selectOption({ label: '实验导师' });
    await page.getByLabel('对话消息', { exact: true }).fill('如何做安装实验？');
    await page.getByRole('button', { name: '发送消息', exact: true }).click();
    const send = page.getByRole('dialog', { name: '确认聊天发送范围', exact: true });
    await send.getByText('读取已授权的仓库资料', { exact: true }).click();
    await send.getByRole('combobox', { name: 'MCP 仓库', exact: true }).selectOption('github:example/training');
    await send.getByLabel('条目编号', { exact: true }).fill('12');
    await send.getByRole('button', { name: '读取并预览仓库资料', exact: true }).click();
    const evidence = send.getByRole('group', { name: '本次发送的仓库资料', exact: true });
    await expect(evidence.getByText(text, { exact: true })).toBeVisible();
    await expect(evidence.getByRole('link')).toHaveAttribute('href', 'https://github.com/example/training/issues/12');
    await send.getByRole('button', { name: '确认发送', exact: true }).click();
    await expect(page.getByText('模型收到了已确认的仓库片段。', { exact: true })).toBeVisible();
    await expect(page.getByText('仓库资料 · github:example/training', { exact: true })).toBeVisible();
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
