import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { expect, test } from '@playwright/test';

test('从对话保存记忆，经确认共享并删除后保留原话', async ({ page }) => {
  test.setTimeout(90000);
  page.setDefaultTimeout(12000);
  const data = await mkdtemp(join(tmpdir(), 'zhixue-local-memory-'));
  const python = process.env.ZHIXUE_TEST_PYTHON ?? resolve(
    process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python',
  );
  const service = spawn(python, ['-m', 'backend.launch', '--port', '0', '--stdio', '--data', data], {
    stdio: ['pipe', 'pipe', 'inherit'], windowsHide: true,
  });
  const stdout = createInterface({ input: service.stdout });
  const original = '我的学习偏好是先看一个例子。';
  try {
    const [address] = await Promise.race([
      once(stdout, 'line'), once(service, 'exit').then(([code]) => { throw new Error('本机服务提前退出 ' + code); }),
    ]);
    await page.goto(address);
    await page.getByRole('button', { name: '使用此位置', exact: true }).click();
    await page.getByRole('button', { name: '稍后配置模型', exact: true }).click();
    await page.getByRole('link', { name: '管理中心', exact: true }).click();
    await page.getByRole('link', { name: '记忆', exact: true }).click();
    const sections = page.getByRole('group', { name: '记忆范围', exact: true });
    await expect(sections.getByRole('button', { name: 'Agent 专属', exact: true })).toBeVisible();

    await page.getByRole('link', { name: '聊天', exact: true }).click();
    await page.getByLabel('对话消息', { exact: true }).fill(original);
    await page.getByRole('button', { name: '发送消息', exact: true }).click();
    await page.getByRole('button', { name: '保存为记忆', exact: true }).click();
    const form = page.getByRole('dialog', { name: '新增记忆', exact: true });
    await expect(form.getByRole('textbox', { name: '记忆内容', exact: true })).toHaveValue(original);
    await form.getByRole('button', { name: '保存记忆', exact: true }).click();
    await expect(page.getByText('已加入待确认', { exact: true })).toBeVisible();

    await page.getByRole('link', { name: '管理中心', exact: true }).click();
    await page.getByRole('link', { name: '记忆待确认（1）', exact: true }).click();
    await expect(page.getByText(original, { exact: true })).toBeVisible();
    await expect(page.getByText('用户明确表达', { exact: true })).toBeVisible();
    await sections.getByRole('button', { name: '共享记忆', exact: true }).click();
    await expect(page.getByText(original, { exact: true })).toHaveCount(0);
    await sections.getByRole('button', { name: '待确认', exact: true }).click();
    await page.getByRole('button', { name: '编辑', exact: true }).click();
    const edit = page.getByRole('dialog', { name: '编辑记忆', exact: true });
    await edit.getByRole('textbox', { name: '记忆内容', exact: true }).fill('先讲一个例子。');
    await edit.getByRole('button', { name: '保存记忆', exact: true }).click();
    await expect(edit).toBeHidden();
    await page.getByRole('checkbox', { name: '选择记忆', exact: true }).check();
    await page.getByRole('button', { name: '接受所选', exact: true }).click();

    await page.reload();
    await sections.getByRole('button', { name: '共享记忆', exact: true }).click();
    await expect(page.getByText('先讲一个例子。', { exact: true })).toBeVisible();
    await page.getByText('查看来源与范围', { exact: true }).click();
    await expect(page.getByText(original, { exact: true })).toBeVisible();
    const source = page.getByRole('link', { name: '打开原对话', exact: true });
    const thread = await source.getAttribute('href');
    expect(thread).toContain('/chat/');
    await page.getByRole('button', { name: '删除', exact: true }).click();
    const confirm = page.getByRole('dialog', { name: '删除记忆', exact: true });
    await confirm.getByRole('button', { name: '确认删除', exact: true }).click();
    await expect(page.getByText('先讲一个例子。', { exact: true })).toHaveCount(0);
    await page.goto(new URL(thread!, page.url()).href);
    await expect(page.getByRole('paragraph').filter({ hasText: original })).toBeVisible();
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