import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, rm, rmdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { expect, test } from '@playwright/test';

const source = [
  '---', 'name: study-checklist', 'description: 整理试印前检查事项。', 'metadata:', '  version: "1.0"',
  'allowed-tools: Read', '---', '# 试印前', '<em>这是原文</em>', '',
].join('\n');

test('能力库预览本地文件、明确确认、重载后查看原文', async ({ page }) => {
  test.setTimeout(90000);
  const data = await mkdtemp(join(tmpdir(), 'zhixue-skill-e2e-'));
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
      once(service, 'exit').then(([code]) => { throw new Error('本机验收服务提前退出，退出码 ' + code); }),
    ]);
    await page.goto(address);
    await page.getByRole('button', { name: '使用此位置', exact: true }).click();
    await page.getByRole('button', { name: '稍后配置模型', exact: true }).click();
    await page.getByRole('link', { name: '管理中心', exact: true }).click();
    await page.getByRole('link', { name: '能力库', exact: true }).click();
    await expect(page.getByText('还没有已导入的 Skill')).toBeVisible();
    const file = { name: 'SKILL.md', mimeType: 'text/markdown', buffer: Buffer.from(source, 'utf8') };
    const picker = page.getByLabel('选择 SKILL.md');
    await picker.setInputFiles(file);
    const review = page.getByRole('dialog', { name: '确认导入 Skill' });
    await expect(review.getByText('study-checklist', { exact: true })).toBeVisible();
    await review.getByRole('button', { name: '取消', exact: true }).click();
    await expect(page.getByText('还没有已导入的 Skill')).toBeVisible();
    await picker.setInputFiles(file);
    await expect(review.getByText('Read', { exact: true })).toBeVisible();
    await review.getByRole('button', { name: '确认导入', exact: true }).click();
    await expect(review).not.toBeVisible();
    await page.reload();
    await page.getByRole('button', { name: '查看 study-checklist', exact: true }).click();
    const detail = page.getByRole('dialog', { name: 'Skill 详情' });
    await expect(detail.getByText('1.0', { exact: true })).toBeVisible();
    await detail.getByText('查看 SKILL.md 原文', { exact: true }).click();
    await expect(detail.locator('pre')).toContainText('<em>这是原文</em>');
    await expect(detail.locator('em')).toHaveCount(0);

    await detail.getByRole('button', { name: '关闭', exact: true }).click();

    const update = {
      ...file, buffer: Buffer.from(source
        .replace('version: "1.0"', 'version: "2.0"')
        .replace('allowed-tools: Read', 'allowed-tools: Read Search')
        .replace('这是原文', '这是更新的原文'), 'utf8'),
    };
    await picker.setInputFiles(update);
    const updateReview = page.getByRole('dialog', { name: '确认更新 Skill' });
    await expect(updateReview.getByRole('status')).toContainText('有可更新版本');
    const changes = updateReview.getByRole('table', { name: '元数据变更' });
    await expect(changes.getByRole('row', { name: /声明的工具/ })).toContainText('Read Search');
    await updateReview.getByText('查看原文差异', { exact: true }).click();
    await expect(updateReview.locator('pre:visible')).toContainText('+<em>这是更新的原文</em>');
    await updateReview.getByRole('button', { name: '取消', exact: true }).click();
    await expect(page.getByText('版本 1.0', { exact: true })).toBeVisible();
    await picker.setInputFiles(update);
    await updateReview.getByRole('button', { name: '确认更新', exact: true }).click();
    await expect(page.getByText('版本 2.0', { exact: true })).toBeVisible();

    await page.getByRole('button', { name: '查看 study-checklist', exact: true }).click();
    await detail.getByRole('button', { name: '查看第 1 版', exact: true }).click();
    await expect(detail.getByRole('table', { name: '元数据变更' }).getByRole('row', { name: /声明的工具/ }))
      .toContainText('Read Search');
    await detail.getByRole('button', { name: '确认切换到此版本', exact: true }).click();
    await expect(detail.getByText('当前版本 · 第 1 版', { exact: true })).toBeVisible();
    await expect(detail.getByRole('button', { name: '查看第 2 版', exact: true })).toBeVisible();
    await page.reload();
    await expect(page.getByText('版本 1.0', { exact: true })).toBeVisible();
    const filters = page.getByRole('group', { name: '能力分类', exact: true });
    await expect(page.getByText('可更新', { exact: true })).toBeVisible({ timeout: 6000 });
    await filters.getByRole('button', { name: 'MCP', exact: true }).click();
    await expect(page.getByText('尚无已配置的 MCP 连接', { exact: true })).toBeVisible();
    await expect(picker).toHaveCount(0);
    await filters.getByRole('button', { name: '工具', exact: true }).click();
    const openTool = page.getByRole('button', { name: '查看 知识库检索', exact: true });
    await openTool.click();
    const tool = page.getByRole('dialog', { name: '知识库检索', exact: true });
    await expect(tool.getByText('智学内置', { exact: true })).toBeVisible();
    await expect(tool.getByText('本机知识库索引', { exact: true })).toBeVisible();
    await expect(tool.getByText('尚无 Agent 获得授权', { exact: true })).toBeVisible();

    const origin = new URL(address);
    const session = new URLSearchParams(origin.hash.split('?')[1]).get('session');
    const create = await page.request.post(origin.origin + '/api/files/folders', {
      headers: { Authorization: 'Bearer ' + session }, data: { parent: 'library', name: '授权用例' },
    });
    expect(create.ok(), await create.text()).toBeTruthy();
    await tool.getByRole('link', { name: '管理 Agent 授权', exact: true }).click();
    await page.getByRole('button', { name: '新建专业 Agent', exact: true }).click();
    const agent = page.getByRole('dialog', { name: '新建专业 Agent', exact: true });
    await agent.getByRole('textbox', { name: '需求描述', exact: true }).fill('读授权资料');
    await agent.getByRole('button', { name: '编辑配置', exact: true }).click();
    await agent.getByRole('textbox', { name: 'Agent 名称', exact: true }).fill('目录验收导师');
    await agent.getByRole('checkbox', { name: 'study-checklist', exact: true }).check();
    await agent.getByRole('combobox', { name: '添加授权文件夹', exact: true }).selectOption('library/授权用例');
    await agent.getByRole('button', { name: '预览配置', exact: true }).click();
    await agent.getByRole('button', { name: '确认保存 Agent', exact: true }).click();
    await expect(agent).not.toBeVisible();

    await page.getByRole('link', { name: '能力库', exact: true }).click();
    await expect(page.getByText('已启用', { exact: true })).toBeVisible();
    await filters.getByRole('button', { name: '工具', exact: true }).click();
    await openTool.click();
    const grant = tool.getByRole('list', { name: 'Agent 授权' }).getByRole('listitem');
    await expect(grant).toContainText('目录验收导师');
    await expect(grant).toContainText('library/授权用例');
    await expect(grant).toContainText('可用');

    await tool.getByRole('button', { name: '关闭', exact: true }).click();
    await rmdir(join(data, 'library', '授权用例'));
    await openTool.click();
    await expect(grant).toContainText('目录不可用');

  } finally {
    output.close();
    if (service.exitCode === null && service.signalCode === null) {
      const stopped = once(service, 'exit');
      service.stdin.end();
      await stopped;
    }
    await rm(data, { recursive: true, force: true, maxRetries: 4, retryDelay: 150 });
  }
});
