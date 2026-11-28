import { execFileSync, spawn, type ChildProcess } from 'node:child_process';
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, realpathSync, renameSync, rmSync, unlinkSync, writeFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { once } from 'node:events';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { dirname, isAbsolute, join, relative, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { chromium, expect, test, type Browser, type Locator, type Page } from '@playwright/test';

const python = process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python';

test('a verified course revision waits for confirmation and retains the old reading and note anchor', async ({ page }, info) => {
  test.setTimeout(75000);
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-course-revision-'));
  const play = '将排好的活字固定在版框里，再检查字行。';
  const revised = '固定版框后，先检查字行是否平整，再试印。';
  const source = '排字后用版框固定活字，试印前应检查字行是否平整。';
  const currentSource = '版框固定后先检查墨色和字行，再试印。';
  const nextReading = '固定版框后，核对墨色和字行，再开始试印。';
  let reviewQuote = source;
  const requests: { model: string; prompt: string }[] = [];
  const provider = createServer(async (request, response) => {
    let body = '';
    for await (const chunk of request) body += chunk.toString();
    const payload = JSON.parse(body);
    const prompt: string = payload.messages[0].content;
    requests.push({ model: payload.model, prompt });
    const result = prompt.includes('制定学习路线') ? { stages: [{ name: '试印', lessons: [
      { key: 'proof', title: '试印前的检查', outcome: '说明如何固定版框并检查字行' },
    ] }] } : prompt.includes('复核材料 JSON') ? { sections: [
      { index: 0, verdict: 'supported', reason: '测试资料包含相应步骤。', citations: [{ source: 'S1', quote: reviewQuote }], dependsOn: [] },
    ] } : { sections: [{
      title: prompt.includes(currentSource) ? '新版试印检查' : prompt.includes('修订材料 JSON') ? '先检查版面' : '固定版框',
      text: prompt.includes(currentSource) ? nextReading : prompt.includes('修订材料 JSON') ? revised : play,
      checks: ['版框和字行'],
    }] };
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({ choices: [{ message: { content: JSON.stringify(result) } }],
      usage: { prompt_tokens: 40, completion_tokens: 70 } }));
  });
  let service: ReturnType<typeof start> | undefined;
  try {
    provider.listen(0, '127.0.0.1');
    await once(provider, 'listening');
    const bind = provider.address();
    if (!bind || typeof bind === 'string') throw new Error('Expected a loopback port');
    const endpoint = `http://127.0.0.1:${bind.port}/v1`;
    service = start(folder);
    const url = new URL(await service.url);
    const headers = { Authorization: 'Bearer ' + new URLSearchParams(url.hash.split('?')[1]).get('session') };
    async function request(path: string, method = 'GET', data?: object) {
      const reply = await page.request.fetch(url.origin + '/api' + path, { headers, method, data });
      expect(reply.ok(), await reply.text()).toBeTruthy();
      return reply.json();
    }
    const roles: Record<string, string> = {};
    for (const role of ['main', 'review']) {
      const connection = await request('/models/connections', 'POST', {
        name: `修订测试 ${role}`, endpoint, model: `revision-${role}`, provider: 'custom', protocol: 'chat-completions',
      });
      roles[role] = connection.id;
    }
    await request('/models/roles', 'PATCH', roles);
    const thread = await request('/conversations', 'POST', {
      id: '00000000-0000-4000-8000-000000000085',
      message: { id: '00000000-0000-4000-8000-000000000086', text: '我要学活字试印' },
    });
    const goal = `/conversations/${thread.id}/goal`;
    const proposal = await request(goal, 'POST');
    const input = { goalId: proposal.id, revision: proposal.revision, values: { outcome: '完成试印前检查' } };
    const consent = await request(goal + '/model-preview', 'POST', { ...input, maxCalls: 10 });
    const { projectId } = await request(goal + '/submit', 'POST', { ...input, modelConsent: consent.consent });
    const project = `/projects/${projectId}`;
    await expect.poll(async () => (await request(project + '/generation')).state).toBe('awaiting_review');
    const lesson = (await request(project)).lessons[0].id;
    const point = `/lessons/${lesson}`;
    const file = await request('/files', 'POST', { name: '试印资料.txt', content: source + '\n仅在本地的附件内容。' });
    const sourceView = await request(`/files/${file.id}?at=1`);
    const scope = { sources: [{ fileId: file.id, at: 1, revision: sourceView.revision }] };
    const reviewPreview = await request(project + '/course-review/preview', 'POST', scope);
    await request(project + '/course-review/authorize', 'POST', { ...scope, confirmation: reviewPreview.confirmation });
    await expect.poll(async () => (await request(point)).courseVersion).toBe(1);
    await request(point + '/learned', 'PUT', { learned: true });
    const anchor = point + '?readingVersion=1&section=0&selectionStart=0&selectionEnd=' + [...play].length;
    const noteBody = '我自己的草稿保留。\n\n> ' + play + '\n\n[查看原文 · 第 1 版](#' + anchor + ')\n';
    const note = await request(point + '/note');
    await request(point + '/note', 'PUT', { content: noteBody, baseRevision: note.revision });
    await page.goto(url.href);
    await page.evaluate(path => { window.location.hash = path; }, point);
    await expect(page.getByRole('heading', { name: '固定版框', exact: true })).toBeVisible();
    await page.getByRole('button', { name: '修订课程', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '修订课程', exact: true });
    await dialog.getByLabel('修订范围').selectOption({ label: '固定版框' });
    await dialog.getByLabel('修改要求').fill('把试印前检查放在步骤里');
    await dialog.getByRole('button', { name: '预览发送范围' }).click();
    await expect(dialog.getByText(/起草 · 修订测试 main · revision-main/)).toBeVisible();
    expect(requests).toHaveLength(3);
    await dialog.getByRole('button', { name: '确认并生成修订稿' }).click();
    await expect(dialog.getByRole('heading', { name: '新版差异' })).toBeVisible({ timeout: 15000 });
    await expect(dialog.getByText(revised, { exact: true })).toBeVisible();
    await expect(dialog.locator('.course-revision-changes').getByText(play, { exact: true })).toBeVisible();
    const impact = dialog.getByRole('list', { name: '新版中待重新关联的摘录' });
    await expect(impact.getByText(play, { exact: true })).toBeVisible();
    expect((await request(point)).courseVersion).toBe(1);
    expect(requests.slice(3).map(item => item.model)).toEqual(['revision-main', 'revision-review']);
    expect(requests[3].prompt).not.toContain(source);
    expect(requests[4].prompt).toContain(source);
    expect(requests.slice(3).every(item => !item.prompt.includes('仅在本地的附件内容') && !item.prompt.includes('我自己的草稿'))).toBeTruthy();
    await dialog.screenshot({ path: info.outputPath('课程修订差异.png') });
    await dialog.getByRole('button', { name: '接受新版' }).click();
    await expect(page.getByRole('heading', { name: '先检查版面', exact: true })).toBeVisible();
    await expect(page.getByRole('checkbox', { name: '已学完', exact: true })).toBeChecked();
    await page.getByRole('link', { name: '今日任务', exact: true }).first().click();
    const updates = page.getByRole('heading', { name: '待查看课程更新', exact: true });
    await expect(updates).toBeVisible();
    await page.getByRole('link', { name: '查看课程更新', exact: true }).click();
    await expect(dialog.getByText(/待查看课程更新/)).toBeVisible();
    await dialog.getByRole('button', { name: '标为已知悉' }).click();
    await expect(dialog.getByLabel('修订范围')).toBeVisible();
    await dialog.getByRole('button', { name: '关闭', exact: true }).click();
    await page.getByRole('button', { name: '待重新关联', exact: true }).click();
    const pendingAnchors = page.getByRole('dialog', { name: '待重新关联', exact: true });
    await expect(pendingAnchors.getByRole('link', { name: /试印前的检查/ })).toBeVisible();
    await expect(pendingAnchors.getByText(play, { exact: true })).toBeVisible();
    await pendingAnchors.screenshot({ path: info.outputPath('待重新关联清单.png') });
    await pendingAnchors.getByRole('button', { name: '重新关联此摘录' }).click();
    const linkEditor = page.getByRole('dialog', { name: '重新关联摘录' });
    await expect(linkEditor.getByRole('heading', { name: '旧版原文' })).toBeVisible();
    await linkEditor.getByRole('button', { name: '关闭', exact: true }).click();

    await page.getByRole('button', { name: '笔记', exact: true }).click();
    const notes = page.locator('.note-panel');
    await notes.getByRole('link', { name: '查看原文 · 第 1 版' }).click();
    const previous = page.getByRole('dialog', { name: '已保存课程版本' });
    await expect(previous.getByText(/待重新关联/)).toBeVisible();
    await expect(previous.locator('mark')).toHaveText(play);
    await previous.getByRole('button', { name: '选择新版位置' }).click();
    const targetField = linkEditor.getByRole('textbox', { name: '选择新版文字' });
    const selectedQuote = revised.split('，再试印')[0];
    await targetField.focus();
    await targetField.evaluate((field: HTMLTextAreaElement) => field.setSelectionRange(0, 0));
    for (const _character of selectedQuote) await targetField.press('Shift+ArrowRight');
    await linkEditor.getByRole('button', { name: '预览关联' }).click();
    await expect(linkEditor.getByText(selectedQuote, { exact: true })).toBeVisible();
    expect((await request(point + '/anchor-links')).items).toHaveLength(1);
    await linkEditor.screenshot({ path: info.outputPath('摘录关联预览.png') });
    let releaseSave!: () => void;
    const heldSave = new Promise<void>(resolve => { releaseSave = resolve; });
    const saveRoute = '**/api' + point + '/anchor-links';
    await page.route(saveRoute, async route => {
      if (route.request().method() === 'PUT') await heldSave;
      await route.continue();
    });
    try {
      await linkEditor.getByRole('button', { name: '确认关联' }).click();
      await expect(linkEditor.getByRole('button', { name: '关闭', exact: true })).toBeDisabled();
      await page.keyboard.press('Escape');
      await expect(linkEditor).toBeVisible();
    } finally {
      releaseSave();
    }
    await expect(page.locator('.course-section mark')).toHaveText(selectedQuote);
    await page.unroute(saveRoute);
    await expect(page.getByText('此摘录已关联到第 2 版。')).toBeVisible();
    await page.getByRole('button', { name: '查看旧版原文', exact: true }).click();
    await expect(previous.locator('mark')).toHaveText(play);
    await previous.getByRole('button', { name: '关闭', exact: true }).click();
    const checkedNote = await request(point + '/note');
    expect(checkedNote.content).toBe(noteBody);
    expect((await request(point + '/anchor-links')).items).toEqual([]);
    await page.reload();
    await expect(page.locator('.course-section mark')).toHaveText(selectedQuote);
    expect(requests).toHaveLength(5);

    reviewQuote = currentSource;
    writeFileSync(join(folder, sourceView.path), currentSource + '\n仅在本地的附件内容。');
    await page.getByRole('link', { name: '今日任务', exact: true }).first().click();
    const sourceTasks = page.getByRole('heading', { name: '待核对的来源变更', exact: true });
    await expect(sourceTasks).toBeVisible();
    await page.getByRole('link', { name: '核对来源变更', exact: true }).click();
    const comparison = page.getByRole('dialog', { name: '核对来源变更' });
    await expect(comparison.getByRole('heading', { name: '先检查版面' })).toBeVisible();
    await expect(comparison.getByText(source, { exact: true })).toBeVisible();
    await expect(comparison.getByText(currentSource, { exact: true })).toBeVisible();
    await expect(comparison.getByRole('heading', { name: '修订建议' })).toBeVisible();
    expect(requests).toHaveLength(5);
    await comparison.screenshot({ path: info.outputPath('来源变更对照.png') });
    const viewport = page.viewportSize();
    if (viewport) {
      await page.setViewportSize({ width: 480, height: 900 });
      await comparison.screenshot({ path: info.outputPath('来源变更对照-窄屏.png') });
      await page.setViewportSize(viewport);
    }

    await comparison.getByRole('button', { name: '用此定位开始修订' }).click();
    await expect(dialog.getByLabel('修订范围')).toHaveValue('0');
    await expect(dialog.getByLabel('修改要求')).toHaveValue(/用新资料逐条核对/);
    await dialog.getByRole('button', { name: '预览发送范围' }).click();
    await expect(dialog.getByText('起草与独立复核收到的资料', { exact: true })).toBeVisible();
    await expect(dialog.getByText(currentSource, { exact: true })).toBeVisible();
    expect(requests).toHaveLength(5);
    await dialog.getByRole('button', { name: '确认并生成修订稿' }).click();
    await expect(dialog.getByRole('heading', { name: '新版差异' })).toBeVisible({ timeout: 15000 });
    await expect(dialog.getByText(nextReading, { exact: true })).toBeVisible();
    expect((await request(point)).courseVersion).toBe(2);
    expect(requests.slice(5).map(item => item.model)).toEqual(['revision-main', 'revision-review']);
    expect(requests.slice(5).every(item => item.prompt.includes(currentSource)
      && !item.prompt.includes('仅在本地的附件内容'))).toBeTruthy();
    await dialog.getByRole('button', { name: '接受新版' }).click();
    await expect(page.getByRole('heading', { name: '新版试印检查', exact: true })).toBeVisible();
    await page.getByRole('link', { name: '今日任务', exact: true }).first().click();
    await expect(page.getByRole('link', { name: '查看课程更新', exact: true })).toBeVisible();
    await expect(sourceTasks).toHaveCount(0);
    expect(await request('/study-plan/source-updates')).toEqual([]);
    const pendingAgain = (await request(point + '/anchor-links')).items;
    expect(pendingAgain.map((item: { quote: string }) => item.quote)).toEqual([play]);
  } finally {
    if (service) await stop(service.child);
    await new Promise<void>(done => provider.close(() => done()));
    rmSync(folder, { recursive: true, force: true });
  }
});

const testClockLauncher = `
from datetime import datetime
from pathlib import Path
import sys

from backend import launch
from backend.app import create_app
from backend.project_activity import study_moment

path = Path(sys.argv.pop(1))

def now():
    return datetime.fromisoformat(path.read_text(encoding="utf-8"))

launch.create_app = lambda folder, token, **options: create_app(
    folder, token, today=lambda: study_moment(now()).date(), now=now, **options
)
raise SystemExit(launch.main())
`;

function start(folder: string, clockFile?: string) {
  return startService(folder, clockFile ? ['-c', testClockLauncher, clockFile] : ['-m', 'backend.launch']);
}

async function openDefaultWorkspace(address: string) {
  const local = new URL(address);
  const session = new URLSearchParams(local.hash.split('?')[1]).get('session');
  const base = local.origin + '/api/setup';
  const headers = { Authorization: 'Bearer ' + session, 'Content-Type': 'application/json' };
  const status = async () => {
    const response = await fetch(base, { headers, signal: AbortSignal.timeout(1200) });
    if (!response.ok) throw new Error('Could not read workspace setup: ' + response.status);
    return (await response.json()).stage as 'storage' | 'opening' | 'models' | 'complete';
  };

  let stage = await status();
  if (stage === 'storage') {
    const result = await fetch(base + '/storage', { headers, method: 'PUT', body: JSON.stringify({ directory: null }) });
    if (result.status !== 202) throw new Error('Could not select the fixture workspace: ' + result.status);
    stage = 'opening';
  }
  if (stage === 'opening') {
    const deadline = Date.now() + 10000;
    while (stage === 'opening' && Date.now() < deadline) {
      try {
        stage = await status();
      } catch (error) {
        if (!(error instanceof TypeError || error instanceof DOMException)) throw error;
      }
      if (stage === 'opening') await new Promise(resolve => setTimeout(resolve, 70));
    }
    if (stage === 'opening') throw new Error('Fixture workspace did not finish opening');
  }
  if (stage === 'models') {
    const result = await fetch(base + '/complete', { headers, method: 'POST' });
    if (!result.ok) throw new Error('Could not finish fixture setup: ' + result.status);
  }
  return address;
}

function startService(folder: string, runner: string[], firstUse = false) {
  const child = spawn(resolve(python), [...runner, '--port', '0', '--stdio', '--data', folder], {
    stdio: ['pipe', 'pipe', 'inherit'], windowsHide: true,
  });
  const url = new Promise<string>((resolveUrl, reject) => {
    child.once('error', reject);
    child.once('exit', (code) => reject(new Error(`Demo service exited (${code})`)));
    createInterface({ input: child.stdout }).once('line', address => {
      if (firstUse) resolveUrl(address);
      else void openDefaultWorkspace(address).then(resolveUrl, reject);
    });
  });
  return { child, url };
}

async function stop(child: ChildProcess) {
  if (child.exitCode !== null || child.signalCode !== null) return;
  const exited = new Promise<void>((resolveExit) => child.once('exit', () => resolveExit()));
  child.stdin?.end();
  await exited;
}

test('first use opens the chosen library, assigns models and shows confirmed connection results', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-first-use-'));
  const control = join(folder, 'device');
  const chosen = join(folder, '我的知识库');
  const requests: string[] = [];
  const provider = createServer((request, response) => {
    let body = '';
    request.setEncoding('utf8');
    request.on('data', chunk => { body += chunk; });
    request.on('end', () => {
      requests.push(JSON.parse(body).model);
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({ choices: [{ message: { content: 'OK' } }] }));
    });
  });
  provider.listen(0, '127.0.0.1');
  await once(provider, 'listening');
  const location = provider.address();
  if (!location || typeof location === 'string') throw new Error('Fixture model service failed to bind');
  const startGuide = () => startService(control, ['-m', 'backend.launch'], true);
  let service = startGuide();
  try {
    await page.setViewportSize({ width: 410, height: 820 });
    await page.goto(await service.url);
    await expect(page.getByRole('heading', { name: '选择知识库位置' })).toBeVisible();
    const path = page.getByLabel('保存文件夹', { exact: true });
    await path.fill('相对路径');
    await page.getByRole('button', { name: '使用此位置', exact: true }).click();
    await expect(page.getByRole('alert')).toContainText('绝对路径');
    await path.fill(chosen);
    await page.getByRole('button', { name: '使用此位置', exact: true }).click();
    await expect(page.getByRole('heading', { name: '配置学习模型', exact: true })).toBeVisible({ timeout: 12000 });

    for (const info of [{ name: '本机主模型', model: 'guide-main' }, { name: '本机复核模型', model: 'guide-review' }]) {
      await page.getByRole('button', { name: '新增连接', exact: true }).click();
      const editor = page.getByRole('dialog', { name: '新增服务连接' });
      const providerField = editor.getByRole('combobox', { name: '服务商', exact: true });
      await expect(providerField).toHaveValue('minimax');
      await providerField.selectOption('custom');
      await editor.getByLabel('连接名称', { exact: true }).fill(info.name);
      await editor.getByLabel('API 地址', { exact: true }).fill('http://127.0.0.1:' + location.port + '/v1');
      await editor.getByLabel('模型名称', { exact: true }).fill(info.model);
      await editor.getByRole('button', { name: '保存连接', exact: true }).click();
      await expect(editor).toHaveCount(0);
    }
    await page.getByRole('button', { name: '模型分工', exact: true }).click();
    await page.getByRole('combobox', { name: '主模型', exact: true }).selectOption({ label: '本机主模型 · guide-main' });
    await page.getByRole('combobox', { name: '复核模型', exact: true }).selectOption({ label: '本机复核模型 · guide-review' });
    await page.getByRole('button', { name: '保存模型分工', exact: true }).click();
    await expect(page.getByText('模型分工已保存', { exact: true })).toBeVisible();
    await page.getByRole('link', { name: '查看测试结果', exact: true }).click();
    expect(requests).toEqual([]);
    const lead = page.getByRole('article').filter({ hasText: '本机主模型' });
    await expect(lead.getByText('尚未测试', { exact: true })).toBeVisible();
    await lead.getByRole('button', { name: '测试连接', exact: true }).click();
    expect(requests).toEqual([]);
    await page.getByRole('dialog', { name: '测试服务连接' }).getByRole('button', { name: '开始测试', exact: true }).click();
    await expect(lead.getByText('文本回复通过', { exact: true })).toBeVisible();
    expect(requests).toEqual(['guide-main']);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(410);

    await stop(service.child);
    service = startGuide();
    await page.goto(await service.url);
    await page.getByRole('link', { name: '查看测试结果', exact: true }).click();
    await expect(lead.getByText('文本回复通过', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '进入首页', exact: true }).click();
    await expect(page.getByRole('heading', { name: '今天，想弄懂什么？', exact: true })).toBeVisible();
    await page.getByRole('link', { name: '知识库', exact: true }).click();
    await page.getByLabel('导入资料').setInputFiles({
      name: '首次导入.txt', mimeType: 'text/plain', buffer: Buffer.from('实际写入引导选择的知识库。'),
    });
    await expect(page.getByText('实际写入引导选择的知识库。', { exact: true })).toBeVisible();
    const imports = join(chosen, 'library', 'imports');
    expect(readFileSync(join(imports, readdirSync(imports)[0]), 'utf8')).toBe('实际写入引导选择的知识库。');
    await page.getByRole('link', { name: '管理中心', exact: true }).click();
    await expect(page.getByText(realpathSync(join(chosen, 'library')), { exact: true })).toBeVisible();
    expect(requests).toEqual(['guide-main']);
  } finally {
    await stop(service.child);
    const closed = once(provider, 'close');
    provider.close();
    await closed;
    rmSync(folder, { recursive: true, force: true });
  }
});

test('first use can defer models and keeps the application usable after reload', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-first-use-defer-'));
  const service = startService(folder, ['-m', 'backend.launch'], true);
  try {
    const address = new URL(await service.url);
    await page.goto(address.href);
    await page.getByRole('button', { name: '使用此位置', exact: true }).click();
    await page.getByRole('button', { name: '稍后配置模型', exact: true }).click({ timeout: 12000 });
    await expect(page.getByRole('heading', { name: '今天，想弄懂什么？', exact: true })).toBeVisible();
    await page.reload();
    await expect(page.getByRole('heading', { name: '今天，想弄懂什么？', exact: true })).toBeVisible();
    const session = new URLSearchParams(address.hash.split('?')[1]).get('session');
    const headers = { Authorization: 'Bearer ' + session };
    const state = await page.request.get(address.origin + '/api/state', { headers });
    expect((await state.json()).recentProjects).toEqual([]);
    await page.getByRole('link', { name: '知识库', exact: true }).click();
    await page.getByLabel('导入资料').setInputFiles({
      name: '本地资料.txt', mimeType: 'text/plain', buffer: Buffer.from('未配置模型也能导入。'),
    });
    await expect(page.getByText('未配置模型也能导入。', { exact: true })).toBeVisible();
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

test('saved goal and course conversation reopen, and route changes require confirmation', async ({ page }, info) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-intake-e2e-'));
  let service = start(folder);
  try {
    await page.goto(await service.url);
    await page.getByLabel('学习问题或目标').fill('我想练习法语口语');
    await page.getByRole('button', { name: '开始探索' }).click();
    await page.getByLabel('添加资料').setInputFiles({
      name: '口语任务.txt', mimeType: 'text/plain', buffer: Buffer.from('在餐厅点餐。'),
    });
    await expect(page.getByRole('link', { name: '口语任务.txt', exact: true })).toBeVisible();
    await page.getByRole('button', { name: '整理为学习目标' }).click();
    await page.getByText('学习依据（2）', { exact: true }).click();
    await page.getByRole('button', { name: '移除依据 口语任务.txt' }).click();
    await page.getByLabel('预期成果', { exact: true }).fill('完成餐馆点餐');
    await page.getByLabel('练习场景', { exact: true }).fill('小餐馆');
    await expect(page.getByText('目标卡有待保存的改动。', { exact: true })).toBeVisible();
    await page.reload();
    await expect(page.getByLabel('练习场景', { exact: true })).toHaveValue('小餐馆');
    await page.getByRole('button', { name: '保存草稿' }).click();
    await expect(page.getByText('目标草稿已保存', { exact: true })).toBeVisible();

    await stop(service.child);
    service = start(folder);
    await page.goto(await service.url);
    await page.getByRole('navigation', { name: '主要导航' }).getByRole('link', { name: '聊天' }).click();
    await page.getByRole('navigation', { name: '历史对话' }).getByRole('link', { name: '我想练习法语口语', exact: true }).click();
    await expect(page.getByLabel('练习场景', { exact: true })).toHaveValue('小餐馆');
    await page.getByRole('button', { name: '提交并生成路线' }).click();
    await expect(page.getByRole('heading', { name: '法语口语', exact: true })).toBeVisible();
    await expect(page.getByText('3 / 3 节已就绪', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '路线版本与顺序' }).click();
    const orderDialog = page.getByRole('dialog', { name: '路线版本与顺序' });
    await orderDialog.getByRole('button', { name: /上移/ }).last().click();
    await orderDialog.getByRole('button', { name: '预览顺序变化' }).click();
    await expect(orderDialog.getByText('当前路线第 1 版', { exact: true })).toBeVisible();
    await orderDialog.getByRole('button', { name: '确认新顺序' }).click();
    await expect(orderDialog.getByText('当前路线第 2 版', { exact: true })).toBeVisible();
    await orderDialog.getByRole('button', { name: '修改学习目标', exact: true }).click();
    await orderDialog.getByRole('textbox', { name: '预期成果', exact: true }).fill('用法语完成点餐和结账');
    await orderDialog.getByLabel('目标日期', { exact: true }).fill('2027-06-15');
    await orderDialog.getByRole('button', { name: '预览目标变化' }).click();
    await expect(orderDialog.getByRole('heading', { name: '预览目标变化' })).toBeVisible();
    const difference = orderDialog.getByRole('list', { name: '目标字段变化' });
    await expect(difference.getByText('完成餐馆点餐', { exact: true })).toBeVisible();
    await expect(difference.getByText('用法语完成点餐和结账', { exact: true })).toBeVisible();
    await expect(difference.getByText('2027-06-15', { exact: true })).toBeVisible();
    await orderDialog.screenshot({ path: info.outputPath('目标变更预览.png') });
    await page.setViewportSize({ width: 410, height: 850 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(410);
    await orderDialog.screenshot({ path: info.outputPath('目标变更窄屏.png') });
    await page.setViewportSize({ width: 1440, height: 900 });
    await orderDialog.getByRole('button', { name: '确认目标变更' }).click();
    await expect(orderDialog.getByText('当前路线第 3 版', { exact: true })).toBeVisible();
    await orderDialog.getByText('目标版本记录（2）', { exact: true }).click();
    const currentGoal = orderDialog.getByText('目标第 2 版', { exact: true });
    await currentGoal.click();
    await expect(currentGoal.locator('..').getByText('小餐馆', { exact: true })).toBeVisible();
    await orderDialog.getByRole('button', { name: '关闭' }).click();
    await page.reload();
    await expect(page.getByText('用法语完成点餐和结账', { exact: true })).toBeVisible();

    await page.getByRole('link', { name: '开始学习' }).click();
    await page.getByRole('button', { name: '课内助手', exact: true }).click();
    const assistant = page.getByRole('complementary', { name: '课内助手' });
    await assistant.getByLabel('对话消息').fill('我应该从哪一个目标开始？');
    await page.getByRole('button', { name: '笔记', exact: true }).click();
    await expect(assistant).toBeHidden();
    await page.getByRole('button', { name: '课内助手', exact: true }).click();
    await expect(assistant.getByLabel('对话消息')).toHaveValue('我应该从哪一个目标开始？');
    await assistant.getByRole('button', { name: '发送消息' }).click();
    await expect(assistant.getByText('我应该从哪一个目标开始？', { exact: true })).toBeVisible();
    await assistant.getByRole('button', { name: '理解后可以勾选「已学完」' }).click();
    await expect(page.getByRole('checkbox', { name: '已学完', exact: true })).toBeFocused();
    await expect(page.getByRole('checkbox', { name: '已学完', exact: true })).not.toBeChecked();
    await page.getByRole('button', { name: '课内助手', exact: true }).click();
    await assistant.getByRole('link', { name: '在聊天中打开' }).click();
    await expect(page.getByText('我应该从哪一个目标开始？', { exact: true })).toBeVisible();
    await expect(page.getByRole('link', { name: '返回关联课程' })).toBeVisible();
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

test('route practice and learning marks apply only after preview and open the saved Markdown', async ({ page }, info) => {
  test.setTimeout(50000);
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-route-practice-'));
  let service = start(folder);
  const identity = '00000000-0000-4000-8000-000000000201';
  const project = `/projects/${identity}`;
  try {
    const url = new URL(await service.url);
    const headers = { Authorization: 'Bearer ' + new URLSearchParams(url.hash.split('?')[1]).get('session') };
    const response = await page.request.post(url.origin + '/api/projects', {
      headers, data: { id: identity, topic: 'SQLite 实战', outcome: '整理学习记录' },
    });
    expect(response.status()).toBe(201);
    const original = await response.json();
    await page.goto(url.href);
    await page.evaluate(path => { window.location.hash = path; }, project);
    await expect(page.getByText('3 / 3 节已就绪', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '路线版本与顺序', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '路线版本与顺序' });
    await dialog.getByLabel('标记 理解表、行和列', { exact: true }).selectOption({ label: '待巩固' });
    await dialog.getByRole('button', { name: '添加实战', exact: true }).click();
    await dialog.getByRole('textbox', { name: '实战标题', exact: true }).fill('汇总本周学习记录');
    await dialog.getByRole('textbox', { name: '实战预期成果', exact: true }).fill('按主题交付汇总表');
    await dialog.getByRole('textbox', { name: '实战说明', exact: true }).fill('按主题汇总五条记录，并核对总时长。');
    await dialog.getByRole('combobox', { name: '插在知识点后', exact: true }).selectOption({ label: '理解表、行和列' });
    const previewResponse = page.waitForResponse(reply => reply.request().method() === 'POST'
      && new URL(reply.url()).pathname.endsWith('/route-order/preview'));
    await dialog.getByRole('button', { name: '预览路线变化', exact: true }).click();
    const difference = dialog.getByRole('region', { name: '路线变更预览' });
    await expect(difference.getByText('汇总本周学习记录', { exact: true })).toBeVisible();
    await expect(difference.getByText(/未标注.*待巩固/)).toBeVisible();
    await expect(dialog.getByText('当前路线第 1 版', { exact: true })).toBeVisible();
    await page.setViewportSize({ width: 410, height: 850 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(410);
    await dialog.screenshot({ path: info.outputPath('实战与标记预览.png') });
    const proposed = await (await previewResponse).json();
    const practice = proposed.after.find((node: { kind?: string }) => node.kind === 'practice');
    const duplicate = join(folder, 'library', 'courses', practice.id + '.md');
    writeFileSync(duplicate, '外部同名文件');
    await dialog.getByRole('button', { name: '确认路线变更', exact: true }).click();
    await expect(dialog.getByText('当前路线第 2 版', { exact: true })).toBeVisible();
    await dialog.getByRole('button', { name: '关闭', exact: true }).click();
    await expect(page.getByRole('button', { name: '理解表、行和列', exact: true })).toContainText('待巩固');
    await page.getByRole('button', { name: '汇总本周学习记录', exact: true }).click();
    await expect(page.getByRole('alert')).toContainText('实战文件写入失败');
    expect(readFileSync(duplicate, 'utf8')).toBe('外部同名文件');
    await page.getByRole('button', { name: '理解表、行和列', exact: true }).click();
    await page.getByRole('link', { name: '开始学习', exact: true }).click();
    await expect(page.getByRole('link', { name: '下一节', exact: true })).toHaveAttribute('href', `#/lessons/${original.lessons[1].id}`);
    await page.evaluate(path => { window.location.hash = path; }, project);
    await page.getByRole('button', { name: '汇总本周学习记录', exact: true }).click();
    unlinkSync(duplicate);
    await page.getByRole('alert').getByRole('button', { name: '重试', exact: true }).click();
    await page.getByRole('link', { name: '打开实战', exact: true }).click();
    await expect(page.getByText('按主题汇总五条记录，并核对总时长。', { exact: true })).toBeVisible();
    const pagePath = new URL(page.url()).hash;
    await page.getByRole('checkbox', { name: '已学完', exact: true }).click();
    await expect(page.getByRole('checkbox', { name: '已学完', exact: true })).toBeChecked();
    await stop(service.child);
    service = start(folder);
    await page.goto(await service.url);
    await page.evaluate(path => { window.location.hash = path; }, pagePath);
    await expect(page.getByRole('checkbox', { name: '已学完', exact: true })).toBeChecked();
    await expect(page.getByText('按主题汇总五条记录，并核对总时长。', { exact: true })).toBeVisible();
    await page.screenshot({ path: info.outputPath('实战重启复验.png') });
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

test('route hierarchy edits preserve old assessments and reopen the saved directory', async ({ page }, info) => {
  test.setTimeout(65000);
  page.setDefaultTimeout(8500);
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-route-grouping-'));
  let service = start(folder);
  const identity = '00000000-0000-4000-8000-000000000206';
  const project = '/projects/' + identity;
  try {
    const url = new URL(await service.url);
    const headers = { Authorization: 'Bearer ' + new URLSearchParams(url.hash.split('?')[1]).get('session') };
    async function request(path: string, method = 'GET', data?: object) {
      const reply = await page.request.fetch(url.origin + '/api' + path, { headers, method, data });
      expect(reply.ok(), await reply.text()).toBeTruthy();
      return reply.json();
    }
    const initial = await request('/projects', 'POST', { id: identity, topic: 'SQLite 学习', outcome: '整理学习记录' });
    const first = initial.lessons[0];
    const firstUrl = '/lessons/' + first.id;
    const note = '重组前留下的笔记。';
    await page.setViewportSize({ width: 410, height: 850 });
    await page.goto(url.href);
    await page.evaluate(path => { window.location.hash = path; }, project);
    await expect(page.getByText('3 / 3 节已就绪', { exact: true })).toBeVisible();
    await request(firstUrl + '/note', 'PUT', { content: note });
    await request(firstUrl + '/learned', 'PUT', { learned: true });
    const listPath = project + '/stage-assessments';
    const { items: [stage] } = await request(listPath);
    const stagePath = listPath + '/' + stage.id;
    await request(stagePath + '/attempts', 'POST', {
      id: '00000000-0000-4000-8000-000000000207', kind: 'quiz', expectedAttempt: 0, answers: [2, 0],
    });
    await page.reload();
    await page.getByRole('button', { name: '路线版本与顺序', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '路线版本与顺序' });
    await dialog.getByRole('button', { name: '重组阶段与分支', exact: true }).click();
    await dialog.getByLabel('阶段 ' + first.title, { exact: true }).selectOption({ label: '新建阶段' });
    await dialog.getByLabel('新阶段名称 ' + first.title, { exact: true }).fill('整理学习记录');
    await dialog.getByLabel('分支 ' + first.title, { exact: true }).fill('重点练习');
    await dialog.getByRole('button', { name: '预览路线变化', exact: true }).click();
    const difference = dialog.getByRole('region', { name: '路线变更预览' });
    await expect(difference.getByText('阶段测评范围', { exact: true })).toBeVisible();
    await expect(difference.getByText(/建立基础.*理解表、行和列/)).toBeVisible();
    await expect(difference.getByText(/整理学习记录.*理解表、行和列/)).toBeVisible();
    expect((await request(project)).lessons[0].phase).toBe(first.phase);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(410);
    await dialog.screenshot({ path: info.outputPath('阶段分支预览.png') });
    await dialog.getByRole('button', { name: '确认路线变更', exact: true }).click();
    await expect(dialog.getByText('当前路线第 2 版', { exact: true })).toBeVisible();
    await dialog.getByRole('button', { name: '关闭', exact: true }).click();
    const course = page.getByRole('button', { name: first.title, exact: true });
    const branch = page.locator('details.route-branch').filter({ has: page.locator('summary').getByText('重点练习', { exact: true }) });
    await expect(branch.getByRole('button', { name: first.title, exact: true })).toBeVisible();
    await branch.locator('summary').click();
    await expect(course).toBeHidden();
    await branch.locator('summary').click();
    await expect(course).toBeVisible();
    const panel = page.getByRole('region', { name: '阶段综合测评' });
    await expect(panel.getByText('历史阶段', { exact: true })).toBeVisible();
    await panel.getByRole('button', { name: /01 · 建立基础/ }).click();
    await expect(page.getByText('此测评属于已替换的阶段范围。', { exact: true })).toBeVisible();
    const historical = await request(stagePath);
    expect(historical.attemptCount).toBe(1);
    expect(historical.currentScope).toBe(false);

    await stop(service.child);
    service = start(folder);
    await page.goto(await service.url);
    await page.evaluate(path => { window.location.hash = path; }, project);
    await expect(branch.getByRole('button', { name: first.title, exact: true })).toBeVisible();
    await expect(panel.getByText('历史阶段', { exact: true })).toBeVisible();
    await page.screenshot({ path: info.outputPath('路线分支重启.png'), fullPage: true });
    await course.click();
    await page.getByRole('link', { name: '重读课程', exact: true }).click();
    await expect(page.getByRole('checkbox', { name: '已学完', exact: true })).toBeChecked();
    await expect(page.getByRole('link', { name: '下一节', exact: true })).toHaveAttribute('href', '#/lessons/' + initial.lessons[1].id);
    await page.getByRole('button', { name: '笔记', exact: true }).click();
    await page.getByRole('button', { name: 'Markdown 原文', exact: true }).click();
    await expect(page.getByLabel('本节笔记', { exact: true })).toHaveValue(note);
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

test('the course assistant previews and confirms typed and voice route actions', async ({}, info) => {
  test.setTimeout(65000);
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-course-route-actions-'));
  let service = start(folder);
  let browser: Browser | undefined;
  try {
    browser = await chromium.launch({ channel: 'chrome', args: [
      '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream',
    ] });
    const page = await browser.newPage({ viewport: { width: 410, height: 900 } });
    page.setDefaultTimeout(8000);
    let address = new URL(await service.url);
    const sessionHeaders = () => ({ Authorization: 'Bearer ' + new URLSearchParams(address.hash.split('?')[1]).get('session') });
    const auth = sessionHeaders();
    const record = await page.request.post(address.origin + '/api/projects', {
      headers: auth, data: { id: '00000000-0000-4000-8000-000000000203', topic: 'SQLite 课内路线指令', outcome: '练习初步筛选' },
    });
    expect(record.status()).toBe(201);
    const saved = await record.json();
    const ids: string[] = saved.lessons.map((lesson: { id: string }) => lesson.id);
    const project = '/api/projects/' + saved.id;
    const course = '/lessons/' + ids[1];
    const projectState = () => page.request.get(address.origin + project, { headers: sessionHeaders() }).then(result => result.json());
    const history = () => page.request.get(address.origin + project + '/versions', { headers: sessionHeaders() }).then(result => result.json());
    const order = async () => (await projectState()).lessons.map((lesson: { id: string }) => lesson.id);
    await expect.poll(async () => (await projectState()).task.status).toBe('ready');
    const note = await page.request.put(address.origin + '/api' + course + '/note', {
      headers: auth, data: { content: '这个课程的笔记属于原课程。' },
    });
    expect(note.ok()).toBe(true);
    await page.goto(address.href);
    await page.evaluate(path => { window.location.hash = path; }, course);
    await page.getByRole('button', { name: '课内助手', exact: true }).click();
    const assistant = page.getByRole('complementary', { name: '课内助手' });
    const input = assistant.getByRole('textbox', { name: '对话消息' });
    const send = assistant.getByRole('button', { name: '发送消息' });
    async function command(text: string) {
      await input.fill(text);
      await send.click();
    }

    await command('这节我已经会了。');
    const mark = page.getByRole('dialog', { name: '标记当前知识点' });
    await mark.getByRole('button', { name: '预览路线变化', exact: true }).click();
    await expect(mark.getByText(/未标注.*已会/)).toBeVisible();
    expect((await history()).routes).toHaveLength(1);
    await mark.getByRole('button', { name: '关闭', exact: true }).click();
    expect((await projectState()).lessons[1].learningMark).toBeNull();
    await command('这节我已经会了。');
    await mark.getByRole('button', { name: '预览路线变化', exact: true }).click();
    await mark.getByRole('button', { name: '确认路线变更', exact: true }).click();
    await expect(mark).toBeHidden();
    const learned = (await projectState()).lessons[1];
    expect(learned.learningMark).toBe('known');
    expect(learned.learnedOn).toBeNull();

    let transcriptions = 0;
    await page.route('**/api/voice/transcribe?language=zh', async route => {
      expect(route.request().postDataBuffer()?.subarray(0, 4).toString()).toBe('RIFF');
      transcriptions++;
      await route.fulfill({ json: { text: '把这个知识点提前。' } });
    });
    await assistant.getByRole('button', { name: '对话语音输入' }).click();
    const voice = page.getByRole('dialog', { name: '录音转文字' });
    expect(transcriptions).toBe(0);
    await voice.getByRole('button', { name: '开始录音' }).click();
    await expect(voice.getByText('正在录音', { exact: true })).toBeVisible();
    await page.waitForTimeout(420);
    await voice.getByRole('button', { name: '停止并识别' }).click();
    await expect(voice.getByRole('textbox', { name: '语音草稿' })).toHaveValue('把这个知识点提前。');
    await voice.getByRole('button', { name: '填入文字' }).click();
    await expect(input).toHaveValue('把这个知识点提前。');
    expect(await order()).toEqual(ids);
    expect(transcriptions).toBe(1);
    await send.click();
    const earlier = page.getByRole('dialog', { name: '提前当前知识点' });
    await earlier.getByRole('button', { name: '预览路线变化', exact: true }).click();
    await expect(earlier.getByText('原位置 2', { exact: true })).toBeVisible();
    expect(await order()).toEqual(ids);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(410);
    await earlier.screenshot({ path: info.outputPath('课内提前预览.png') });
    await earlier.getByRole('button', { name: '确认路线变更', exact: true }).click();
    await expect(earlier).toBeHidden();
    expect(await order()).toEqual([ids[1], ids[0], ids[2]]);
    await assistant.getByRole('button', { name: '关闭课内助手' }).click();
    const directory = page.getByRole('navigation', { name: '课程目录' });
    await directory.getByRole('button', { name: '课程目录', exact: true }).click();
    await expect(directory.getByRole('link').first()).toHaveAttribute('href', '#' + course);
    await page.getByRole('button', { name: '课内助手', exact: true }).click();

    await command('修改我的学习目标');
    const goal = page.getByRole('dialog', { name: '修改学习目标' });
    await goal.getByRole('textbox', { name: '预期成果', exact: true }).fill('完成筛选和分组');
    await goal.getByLabel('目标日期', { exact: true }).fill('2027-06-15');
    await goal.getByRole('button', { name: '预览目标变化' }).click();
    const changes = goal.getByRole('list', { name: '目标字段变化' });
    await expect(changes.getByText('练习初步筛选', { exact: true })).toBeVisible();
    await expect(changes.getByText('完成筛选和分组', { exact: true })).toBeVisible();
    await expect(changes.getByText('2027-06-15', { exact: true })).toBeVisible();
    expect((await projectState()).outcome).toBe('练习初步筛选');
    await goal.screenshot({ path: info.outputPath('课内目标预览.png') });
    await goal.getByRole('button', { name: '确认目标变更' }).click();
    await expect(goal).toBeHidden();
    expect((await projectState()).outcome).toBe('完成筛选和分组');

    await stop(service.child);
    service = start(folder);
    address = new URL(await service.url);
    await page.goto(address.href);
    await page.evaluate(path => { window.location.hash = path; }, course);
    await page.getByRole('button', { name: '课内助手', exact: true }).click();
    await expect(assistant.getByText('把这个知识点提前。', { exact: true })).toBeVisible();
    await assistant.getByRole('link', { name: '在聊天中打开' }).click();
    await expect(page.getByText('修改我的学习目标', { exact: true })).toBeVisible();
    const linkedComposer = page.getByRole('textbox', { name: '对话消息' });
    const linkedSend = page.getByRole('button', { name: '发送消息' });
    await linkedComposer.click();
    await page.keyboard.insertText('请把这节提前');
    await expect(linkedComposer).toHaveValue('请把这节提前');
    await expect(linkedSend).toBeEnabled();
    await linkedSend.click();
    await expect(earlier.getByText('当前知识点已排在首位。', { exact: true })).toBeVisible();
    await expect(earlier.getByRole('button', { name: '预览路线变化' })).toHaveCount(0);
    await earlier.getByRole('button', { name: '关闭', exact: true }).click();
    const retained = await page.request.get(address.origin + '/api' + course, { headers: sessionHeaders() });
    expect((await retained.json()).note).toBe('这个课程的笔记属于原课程。');
    expect(await order()).toEqual([ids[1], ids[0], ids[2]]);
    const versions = await history();
    expect(versions.routes.map((route: { version: number }) => route.version)).toEqual([4, 3, 2, 1]);
    expect(versions.routes[0].nodes[0].learningMark).toBe('known');
    expect(versions.routes[3].nodes.map((item: { id: string }) => item.id)).toEqual(ids);
    expect(versions.goals[0].fields.find((field: { key: string }) => field.key === versions.goalDateKey).value).toBe('2027-06-15');
  } finally {
    if (browser) await browser.close();
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

test('course directory and focus mode preserve a reading passage, note caret, and manual timer', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-reading-e2e-'));
  let service = start(folder);
  try {
    await page.goto(await service.url);
    await page.getByRole('button', { name: '体验 SQLite 样例' }).click();
    await page.getByRole('button', { name: '提交并生成路线' }).click();
    await page.getByRole('link', { name: '开始学习' }).click();
    await expect(page.getByRole('button', { name: '开始计时', exact: true })).toBeVisible();
    const route = new URL(page.url()).hash;
    const lesson = route.split('/').at(-1)!;
    const file = join(folder, 'library', 'courses', `${lesson}.md`);
    const marker = '𠮷这里是需要重新定位的同一行文字。';
    const passage = Array.from({ length: 120 }, (_, index) => index === 65
      ? marker : `学习片段 ${index} 描述了具体操作和预期结果。`).join(' ');
    const tail = '复查过程需要结合多个小节。'.repeat(110);
    const doc = `# 阅读验收样例\n\n## 小节导读\n\n用目录可以定位正文。\n\n## 长文练习\n\n${passage}\n\n## 课后回顾\n\n${tail}\n`;
    writeFileSync(file, doc, 'utf8');
    await page.reload();
    const directory = page.getByRole('navigation', { name: '课程目录' });
    await expect(directory.locator('[aria-current="page"]')).toHaveCount(1);
    await directory.getByRole('button', { name: '长文练习', exact: true }).click();
    await expect(page.getByRole('heading', { name: '长文练习' })).toBeFocused();
    const paragraph = page.locator('.reading-panel p').filter({ hasText: marker });
    const measure = (move = false) => paragraph.evaluate((element, { needle, move }) => {
      const text = element.firstChild!;
      const index = text.textContent!.indexOf(needle);
      const range = document.createRange();
      range.setStart(text, index);
      range.setEnd(text, index + needle.length);
      const top = range.getClientRects()[0].top;
      if (move) window.scrollBy({ top: top - 104, behavior: 'instant' });
      return top;
    }, { needle: marker, move });
    async function nearMarker() {
      await expect.poll(async () => Math.abs((await measure()) - 108)).toBeLessThan(64);
    }

    await page.getByRole('button', { name: '开始计时', exact: true }).click();
    await page.getByRole('button', { name: '笔记', exact: true }).click();
    await page.getByRole('button', { name: 'Markdown 原文' }).click();
    const note = page.getByLabel('本节笔记');
    await note.fill('这里有一份未保存的草稿。');
    await note.evaluate(element => (element as HTMLTextAreaElement).setSelectionRange(2, 5));
    await measure(true);
    await nearMarker();
    await page.getByRole('button', { name: '专注阅读', exact: true }).click();
    await expect(page.getByRole('navigation', { name: '主要导航' })).toBeHidden();
    await expect(directory).toBeHidden();
    await expect(note).toBeHidden();
    await expect(page.getByRole('button', { name: '课内助手', exact: true })).toBeHidden();
    await expect(page.getByRole('link', { name: '返回计时课程' })).toBeVisible();
    await nearMarker();
    await page.getByRole('button', { name: '退出专注阅读', exact: true }).click();
    await expect(directory).toBeVisible();
    await expect(note).toHaveValue('这里有一份未保存的草稿。');
    expect(await note.evaluate(element => {
      const input = element as HTMLTextAreaElement;
      return [input.selectionStart, input.selectionEnd];
    })).toEqual([2, 5]);
    await nearMarker();
    await page.getByRole('button', { name: '关闭笔记' }).click();
    await nearMarker();
    await page.getByRole('button', { name: '结束计时' }).click();

    const position = () => page.evaluate(async lesson => {
      const response = await fetch(`/api/lessons/${lesson}`, {
        headers: { Authorization: `Bearer ${sessionStorage.getItem('zhixue-session')}` },
      });
      if (!response.ok) throw new Error(`课程读取失败 (${response.status})`);
      const detail = await response.json();
      return detail.readingPosition?.point.offset ?? -1;
    }, lesson);
    await measure(true);
    await expect.poll(async () => Math.abs((await position()) - passage.indexOf(marker))).toBeLessThan(80);
    await page.getByRole('navigation', { name: '主要导航' }).getByRole('link', { name: '聊天' }).click();
    await page.goBack();
    await nearMarker();
    await expect(page.getByRole('button', { name: '开始计时', exact: true })).toBeVisible();

    writeFileSync(file, doc.replace('## 小节导读', '## 新增的开场\n\n变更原文件后保留后文。\n\n## 小节导读'), 'utf8');
    await stop(service.child);
    service = start(folder);
    await page.setViewportSize({ width: 390, height: 780 });
    await page.goto(await service.url);
    await page.evaluate(hash => { window.location.hash = hash; }, route);
    await nearMarker();
    await expect(page.getByRole('button', { name: '开始计时', exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

test('prerequisite reading reuses a course, preserves the passage, and saves independently verified help', async ({ page }) => {
  test.setTimeout(75000);
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-prerequisite-e2e-'));
  const evidence = '参数值经由准备好的占位符交给驱动，SQL 语句与值分别处理。';
  const privateLine = '原件旁边的私有纪要';
  const title = '参数占位符';
  const explanation = '用占位符把参数值和 SQL 语句拆开。';
  const requests: { model: string; prompt: string }[] = [];
  let reviews = 0;
  const provider = createServer((request, response) => {
    let body = '';
    request.setEncoding('utf8');
    request.on('data', chunk => { body += chunk; });
    request.on('end', () => {
      const input = JSON.parse(body);
      requests.push({ model: input.model, prompt: input.messages[0].content });
      const review = input.model === 'quick-review';
      const result = review ? { sections: [{ index: 0, verdict: 'supported', reason: '资料解释了参数绑定。',
        citations: [{ source: 'S1', quote: ++reviews === 1 ? '资料中缺失的引文' : evidence }], dependsOn: [],
      }] } : { sections: [{ title: '把参数值传给驱动', text: explanation, checks: ['占位符的用途'] }] };
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({ choices: [{ message: { content: JSON.stringify(result) } }] }));
    });
  });
  provider.listen(0, '127.0.0.1');
  await once(provider, 'listening');
  const address = provider.address();
  if (!address || typeof address === 'string') throw new Error('Expected a loopback port');
  const endpoint = `http://127.0.0.1:${address.port}/v1`;
  let service = start(folder);
  try {
    await page.goto(await service.url);
    await page.getByRole('link', { name: '管理中心' }).click();
    await page.getByRole('link', { name: 'API与模型' }).click();
    async function addConnection(name: string, model: string) {
      await page.getByRole('button', { name: '新增连接' }).click();
      const editor = page.getByRole('dialog');
      await editor.getByLabel('服务商').selectOption('custom');
      await editor.getByLabel('连接名称').fill(name);
      await editor.getByLabel('API 地址').fill(endpoint);
      await editor.getByLabel('模型名称').fill(model);
      await editor.getByRole('button', { name: '保存连接' }).click();
    }
    await addConnection('本机前置起草', 'quick-main');
    await addConnection('本机前置核验', 'quick-review');
    await page.getByRole('button', { name: '模型分工' }).click();
    await page.getByRole('combobox', { name: '主模型', exact: true }).selectOption({ label: '本机前置起草 · quick-main' });
    await page.getByRole('combobox', { name: '复核模型', exact: true }).selectOption({ label: '本机前置核验 · quick-review' });
    await page.getByRole('button', { name: '保存模型分工' }).click();
    await expect(page.getByText('模型分工已保存')).toBeVisible();

    await page.goto(await service.url);
    await page.getByRole('button', { name: '体验 SQLite 样例' }).click();
    await page.getByRole('button', { name: '提交并生成路线' }).click();
    await expect(page.getByText('3 / 3 节已就绪')).toBeVisible();
    await page.getByRole('link', { name: '开始学习' }).click();
    const open = page.getByRole('button', { name: '补前置知识', exact: true });
    await expect(open).toBeVisible();
    const route = new URL(page.url()).hash;
    const lesson = route.split('/').at(-1)!;
    const longText = ('这里是原课程的连续阅读文字。\n\n').repeat(50);
    writeFileSync(join(folder, 'library', 'courses', `${lesson}.md`),
      `# 原课程\n\n${longText}## 阅读定位标记\n\n${longText}`, 'utf8');
    await page.reload();
    const marker = page.getByRole('heading', { name: '阅读定位标记' });
    await marker.evaluate(element => window.scrollBy({ top: element.getBoundingClientRect().top - 147, behavior: 'instant' }));
    const scroll = () => page.evaluate(() => window.scrollY);
    const originalPosition = await scroll();
    expect(originalPosition).toBeGreaterThan(300);
    const dialog = page.getByRole('dialog', { name: '前置知识' });
    await open.click();
    await dialog.getByLabel('前置知识主题').fill('用 SELECT 筛选记录');
    await dialog.getByRole('button', { name: '查找讲解' }).click();
    await expect(dialog.getByText('来自已有课程')).toBeVisible();
    expect(requests).toHaveLength(0);
    await page.evaluate(() => window.scrollBy({ top: 290, behavior: 'instant' }));
    await dialog.getByRole('button', { name: '返回原阅读处' }).click();
    await expect.poll(async () => Math.abs((await scroll()) - originalPosition)).toBeLessThan(5);

    await open.click();
    await dialog.getByLabel('前置知识主题').fill(title);
    await dialog.getByRole('button', { name: '查找讲解' }).click();
    await dialog.getByLabel('导入核验资料').setInputFiles({
      name: '参数资料.txt', mimeType: 'text/plain', buffer: Buffer.from(`开头\n${evidence}\n${privateLine}`),
    });
    await dialog.getByLabel('页、行或段落编号').fill('2');
    await dialog.getByRole('button', { name: '加入此定位' }).click();
    await dialog.getByRole('button', { name: '预览前置讲解发送范围' }).click();
    await expect(dialog.getByText('独立核验 · 本机前置核验 · quick-review')).toBeVisible();
    expect(requests).toHaveLength(0);
    await dialog.getByRole('button', { name: '确认并生成前置讲解' }).click();
    await expect(dialog.getByRole('alert')).toContainText('暂时没有通过独立核验的小节', { timeout: 10000 });
    await dialog.getByText('查看待处理草稿', { exact: true }).click();
    await expect(dialog.getByText(explanation, { exact: true })).toBeVisible();
    await dialog.getByRole('button', { name: '继续此阶段' }).click();
    await expect(dialog.getByText(/已保存到知识库/)).toBeVisible({ timeout: 10000 });
    expect(requests.map(item => item.model)).toEqual(['quick-main', 'quick-review', 'quick-review']);
    expect(requests.every(item => item.prompt.includes(evidence) && !item.prompt.includes(privateLine))).toBe(true);
    await dialog.getByRole('button', { name: '返回原阅读处' }).click();
    await expect.poll(async () => Math.abs((await scroll()) - originalPosition)).toBeLessThan(5);
    await expect(page.getByRole('checkbox', { name: '已学完', exact: true })).not.toBeChecked();
    await expect(page.getByRole('navigation', { name: '课程目录' }).getByRole('link')).toHaveCount(3);

    await stop(service.child);
    service = start(folder);
    await page.setViewportSize({ width: 390, height: 780 });
    await page.goto(await service.url);
    await page.evaluate(hash => { window.location.hash = hash; }, route);
    await open.click();
    await dialog.getByLabel('前置知识主题').fill(title);
    await dialog.getByRole('button', { name: '查找讲解' }).click();
    await expect(dialog.getByText(explanation, { exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
    await dialog.getByRole('link', { name: '在知识库打开' }).click();
    await expect(page.getByRole('heading', { name: '知识库' })).toBeVisible();
    await expect(page.getByText(explanation, { exact: true })).toBeVisible();
    expect(requests).toHaveLength(3);
  } finally {
    await stop(service.child);
    const closed = once(provider, 'close');
    provider.close();
    await closed;
    rmSync(folder, { recursive: true, force: true });
  }
});

test('backup preview recovers a knowledge file as a copy and keeps its catalog after restart', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-backup-e2e-'));
  const password = '隔离测试的备份密码';
  let service = start(folder);
  try {
    await page.goto(await service.url);
    await page.getByRole('link', { name: '知识库', exact: true }).click();
    await page.getByLabel('导入资料').setInputFiles({
      name: '回忆资料.txt', mimeType: 'text/plain', buffer: Buffer.from('只在备份里的资料。'),
    });
    await expect(page.getByText('只在备份里的资料。', { exact: true })).toBeVisible();
    const folderOfImports = join(folder, 'library', 'imports');
    const original = join(folderOfImports, readdirSync(folderOfImports)[0]);

    await page.getByRole('link', { name: '管理中心', exact: true }).click();
    await page.getByRole('link', { name: '备份与恢复', exact: true }).click();
    await page.getByRole('button', { name: '创建加密备份', exact: true }).click();
    const dialog = page.getByRole('dialog');
    await dialog.getByLabel('备份密码').fill(password);
    await dialog.getByRole('button', { name: '开始备份', exact: true }).click();
    await expect(page.getByText('已创建加密备份', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '永久保留', exact: true }).click();
    await expect(page.getByRole('button', { name: '取消永久保留', exact: true })).toBeVisible();
    writeFileSync(original, '原件已更新。');

    await page.getByRole('button', { name: '预览备份', exact: true }).click();
    await dialog.getByLabel('备份密码').fill(password);
    await dialog.getByRole('button', { name: '解锁并比较', exact: true }).click();
    await expect(dialog.getByRole('heading', { name: '整体恢复预览', exact: true })).toBeVisible();
    await dialog.getByRole('button', { name: '找回文件', exact: true }).click();
    await dialog.getByRole('button', { name: '选取 回忆资料.txt', exact: true }).click();
    await expect(dialog.getByText('当前文件内容不同', { exact: true })).toBeVisible();
    await dialog.getByRole('button', { name: '确认找回副本', exact: true }).click();
    await dialog.getByRole('link', { name: '打开找回副本', exact: true }).click();
    await expect(page.getByText('只在备份里的资料。', { exact: true })).toBeVisible();
    expect(readFileSync(original, 'utf8')).toBe('原件已更新。');

    await stop(service.child);
    service = start(folder);
    await page.setViewportSize({ width: 410, height: 820 });
    await page.goto(await service.url);
    await page.getByRole('link', { name: '管理中心', exact: true }).click();
    await page.getByRole('link', { name: '备份与恢复', exact: true }).click();
    await expect(page.getByRole('button', { name: '取消永久保留', exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(410);
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

test('portable restore switches the full workspace and retains a usable pre-restore snapshot', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-full-restore-e2e-'));
  const receiver = join(folder, 'receiver');
  const media = join(folder, 'media');
  const password = '换机测试使用的独立密码';
  let service = start(join(folder, 'producer'));

  async function connect() {
    const url = new URL(await service.url);
    const headers = { Authorization: 'Bearer ' + new URLSearchParams(url.hash.split('?')[1]).get('session') };
    return { url, headers, api: url.origin + '/api' };
  }

  async function seed(device: Awaited<ReturnType<typeof connect>>, id: string, topic: string, name: string, body: string) {
    const { headers, api } = device;
    const created = await page.request.post(api + '/projects', { headers, data: { id, topic, outcome: '核对换机数据' } });
    expect(created.status()).toBe(201);
    const project = await created.json();
    await expect.poll(async () => {
      const response = await page.request.get(api + '/projects/' + id, { headers });
      return (await response.json()).task.status;
    }).toBe('ready');
    const lessonId: string = project.lessons[0].id;
    const importFile = await page.request.post(api + '/files/upload', {
      headers: { ...headers, 'Content-Type': 'text/plain' }, params: { name }, data: Buffer.from(body),
    });
    expect(importFile.ok()).toBeTruthy();
    return { lessonId, fileId: (await importFile.json()).id as string };
  }

  async function preview(button: ReturnType<typeof page.getByRole>) {
    await button.click();
    const dialog = page.getByRole('dialog');
    await dialog.getByLabel('备份密码').fill(password);
    await dialog.getByRole('button', { name: '解锁并比较', exact: true }).click();
    await expect(dialog.getByRole('heading', { name: '整体恢复预览', exact: true })).toBeVisible();
    return dialog;
  }

  try {
    const sourceId = '00000000-0000-4000-8000-000000000051';
    const targetId = '00000000-0000-4000-8000-000000000052';
    const producer = await connect();
    const source = await seed(producer, sourceId, '原设备学习项目', '已备份资料.txt', '从旧设备完整迁入的资料。');
    expect((await page.request.put(producer.api + '/lessons/' + source.lessonId + '/learned', {
      headers: producer.headers, data: { learned: true },
    })).ok()).toBeTruthy();
    expect((await page.request.put(producer.api + '/backups/destination', {
      headers: producer.headers, data: { directory: media },
    })).ok()).toBeTruthy();
    const saved = await page.request.post(producer.api + '/backups', { headers: producer.headers, data: { password } });
    expect(saved.status()).toBe(201);
    await stop(service.child);

    service = start(receiver);
    const target = await connect();
    const local = await seed(target, targetId, '恢复前的设备项目', '本机新增资料.txt', '只在恢复前设备上的资料。');
    expect((await page.request.put(target.api + '/backups/destination', {
      headers: target.headers, data: { directory: media },
    })).ok()).toBeTruthy();
    const timer = await page.request.post(target.api + '/timer', {
      headers: target.headers, data: { action: 'start', lessonId: local.lessonId },
    });
    expect(timer.ok()).toBeTruthy();
    const timerId = (await timer.json()).active.id;
    await page.setViewportSize({ width: 410, height: 820 });
    await page.goto(target.url.href);
    await page.getByRole('link', { name: '管理中心', exact: true }).click();
    await page.getByRole('link', { name: '备份与恢复', exact: true }).click();

    const dialog = await preview(page.getByRole('button', { name: '预览备份', exact: true }));
    await expect(dialog.getByText('本机新增资料.txt', { exact: true })).toBeVisible();
    const advance = dialog.getByRole('button', { name: '继续整体恢复', exact: true });
    await expect(advance).toBeDisabled();
    await dialog.getByRole('button', { name: '暂存计时', exact: true }).click();
    await expect(advance).toBeEnabled();
    const timerState = await page.request.get(target.api + '/timer', { headers: target.headers });
    expect(await timerState.json()).toMatchObject({ active: null, stashed: [{ id: timerId, status: 'stashed' }] });
    await advance.click();
    const [submitted] = await Promise.all([
      page.waitForResponse(response => response.url().endsWith('/restore') && response.request().method() === 'POST'),
      dialog.getByRole('button', { name: '确认整体恢复', exact: true }).click(),
    ]);
    expect(submitted.status()).toBe(202);
    await page.reload();

    const complete = page.getByText('整体恢复完成。恢复前的数据已保存为加密备份。', { exact: true });
    await expect(complete).toBeVisible({ timeout: 12000 });
    const snapshot = page.getByRole('article').filter({ hasText: '恢复前快照' });
    await expect(snapshot.getByRole('button', { name: '取消永久保留', exact: true })).toBeVisible();
    const old = await preview(snapshot.getByRole('button', { name: '预览备份', exact: true }));
    await expect(old.getByText('本机新增资料.txt', { exact: true })).toBeVisible();
    await page.keyboard.press('Escape');

    const result = await page.request.get(target.api + '/projects/' + sourceId, { headers: target.headers });
    expect(result.ok()).toBeTruthy();
    expect((await result.json()).lessons[0].learnedOn).toBeTruthy();
    expect((await page.request.get(target.api + '/projects/' + targetId, { headers: target.headers })).status()).toBe(404);
    await seed(target, '00000000-0000-4000-8000-000000000053', '恢复后新建的项目', '新项目资料.txt', '恢复后可继续使用。');

    await stop(service.child);
    service = start(receiver);
    const restarted = await connect();
    const restorePage = new URL(restarted.url);
    restorePage.hash = restarted.url.hash.replace('#/?', '#/settings/backups?');
    await page.goto(restorePage.href);
    await expect(complete).toBeVisible();
    const restored = await page.request.get(restarted.api + '/files/' + source.fileId, { headers: restarted.headers });
    expect(restored.ok()).toBeTruthy();
    expect((await restored.json()).content).toBe('从旧设备完整迁入的资料。');
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(410);
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

const testAutomaticBackupLauncher = `
from datetime import datetime
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path.cwd()))
role, fixture = sys.argv[1:3]
sys.argv = [sys.argv[0], *sys.argv[3:]]
fixture = Path(fixture)

from backend.backup_automatic import AutomaticBackups

class DemoVault:
    def get(self, reference):
        path = fixture / "vault" / reference
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def put(self, reference, password):
        (fixture / "vault" / reference).write_text(password, encoding="utf-8")

    def discard(self, reference):
        (fixture / "vault" / reference).unlink(missing_ok=True)

if role == "worker":
    from backend import worker

    class ClockedBackups(AutomaticBackups):
        def __init__(self, catalog):
            super().__init__(catalog, DemoVault())

        def check(self, engine):
            try:
                stamp = datetime.fromisoformat((fixture / "clock.txt").read_text(encoding="utf-8"))
            except ValueError:
                return
            super().check(engine, stamp)

    worker.AutomaticBackups = ClockedBackups
    worker.CHECK_INTERVAL = .35
    worker.main()
else:
    from backend import launch
    from backend.app import create_app

    popen = subprocess.Popen

    def start_isolated_worker(command, **options):
        if command[1:3] == ["-m", "backend.worker"]:
            command = [sys.executable, __file__, "worker", str(fixture), command[-1]]
        return popen(command, **options)

    launch.subprocess.Popen = start_isolated_worker
    launch.create_app = lambda folder, token, **options: create_app(
        folder, token, backup_credentials=DemoVault(), **options
    )
    raise SystemExit(launch.main())
`;

test('automatic backups run in the worker across day boundaries and keep each original password', async ({ page }) => {
  const fixture = mkdtempSync(join(tmpdir(), 'zhixue-automatic-backup-e2e-'));
  const data = join(fixture, 'data');
  const launcher = join(fixture, 'scheduled_backup_launcher.py');
  const clock = join(fixture, 'clock.txt');
  const vault = join(fixture, 'vault');
  const firstPassword = '隔离测试的第一个密码';
  const nextPassword = '隔离测试的轮换密码';
  mkdirSync(vault);
  writeFileSync(launcher, testAutomaticBackupLauncher);
  writeFileSync(clock, '2026-09-27T15:56:00+00:00');
  const startBackupService = () => startService(data, [launcher, 'server', fixture]);
  let service = startBackupService();

  async function openBackups(address: string) {
    const url = new URL(address);
    url.hash = url.hash.replace('#/?', '#/settings/backups?');
    await page.goto(url.href);
    await expect(page.getByRole('heading', { name: '备份与恢复', exact: true })).toBeVisible();
  }

  async function setPassword(button: string, password: string, submit: string) {
    await page.getByRole('button', { name: button, exact: true }).click();
    const dialog = page.getByRole('dialog');
    await dialog.getByLabel('自动备份密码', { exact: true }).fill(password);
    const confirmation = dialog.getByLabel('确认自动备份密码', { exact: true });
    await confirmation.fill('不一致');
    const save = dialog.getByRole('button', { name: submit, exact: true });
    await expect(save).toBeDisabled();
    await confirmation.fill(password);
    await save.click();
    await expect(dialog).toHaveCount(0);
  }

  try {
    await page.setViewportSize({ width: 410, height: 820 });
    await openBackups(await service.url);
    await setPassword('设置备份密码', firstPassword, '启用自动备份');
    await expect(page.getByText('创建完成 · 自动', { exact: true })).toHaveCount(1, { timeout: 12000 });
    await setPassword('更改自动备份密码', nextPassword, '保存新密码');
    await stop(service.child);

    service = startBackupService();
    const restarted = new URL(await service.url);
    const session = new URLSearchParams(restarted.hash.split('?')[1]).get('session');
    const headers = { Authorization: 'Bearer ' + session };
    writeFileSync(clock, '2026-09-27T16:03:00+00:00');
    await expect.poll(async () => {
      const response = await page.request.get(restarted.origin + '/api/backups/automatic', { headers });
      expect(response.ok()).toBeTruthy();
      return (await response.json()).lastState;
    }, { timeout: 10000 }).toBe('ready');

    await openBackups(restarted.href);
    await expect(page.getByText('创建完成 · 自动', { exact: true })).toHaveCount(2);
    const items = page.getByRole('article').filter({ hasText: '创建完成 · 自动' });
    for (const [index, password] of [[1, firstPassword], [0, nextPassword]] as const) {
      await items.nth(index).getByRole('button', { name: '预览备份', exact: true }).click();
      const dialog = page.getByRole('dialog');
      await dialog.getByLabel('备份密码', { exact: true }).fill(password);
      await dialog.getByRole('button', { name: '解锁并比较', exact: true }).click();
      await expect(dialog.getByRole('heading', { name: '整体恢复预览', exact: true })).toBeVisible();
      await page.keyboard.press('Escape');
    }
    await page.getByRole('button', { name: '关闭自动备份', exact: true }).click();
    await expect(page.getByRole('button', { name: '设置备份密码', exact: true })).toBeVisible();
    await expect(page.getByText('创建完成 · 自动', { exact: true })).toHaveCount(2);
    expect(readdirSync(vault)).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(410);
  } finally {
    await stop(service.child);
    rmSync(fixture, { recursive: true, force: true });
  }
});

test('model settings probe one local request and preserve independent roles across restart', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-model-settings-'));
  const requests: { target?: string; body: unknown; authorization?: string }[] = [];
  const provider = createServer((request, response) => {
    let body = '';
    request.setEncoding('utf8');
    request.on('data', chunk => { body += chunk; });
    request.on('end', () => {
      requests.push({ target: request.url, body: JSON.parse(body), authorization: request.headers.authorization });
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({ choices: [{ message: { content: 'OK' } }], usage: { prompt_tokens: 4, completion_tokens: 1 } }));
    });
  });
  provider.listen(0, '127.0.0.1');
  await once(provider, 'listening');
  const bind = provider.address();
  if (!bind || typeof bind === 'string') throw new Error('Expected a loopback port');
  const address = `http://127.0.0.1:${bind.port}/v1`;
  let service = start(folder);
  try {
    await page.goto(await service.url);
    await page.getByRole('link', { name: '管理中心' }).click();
    await page.getByRole('link', { name: 'API与模型' }).click();
    await page.getByRole('button', { name: '新增连接' }).click();
    const editor = page.getByRole('dialog');
    await expect(editor).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(editor).toHaveCount(0);
    await page.getByRole('button', { name: '新增连接' }).click();
    await expect(editor.getByLabel('服务商')).toHaveValue('minimax');
    await expect(editor.getByLabel('API 地址')).toBeEmpty();
    await editor.getByLabel('连接名称').fill('主要连接');
    await editor.getByLabel('API 地址').fill('https://main.example.invalid/v1');
    await editor.getByLabel('模型名称').fill('main-local-fixture');
    await editor.getByRole('button', { name: '保存连接' }).click();
    await expect(page.getByText('主要连接', { exact: true })).toBeVisible();

    await page.getByRole('button', { name: '新增连接' }).click();
    await editor.getByLabel('服务商').selectOption('custom');
    await editor.getByLabel('连接名称').fill('独立复核');
    await editor.getByLabel('API 地址').fill(address);
    await editor.getByLabel('模型名称').fill('review-local-fixture');
    await editor.getByRole('button', { name: '保存连接' }).click();
    const reviewConnection = page.getByRole('article', { name: '独立复核' });
    await reviewConnection.getByRole('button', { name: '测试连接' }).click();
    await expect(page.getByRole('dialog').getByText(`${address}/chat/completions`)).toBeVisible();
    expect(requests).toHaveLength(0);
    await page.getByRole('dialog').getByRole('button', { name: '开始测试' }).click();
    await expect(reviewConnection.getByText('文本回复通过')).toBeVisible();
    expect(requests).toEqual([{
      target: '/v1/chat/completions',
      body: { model: 'review-local-fixture', messages: [{ role: 'user', content: 'Reply with OK.' }], max_tokens: 512, stream: false },
      authorization: undefined,
    }]);

    await page.getByRole('button', { name: '模型分工' }).click();
    const mainRole = page.getByRole('combobox', { name: '主模型', exact: true });
    const reviewRole = page.getByRole('combobox', { name: '复核模型', exact: true });
    const mainBackup = page.getByRole('combobox', { name: '主模型备用连接', exact: true });
    await mainRole.selectOption({ label: '主要连接 · main-local-fixture' }, { timeout: 5000 });
    await reviewRole.selectOption({ label: '独立复核 · review-local-fixture' }, { timeout: 5000 });
    await mainBackup.selectOption({ label: '独立复核 · review-local-fixture' }, { timeout: 5000 });
    await page.getByRole('button', { name: '保存模型分工' }).click();
    await expect(page.getByText('模型分工已保存')).toBeVisible();
    await stop(service.child);
    service = start(folder);
    await page.goto(await service.url);
    await page.getByRole('link', { name: '管理中心' }).click();
    await page.getByRole('link', { name: 'API与模型' }).click();
    await page.getByRole('button', { name: '模型分工' }).click();
    await expect(mainRole.locator('option:checked')).toHaveText('主要连接 · main-local-fixture');
    await expect(reviewRole.locator('option:checked')).toHaveText('独立复核 · review-local-fixture');
    await expect(mainBackup.locator('option:checked')).toHaveText('独立复核 · review-local-fixture');
    await expect(page.getByRole('combobox', { name: '复核备用连接', exact: true })).toHaveValue('');
    await expect(page.getByText('文本回复通过', { exact: true })).toHaveCount(2);
    await page.setViewportSize({ width: 430, height: 820 });
    await expect(page.getByRole('heading', { name: 'API与模型' })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(430);
    expect(requests).toHaveLength(1);
  } finally {
    await stop(service.child);
    const closed = once(provider, 'close');
    provider.close();
    await closed;
    rmSync(folder, { recursive: true, force: true });
  }
});

test('verified course runs formal assessment, focused practice, remediation, and learned Q&A consolidation', async ({ page }) => {
  test.setTimeout(110000);
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-model-route-e2e-'));
  const prompts: string[] = [];
  const noteChecks: { model: string; prompt: string }[] = [];
  const noteFocus = '我的练习应回到合成资料中可核对的原句。';
  const unsupportedNote = '这条资料支持全部练习都改用另一份样例。';
  const privateNote = '仅保留在笔记里的课后计划🙂。';
  const evidence = '这是供端到端测试核对的合成资料原句。';
  const privateLine = '这行私有内容未获授权。';
  const bodyCopy = '𠮷合成草稿正文。使用 \x60SELECT 1;\x60 和 $a^2+b^2=c^2$。';
  const interpretation = '先比较这段讲解和资料原句，再复述检查步骤。';
  const additionTitle = '答疑整理 · 如何复述合成检查';
  const additionText = '使用已核验的句子说明检查依据，再用自己的话概括。';
  const consolidationModels: string[] = [];
  let consolidationChecks = 0;
  const { promise: firstGradeGate, resolve: releaseFirstGrade } = Promise.withResolvers<void>();
  const practicePrompts: string[] = [];
  const practiceQuestion = '如何把合成资料的原句用于一条新的检查记录？';
  const practiceAnswer = '专项作答：写下合成资料原句并说明检查依据。';
  const lessons = Array.from({ length: 8 }, (_, index) => ({
    key: `k${index}`, title: `昆曲知识点 ${index + 1}`, outcome: `完成曲牌练习 ${index + 1}`,
    prerequisites: index === 0 ? [] : [`k${index - 1}`], branch: null, required: true,
  }));
  const provider = createServer((request, response) => {
    let body = '';
    request.setEncoding('utf8');
    request.on('data', chunk => { body += chunk; });
    request.on('end', () => {
      const input = JSON.parse(body);
      const prompt: string = input.messages[0].content;
      const verifyingNotes = prompt.includes('笔记核验 JSON');
      const draftingPractice = prompt.startsWith('设计一组新的复习变式题');
      const scoringPractice = prompt.startsWith('按每道题的标准评价整组答案') && prompt.includes(practiceAnswer);
      if (verifyingNotes) noteChecks.push({ model: input.model, prompt });
      else if (draftingPractice || scoringPractice) practicePrompts.push(prompt);
      else prompts.push(prompt);
      const findings = {
        sections: [
          { index: 0, verdict: 'supported', reason: '合成样例与资料相符。', citations: [{ source: 'S1', quote: evidence }], dependsOn: [] },
          { index: 1, verdict: 'disputed', reason: '合成资料没有覆盖这项结论。', citations: [], dependsOn: [] },
        ],
      };
      const draft = { sections: [
        { title: '合成教学示例', text: bodyCopy, checks: ['合成核对项'] },
        { title: '待处理的合成结论', text: '这段争议只供人工判断。', checks: [] },
      ] };
      const grade = {
        items: [
          { index: 0, met: input.model !== 'review-fixture', reason: '合成核心能力评价。' },
          { index: 1, met: false, reason: '合成补充建议。' },
        ], explanation: '已完成合成的整组评价。',
      };
      const assessment = { objectives: [{
        title: '说明合成检查', ability: 'apply', criterion: '说明测试原句提供的检查依据',
        group: { hint: '想一想刚读到的检查依据。', questions: [
          { text: '这个例子用了什么检查依据？', criterion: '提到资料原句', required: true, quote: evidence },
          { text: '还可以怎样复述检查？', criterion: '用自己的话解释', required: false, quote: evidence },
        ] },
      }] };
      const scoring = prompt.includes('评价整组答案') || prompt.includes('独立重新核查');
      const supplementing = prompt.includes('为答错的小题起草错因补学');
      const checking = prompt.includes('独立核验错因补学草稿');
      const supplementInputs = supplementing || checking ? JSON.parse(prompt.split('\n补学材料 JSON\n')[1]) : null;
      const supplementalDraft = supplementing ? { entries: supplementInputs.missedQuestions.map((item: { index: number }) => ({
        index: item.index, cause: '少说明了检查依据。', explanation: '对照合成资料原句，再用自己的话复述检查步骤。',
      })) } : null;
      const supplementalReview = checking ? { sections: supplementInputs.draft.map((item: { index: number }) => ({
        index: item.index, verdict: 'supported', reason: '此合成补充讲解有例句支持。',
        citations: [{ source: 'S1', quote: evidence }], dependsOn: [],
      })) } : null;
      const answering = prompt.includes('答疑上下文 JSON');
      const answer = answering ? { answer: interpretation, support: 'grounded', citations: [{ source: 'S1', quote: evidence }] } : null;
      const consolidating = prompt.includes('整理同一节课的答疑');
      const verifyingConsolidation = prompt.includes('独立核验学完后的答疑补充');
      if (consolidating || verifyingConsolidation) consolidationModels.push(input.model);
      const addition = consolidating ? { section: { title: additionTitle, text: additionText, checks: ['说明原句的检查依据'] } } : null;
      const additionReview = verifyingConsolidation ? {
        verdict: 'supported',
        reason: '合成检查依据可以定位。',
        citations: [{ source: 'S1', quote: ++consolidationChecks === 1 ? '未出现在资料中的引文' : evidence }],
      } : null;
      const noteDecisions = verifyingNotes ? { decisions: [
        { key: 'N1', verdict: 'supported', reason: '合成资料支持使用原句。', evidence: { section: 0, quote: evidence } },
        { key: 'N2', verdict: 'insufficient', reason: '资料缺少这项依据。', evidence: null },
      ] } : null;
      const originalGroup = assessment.objectives[0].group;
      const practiceGroup = { ...originalGroup, questions: [{ ...originalGroup.questions[0], text: practiceQuestion }] };
      const practiceGrade = { items: [grade.items[0]], explanation: '已完成独立的合成专项评价。' };
      const result = noteDecisions ?? addition ?? additionReview ?? answer ?? supplementalDraft ?? supplementalReview
        ?? (draftingPractice ? practiceGroup : scoringPractice ? practiceGrade : scoring ? grade : prompt.includes('拟定可独立检验') ? assessment
        : input.model === 'review-fixture' ? findings : prompt.includes('制定学习路线')
        ? { stages: [{ name: '曲牌基础', lessons: lessons.slice(0, 4) }, { name: '曲牌练习', lessons: lessons.slice(4) }] }
        : draft);
      function reply() {
        response.writeHead(200, { 'Content-Type': 'application/json' });
        response.end(JSON.stringify({
          choices: [{ message: { content: JSON.stringify(result) } }],
          usage: { prompt_tokens: 40, completion_tokens: 20 },
        }));
      }
      if (prompt.includes('评价整组答案')) void firstGradeGate.then(reply);
      else if (scoring) setTimeout(reply, 1400);
      else reply();
    });
  });
  provider.listen(0, '127.0.0.1');
  await once(provider, 'listening');
  const bind = provider.address();
  if (!bind || typeof bind === 'string') throw new Error('Expected a loopback port');
  const address = `http://127.0.0.1:${bind.port}/v1`;
  let service = start(folder);
  try {
    await page.goto(await service.url);
    await page.getByRole('link', { name: '管理中心' }).click();
    await page.getByRole('link', { name: 'API与模型' }).click();
    await page.getByRole('button', { name: '新增连接' }).click();
    const editor = page.getByRole('dialog');
    await editor.getByLabel('服务商').selectOption('custom');
    await editor.getByLabel('连接名称').fill('本地路线测试');
    await editor.getByLabel('API 地址').fill(address);
    await editor.getByLabel('模型名称').fill('route-fixture');
    await editor.getByRole('button', { name: '保存连接' }).click();
    await page.getByRole('button', { name: '新增连接' }).click();
    await editor.getByLabel('服务商').selectOption('custom');
    await editor.getByLabel('连接名称').fill('本机独立复核');
    await editor.getByLabel('API 地址').fill(address);
    await editor.getByLabel('模型名称').fill('review-fixture');
    await editor.getByRole('button', { name: '保存连接' }).click();
    await page.getByRole('button', { name: '模型分工' }).click();
    await page.getByRole('combobox', { name: '主模型', exact: true }).selectOption({ label: '本地路线测试 · route-fixture' });
    await page.getByRole('combobox', { name: '复核模型', exact: true }).selectOption({ label: '本机独立复核 · review-fixture' });
    await page.getByRole('button', { name: '保存模型分工' }).click();
    await expect(page.getByText('模型分工已保存')).toBeVisible();

    await page.goto(await service.url);
    await page.getByLabel('学习问题或目标').fill('我想学习昆曲唱腔');
    await page.getByRole('button', { name: '开始探索' }).click();
    await page.getByLabel('添加资料').setInputFiles({
      name: '曲牌证据.txt', mimeType: 'text/plain', buffer: Buffer.from(`资料行一\n${evidence}\n${privateLine}`),
    });
    await expect(page.getByRole('link', { name: '曲牌证据.txt' })).toBeVisible();
    await page.getByRole('button', { name: '整理为学习目标' }).click();
    await page.getByLabel('预期成果', { exact: true }).fill('读懂一段昆曲唱词');
    await page.getByRole('button', { name: '用主模型生成路线' }).click();
    const consent = page.getByRole('dialog', { name: '确认模型发送范围' });
    await consent.getByLabel('本任务调用次数上限').fill('2');
    await consent.getByRole('button', { name: '查看发送范围' }).click();
    await expect(consent.getByText(`${address}/chat/completions`)).toBeVisible();
    await expect(consent.getByText('读懂一段昆曲唱词', { exact: true })).toBeVisible();
    expect(prompts).toHaveLength(0);
    await consent.getByRole('button', { name: '确认并生成路线' }).click();
    await expect(page.getByRole('button', { name: '昆曲知识点 8', exact: true })).toBeVisible({ timeout: 15000 });
    await expect(page.getByRole('button', { name: '昆曲知识点 1', exact: true })).toHaveAttribute('aria-pressed', 'true');
    const preview = page.getByRole('button', { name: '查看待核验草稿' });
    await expect(preview).toBeVisible({ timeout: 15000 });
    await expect(page.getByText(/已计入 2 次调用/)).toBeVisible();
    const projectRoute = new URL(page.url()).hash;
    expect(projectRoute).toMatch(/^#\/projects\//);
    await stop(service.child);
    service = start(folder);
    await page.goto(await service.url);
    await page.evaluate(hash => { window.location.hash = hash; }, projectRoute);
    await preview.click();
    const draft = page.getByRole('dialog', { name: '待核验草稿' });
    await expect(draft.getByText(bodyCopy, { exact: true })).toBeVisible();
    await draft.getByText('待核查事项', { exact: true }).click();
    await expect(draft.getByText('合成核对项', { exact: true })).toBeVisible();
    await expect(page.getByRole('link', { name: '开始学习' })).toHaveCount(0);
    expect(prompts).toHaveLength(2);
    expect(prompts[0]).toContain('读懂一段昆曲唱词');
    expect(prompts[1]).toContain('昆曲知识点 1');
    expect(prompts.some(prompt => prompt.includes(evidence))).toBe(false);
    await draft.getByRole('button', { name: '关闭' }).click();
    await page.getByRole('button', { name: '选择复核资料', exact: true }).click();
    const scope = page.getByRole('dialog', { name: '选择独立复核资料' });
    await scope.getByLabel('知识库资料').selectOption({ label: '曲牌证据.txt' });
    await scope.getByLabel('页、行或段落编号').fill('2');
    await scope.getByRole('button', { name: '加入此定位' }).click();
    await scope.getByRole('button', { name: '预览复核发送范围' }).click();
    await expect(scope.getByText('本机独立复核 · review-fixture')).toBeVisible();
    await expect(scope.getByText(evidence, { exact: true }).last()).toBeVisible();
    expect(prompts).toHaveLength(2);
    await scope.getByRole('button', { name: '确认并启用复核' }).click();

    await page.getByText('修改任务调用额度').click();
    await page.getByLabel('新的调用次数上限').fill('3');
    await page.getByRole('button', { name: '应用额度并继续' }).click();
    const read = page.getByRole('link', { name: '开始学习' });
    await expect(read).toBeVisible({ timeout: 15000 });
    await read.click();
    await expect(page.getByRole('heading', { name: '合成教学示例' })).toBeVisible();
    await expect(page.getByText('这段争议只供人工判断。')).toHaveCount(0);
    const notes = page.locator('.note-panel');
    const note = notes.getByRole('textbox', { name: '本节笔记' });
    await page.locator('p[data-course-section="0"]').evaluate(paragraph => {
      const range = document.createRange();
      range.selectNodeContents(paragraph);
      const selection = window.getSelection()!;
      selection.removeAllRanges();
      selection.addRange(range);
    });
    await page.getByRole('button', { name: '摘录到笔记', exact: true }).click();
    await expect(note.locator('p code')).toHaveText('SELECT 1;');
    await expect(note.locator('[data-type="inline-math"] annotation')).toHaveText('a^2+b^2=c^2');
    await notes.getByRole('button', { name: '保存笔记' }).click();
    await expect(notes.getByText('笔记已保存')).toBeVisible();
    await notes.getByRole('button', { name: '关闭笔记' }).click();
    await page.getByText('来源与独立复核记录', { exact: true }).click();
    await page.getByRole('link', { name: '曲牌证据.txt · 第 2 行', exact: true }).click();
    await expect(page.getByText('第 2 行', { exact: true })).toBeVisible();
    await page.goBack();
    await page.getByRole('link', { name: '查看课程争议' }).click();
    await expect(page.getByRole('heading', { name: '争议待处理' })).toBeVisible();
    await expect(page.getByText('合成资料没有覆盖这项结论。')).toBeVisible();
    await page.getByRole('button', { name: '采纳进正文', exact: true }).click();
    const adoption = page.getByRole('dialog', { name: '采纳进正文' });
    await adoption.getByRole('button', { name: '确认采纳进正文' }).click();
    await expect(page.getByText('没有符合条件的争议')).toBeVisible();
    await page.getByLabel('处理状态').selectOption('adopted');
    await page.getByRole('link', { name: '查看当前课程' }).click();
    await expect(page.getByText('用户采纳，尚未核验通过', { exact: true })).toBeVisible();
    const versionHistory = page.getByRole('dialog', { name: '已保存课程版本' });
    await page.getByRole('button', { name: '笔记', exact: true }).click();
    await note.getByRole('link', { name: '查看原文 · 第 1 版' }).click();
    await expect(versionHistory.locator('mark')).toHaveText(bodyCopy);
    await expect(versionHistory.getByText('这段争议只供人工判断。')).toHaveCount(0);
    await versionHistory.getByRole('button', { name: '关闭' }).click();
    await notes.getByRole('button', { name: '关闭笔记' }).click();
    await page.getByRole('button', { name: '课程版本 · 第 2 版' }).click();
    await versionHistory.getByLabel('课程版本').selectOption({ label: '第 1 版' });
    await expect(versionHistory.getByText(bodyCopy, { exact: true })).toBeVisible();
    await expect(versionHistory.getByText('这段争议只供人工判断。')).toHaveCount(0);
    expect(prompts).toHaveLength(3);
    expect(prompts[2]).toContain(evidence);
    expect(prompts[2]).not.toContain(privateLine);
    await versionHistory.getByRole('button', { name: '关闭' }).click();
    await page.getByRole('checkbox', { name: '已学完', exact: true }).click();
    const noteFixture = await page.evaluate(async ({ noteFocus, unsupportedNote, privateNote }) => {
      const identity = crypto.randomUUID();
      const headers = { Authorization: 'Bearer ' + sessionStorage.getItem('zhixue-session'), 'Content-Type': 'application/json' };
      const create = await fetch('/api/notes', { method: 'POST', headers,
        body: JSON.stringify({ id: identity, title: '独立复习札记', lessonIds: [] }) });
      if (!create.ok) return false;
      const path = '/api/notes/' + identity;
      const current = await (await fetch(path, { headers })).json();
      const content = [privateNote, noteFocus, unsupportedNote].join('\r\n\r\n');
      const save = await fetch(path, { method: 'PUT', headers, body: JSON.stringify({ baseRevision: current.revision, content }) });
      return save.ok ? identity : null;
    }, { noteFocus, unsupportedNote, privateNote });
    expect(noteFixture).toBeTruthy();
    await page.getByRole('button', { name: '启用本节模型复习' }).click();
    const assessmentConsent = page.getByRole('dialog', { name: '确认复习发送范围' });
    await assessmentConsent.getByRole('checkbox', { name: '自动生成并核验错因补学' }).check();
    const originalViewport = page.viewportSize()!;
    await page.setViewportSize({ width: 410, height: 840 });
    await assessmentConsent.getByText('选用个人笔记片段', { exact: true }).click();
    await assessmentConsent.getByRole('combobox', { name: '选择笔记' }).selectOption({ label: '独立复习札记' });
    const noteInput = assessmentConsent.getByRole('textbox', { name: '笔记选段原文' });
    for (const snippet of [noteFocus, unsupportedNote]) {
      await noteInput.focus();
      await noteInput.evaluate((node, value) => {
        const field = node as HTMLTextAreaElement;
        const start = field.value.indexOf(value);
        field.setSelectionRange(start, start + value.length);
      }, snippet);
      await assessmentConsent.getByRole('button', { name: '加入所选片段' }).click();
    }
    await assessmentConsent.getByRole('button', { name: '预览发送范围' }).click();
    const noteDisclosure = assessmentConsent.getByRole('region', { name: '拟发送的笔记选段' });
    await expect(noteDisclosure.getByRole('blockquote')).toHaveText([noteFocus, unsupportedNote]);
    await expect(noteDisclosure.getByText(privateNote)).toHaveCount(0);
    await expect(assessmentConsent.getByText('合成教学示例', { exact: true })).toBeVisible();
    await expect(assessmentConsent.getByText('这段争议只供人工判断。')).toHaveCount(0);
    expect(prompts).toHaveLength(3);
    expect(noteChecks).toHaveLength(0);
    await assessmentConsent.getByRole('button', { name: '确认并启用复习' }).click();
    await page.setViewportSize(originalViewport);
    await expect(page.getByText(/复习已启用.*已计入 2 次调用/)).toBeVisible({ timeout: 12000 });
    await page.getByText('笔记核验记录', { exact: true }).click();
    await expect(page.getByText('核验通过，可供出题参考')).toBeVisible();
    await expect(page.getByText('资料缺少这项依据。')).toBeVisible();
    expect(noteChecks.map(item => item.model)).toEqual(['review-fixture']);
    expect(noteChecks[0].prompt).not.toContain(privateNote);
    expect(JSON.parse(prompts[3].split('\n复习数据 JSON\n')[1]).verifiedNoteFocus).toEqual([noteFocus]);
    expect(prompts[3]).not.toContain(unsupportedNote);
    await page.getByRole('link', { name: '今日复习', exact: true }).click();
    await expect(page.getByText('说明合成检查', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '推进试用日期一天' }).click();
    const startAssessment = page.getByRole('link', { name: '开始复习：说明合成检查' });
    await startAssessment.click();
    const first = page.getByRole('textbox', { name: /这个例子用了什么检查依据/ });
    await page.getByRole('button', { name: '第 1 题语音作答' }).click();
    const voice = page.getByRole('dialog', { name: '录音转文字' });
    await expect(voice.getByRole('button', { name: '开始录音' })).toBeEnabled();
    await voice.getByRole('button', { name: '取消' }).click();
    await expect(first).toHaveValue('');
    await first.fill('我使用选定的合成资料原句作为依据。');
    await page.getByRole('button', { name: '稍后再答' }).click();
    await startAssessment.click();
    await expect(first).toHaveValue('我使用选定的合成资料原句作为依据。');
    await page.getByRole('textbox', { name: /还可以怎样复述检查/ }).fill('再检查一个补充项。');
    await page.getByRole('button', { name: '提交本组答案' }).click();
    await expect(page.getByText('答案已保存，等待评分')).toBeVisible();
    const assessmentUrl = page.url();
    const revisedNote = '答案已提交后的笔记修改。';
    await expect.poll(() => prompts.some(prompt => prompt.includes('评价整组答案'))).toBe(true);
    const noteUpdated = await page.evaluate(async ({ noteFixture, revisedNote }) => {
      const headers = { Authorization: 'Bearer ' + sessionStorage.getItem('zhixue-session'), 'Content-Type': 'application/json' };
      const path = '/api/notes/' + noteFixture;
      const current = await (await fetch(path, { headers })).json();
      const save = await fetch(path, { method: 'PUT', headers, body: JSON.stringify({
        baseRevision: current.revision, content: revisedNote,
      }) });
      return save.ok;
    }, { noteFixture, revisedNote });
    expect(noteUpdated).toBe(true);
    await page.getByRole('navigation', { name: '主要导航' }).getByRole('link', { name: '今日复习', exact: true }).click();
    const objectiveRow = page.locator('.review-row').filter({ hasText: '说明合成检查' });
    await expect(objectiveRow.getByText('等待评价', { exact: true })).toBeVisible();
    const advance = page.getByRole('button', { name: '推进试用日期一天' });
    for (let day = 0; day < 3; day++) {
      const advanced = page.waitForResponse(response => response.url().endsWith('/api/demo/next-day') && response.ok());
      await advance.click();
      await advanced;
    }
    releaseFirstGrade();
    await expect(page.getByRole('link', { name: '开始复习：说明合成检查' })).toBeVisible({ timeout: 12000 });
    await expect(objectiveRow).toHaveCount(1);
    await expect(objectiveRow.getByText('评分延迟', { exact: true })).toBeVisible();
    await page.goto(assessmentUrl);
    await expect(page.getByRole('heading', { name: '独立通过' })).toBeVisible({ timeout: 12000 });
    await expect(page.getByText(/^评分延迟 · 作答于.+评价于/)).toBeVisible();
    await page.getByText('出题时的笔记参考范围', { exact: true }).click();
    await expect(page.getByRole('link', { name: '独立复习札记 · 第 3 行' })).toBeVisible();
    await expect(page.getByText(noteFocus, { exact: true })).toBeVisible();
    expect(prompts[4]).not.toContain(noteFocus);
    expect(prompts[4]).not.toContain(revisedNote);
    const supplementLink = page.getByRole('link', { name: '查看课程中的错因补学' });
    await expect(supplementLink).toBeVisible({ timeout: 12000 });
    await supplementLink.click();
    const supplement = page.getByRole('dialog', { name: '错因补学' });
    await expect(supplement.getByText('对照合成资料原句，再用自己的话复述检查步骤。', { exact: true })).toBeVisible();
    const desktop = page.viewportSize()!;
    await page.setViewportSize({ width: 390, height: 780 });
    await expect(supplement).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
    await page.setViewportSize(desktop);
    await supplement.getByRole('button', { name: '关闭' }).click();
    const marker = page.getByRole('button', { name: /错因补学 · 合成教学示例/ });
    await marker.hover();
    await expect(page.getByRole('tooltip').getByText('少说明了检查依据。')).toBeVisible();
    await marker.click();
    await supplement.getByRole('link', { name: '在知识库打开讲解' }).click();
    await expect(page.getByRole('heading', { name: '知识库' })).toBeVisible();
    await expect(page.getByText('对照合成资料原句，再用自己的话复述检查步骤。', { exact: true })).toBeVisible();
    await page.goBack();
    await marker.click();
    await supplement.getByRole('link', { name: '查看当轮评价' }).click();
    expect(prompts).toHaveLength(7);
    expect(prompts[5]).toContain('再检查一个补充项。');
    expect(prompts[5]).not.toContain('我使用选定的合成资料原句作为依据。');
    await page.getByRole('button', { name: '重新核查' }).click();
    await expect(page.getByText('正在重新核查，已暂撤原判定的排期影响')).toBeVisible();
    await expect(page.getByRole('heading', { name: '需要再练习' })).toBeVisible({ timeout: 12000 });
    await page.getByText('历次评价', { exact: true }).click();
    await expect(page.getByText(/独立通过 · 本地路线测试/)).toBeVisible();
    await expect(supplementLink).toBeVisible({ timeout: 12000 });
    expect(prompts).toHaveLength(10);
    expect(prompts[3]).toContain(evidence);
    expect(prompts[3]).not.toContain('这段争议只供人工判断。');
    expect(prompts[7]).toContain('我使用选定的合成资料原句作为依据。');

    const formalReviewUrl = page.url();
    async function currentReviewAndReport() {
      return page.evaluate(async () => {
        const headers = { Authorization: 'Bearer ' + sessionStorage.getItem('zhixue-session') };
        const paths = ['/api/reviews', '/api/study-report?period=month'];
        const [reviews, report] = await Promise.all(paths.map(async path => {
          const response = await fetch(path, { headers });
          if (!response.ok) throw new Error('Cannot read formal progress');
          return response.json();
        }));
        return { reviews: reviews.reviews, totals: report.totals.review, weak: report.weak };
      });
    }
    const formalBaseline = await currentReviewAndReport();
    await page.getByRole('link', { name: '学习报告', exact: true }).click();
    await page.getByRole('button', { name: '月', exact: true }).click();
    const monthUrl = page.url();
    await page.getByRole('button', { name: '查看需要再练习的正式复习记录' }).click();
    const records = page.getByRole('region', { name: '正式复习记录', exact: true });
    const modelRecord = records.getByRole('listitem').filter({ hasText: '说明合成检查' });
    await expect(modelRecord).toContainText('第 1 轮');
    const original = modelRecord.getByRole('link', { name: '查看当轮评价' });
    const reportWithFilter = page.url();
    const linkedGroup = new URLSearchParams((await original.getAttribute('href'))?.split('?')[1]).get('group');
    expect(linkedGroup).toBe(new URLSearchParams(new URL(formalReviewUrl).hash.split('?')[1]).get('group'));
    await original.click();
    await expect(page.getByRole('heading', { name: '需要再练习', exact: true })).toBeVisible();
    expect(prompts).toHaveLength(10);
    await expect(page.getByRole('link', { name: '返回学习报告', exact: true })).toBeVisible({ timeout: 4000 });
    await page.getByRole('link', { name: '返回学习报告', exact: true }).click();
    await expect(page).toHaveURL(reportWithFilter);
    await expect(modelRecord).toBeVisible();
    await page.goto(monthUrl);
    await page.getByRole('link', { name: '专项练习 说明合成检查', exact: true }).click();
    await page.getByText('来源评价与讲解', { exact: true }).click();
    await expect(page.getByText('已完成合成的整组评价。', { exact: true })).toBeVisible();
    await expect(supplementLink).toBeVisible();
    expect(practicePrompts).toHaveLength(0);
    await page.getByRole('button', { name: '生成专项练习', exact: true }).click();
    await page.getByRole('textbox', { name: new RegExp(practiceQuestion.replace('？', '')) }).fill(practiceAnswer);
    await page.getByRole('button', { name: '提交本组答案', exact: true }).click();
    await expect(page.getByText('已完成独立的合成专项评价。', { exact: true })).toBeVisible({ timeout: 12000 });
    expect(practicePrompts).toHaveLength(2);
    expect(practicePrompts[0]).toContain('这个例子用了什么检查依据？');
    expect(practicePrompts[0]).not.toContain('我使用选定的合成资料原句作为依据。');
    await page.reload();
    await expect(page.getByText('已完成独立的合成专项评价。', { exact: true })).toBeVisible({ timeout: 12000 });
    await page.getByRole('link', { name: '返回学习报告', exact: true }).click();
    await expect(page).toHaveURL(monthUrl);
    expect(await currentReviewAndReport()).toEqual(formalBaseline);
    await page.goto(formalReviewUrl);

    await supplementLink.click();
    await supplement.getByRole('button', { name: '关闭' }).click();
    const courseRoute = new URL(page.url()).hash.split('?')[0];
    const transcript = page.getByRole('complementary', { name: '课内助手' });
    const excerpt = '草稿正文';
    const question = '怎么用原始资料理解这句？';
    await page.getByRole('button', { name: '课内助手', exact: true }).click();
    const questionInput = transcript.getByLabel('对话消息');
    const sendQuestion = transcript.getByRole('button', { name: '发送消息' });
    await questionInput.fill(question);
    await expect(sendQuestion).toBeDisabled();
    await questionInput.fill('这节我已经会了？');
    await expect(sendQuestion).toBeDisabled();
    await questionInput.fill('这节我已经会了。');
    await expect(sendQuestion).toBeEnabled();
    await sendQuestion.click();
    const routeAction = page.getByRole('dialog', { name: '标记当前知识点' });
    await routeAction.getByRole('button', { name: '预览路线变化', exact: true }).click();
    await expect(routeAction.getByText(/未标注.*已会/)).toBeVisible();
    await routeAction.getByRole('button', { name: '关闭', exact: true }).click();
    expect(prompts).toHaveLength(10);
    await transcript.getByRole('button', { name: '关闭课内助手' }).click();
    await page.locator('p[data-course-section="0"]').evaluate((paragraph, excerpt) => {
      const text = paragraph.firstChild!;
      const range = document.createRange();
      const start = text.textContent!.indexOf(excerpt);
      range.setStart(text, start);
      range.setEnd(text, start + excerpt.length);
      window.getSelection()!.removeAllRanges();
      window.getSelection()!.addRange(range);
    }, excerpt);
    await page.getByRole('button', { name: '问助手', exact: true }).click();
    await expect(transcript.getByText('选段 · 第 2 版')).toBeVisible();
    await expect(transcript.getByText(excerpt, { exact: true })).toBeVisible();
    await transcript.getByLabel('对话消息').fill(question);
    await expect(transcript.getByRole('button', { name: '发送消息' })).toBeDisabled();
    await page.getByRole('button', { name: '笔记', exact: true }).click();
    await expect(transcript).toBeHidden();
    await page.getByRole('button', { name: '课内助手', exact: true }).click();
    await expect(transcript.getByLabel('对话消息')).toHaveValue(question);
    await transcript.getByRole('button', { name: '启用本节课内答疑' }).click();
    const assistantScope = page.getByRole('dialog', { name: '确认课内答疑发送范围' });
    await assistantScope.getByLabel('本节答疑调用次数上限').fill('3');
    await assistantScope.getByRole('button', { name: '预览发送范围' }).click();
    await expect(assistantScope.getByText(evidence, { exact: true })).toBeVisible();
    await expect(assistantScope.getByText('这段争议只供人工判断。')).toHaveCount(0);
    expect(prompts).toHaveLength(10);
    await assistantScope.getByRole('button', { name: '确认并启用课内答疑' }).click();
    await transcript.getByRole('button', { name: '发送消息' }).click();
    await expect(transcript.getByText(interpretation, { exact: true })).toBeVisible({ timeout: 12000 });
    await expect(transcript.getByText('引文已定位')).toBeVisible();
    const answerRequest = JSON.parse(prompts[10].split('\n答疑上下文 JSON\n')[1]);
    expect(answerRequest.selection).toEqual({ version: 2, sectionIndex: 0, start: 3, end: 7, text: excerpt, state: 'verified' });
    expect(prompts[10]).toContain(evidence);
    expect(prompts[10]).not.toContain('这段争议只供人工判断。');
    expect(prompts).toHaveLength(11);
    await transcript.getByRole('button', { name: '收藏疑问', exact: true }).click();
    await expect(transcript.getByText('已收藏疑问 · 1', { exact: true })).toBeVisible();

    await stop(service.child);
    service = start(folder);
    await page.goto(await service.url);
    await page.evaluate(hash => { window.location.hash = hash; }, courseRoute);
    await page.getByRole('button', { name: '课内助手', exact: true }).click();
    await transcript.getByText('已收藏疑问 · 1', { exact: true }).click();
    await transcript.getByRole('button', { name: question, exact: true }).click();
    await expect(transcript.getByText(interpretation, { exact: true })).toBeVisible();
    await transcript.getByRole('link', { name: '回到原文' }).click();
    await expect(page.locator('p[data-course-section="0"] mark')).toHaveText(excerpt);
    const previousAnchor = new URL(page.url()).hash.replace('readingVersion=2', 'readingVersion=1');
    await page.evaluate(hash => { window.location.hash = hash; }, previousAnchor);
    await expect(versionHistory.locator('mark')).toHaveText(excerpt);
    await versionHistory.getByRole('button', { name: '关闭' }).click();
    await page.setViewportSize({ width: 390, height: 780 });
    await expect(transcript).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
    expect(prompts).toHaveLength(11);

    await page.setViewportSize(desktop);
    await transcript.getByRole('button', { name: '关闭课内助手' }).click();
    const learned = page.getByRole('checkbox', { name: '已学完', exact: true });
    await learned.click();
    await expect(learned).not.toBeChecked();
    await learned.click();
    await expect(learned).toBeChecked();
    const consolidation = page.getByRole('region', { name: '学完后的答疑整理' });
    await expect(consolidation.getByRole('alert')).toContainText('请确认学完后整理答疑');
    await consolidation.getByRole('button', { name: '启用学完后答疑整理' }).click();
    const consolidationScope = page.getByRole('dialog', { name: '确认学完后答疑整理' });
    await consolidationScope.getByLabel('本节整理调用次数上限').fill('4');
    await consolidationScope.getByRole('button', { name: '预览整理发送范围' }).click();
    await expect(consolidationScope.getByText('独立核验 · 本机独立复核 · review-fixture')).toBeVisible();
    await expect(consolidationScope.getByText(question, { exact: true })).toBeVisible();
    expect(prompts).toHaveLength(11);
    await consolidationScope.getByRole('button', { name: '确认并启用答疑整理' }).click();
    await expect(consolidation.getByRole('alert')).toContainText('新增讲解尚未通过独立核验', { timeout: 12000 });
    await expect(page.getByRole('button', { name: '课程版本 · 第 2 版' })).toBeVisible();
    await expect(learned).toBeChecked();
    await consolidation.getByText('查看待处理草稿', { exact: true }).click();
    await expect(consolidation.getByText(additionText, { exact: true })).toBeVisible();
    await consolidation.getByRole('button', { name: '重试此阶段' }).click();
    await expect(consolidation.getByText('已融入课程', { exact: true })).toBeVisible({ timeout: 12000 });
    await expect(page.getByRole('button', { name: '课程版本 · 第 3 版' })).toBeVisible();
    await consolidation.getByRole('link', { name: '定位新增段落' }).click();
    await expect(page.getByRole('heading', { name: additionTitle, exact: true })).toBeInViewport();
    const additionSection = page.locator('.course-section').filter({ has: page.getByRole('heading', { name: additionTitle }) });
    await additionSection.getByText('来源与独立复核记录', { exact: true }).click();
    await expect(additionSection.getByText(/整理自 1 轮课内问答/)).toBeVisible();
    await expect(additionSection.getByRole('link', { name: '曲牌证据.txt · 第 2 行' })).toBeVisible();
    await expect(additionSection.getByRole('link', { name: '打开原课内对话' })).toBeVisible();
    expect(prompts).toHaveLength(14);
    expect(consolidationModels).toEqual(['route-fixture', 'review-fixture', 'review-fixture']);
    expect(prompts[11]).toContain(question);
    expect(prompts.slice(12).every(prompt => !prompt.includes(question) && !prompt.includes(privateLine))).toBe(true);

    const lessonId = courseRoute.split('/').at(-1)!;
    const live = await page.evaluate(async lessonId => {
      const response = await fetch('/api/lessons/' + lessonId, {
        headers: { Authorization: 'Bearer ' + sessionStorage.getItem('zhixue-session') },
      });
      if (!response.ok) throw new Error('Current course could not be read');
      return response.json();
    }, lessonId);
    const currentFile = lessonId + '-v' + live.courseVersion + '-' + live.readingRevision.slice(0, 12) + '.md';
    unlinkSync(join(folder, 'library', 'courses', currentFile));
    await page.evaluate(id => { window.location.hash = '#/library?file=note-' + id; }, lessonId);
    await page.locator('.library-preview').getByRole('link', { name: '查看原文 · 第 1 版' }).click();
    await expect(versionHistory.locator('mark')).toHaveText(bodyCopy);
    await expect(page.getByText('本机课程文件当前不可用，可查看已保存的版本。')).toBeVisible();
  } finally {
    releaseFirstGrade();
    await stop(service.child);
    const closed = once(provider, 'close');
    provider.close();
    await closed;
    rmSync(folder, { recursive: true, force: true });
  }
});

test('target card, coursework, real Markdown, and independent review survive restart', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-e2e-'));
  let service = start(folder);
  const pageErrors: string[] = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  try {
    await page.goto(await service.url);
    await expect(page.getByRole('heading', { name: '今天，想弄懂什么？' })).toBeVisible();
    await page.getByRole('button', { name: '体验 SQLite 样例' }).click();
    await expect(page.getByRole('heading', { name: '学习目标' })).toBeVisible();
    await page.getByRole('textbox', { name: '预期成果', exact: true }).fill('能从学习记录中筛选 SQLite 数据');
    await page.getByRole('button', { name: '提交并生成路线' }).click();
    await expect(page.getByText('3 / 3 节已就绪')).toBeVisible();

    await page.getByRole('button', { name: /排序并挑出最长的学习记录/ }).click();
    await expect(page.getByRole('heading', { name: '排序并挑出最长的学习记录' })).toBeVisible();
    await page.getByRole('button', { name: /理解表、行和列/ }).click();
    await page.getByRole('link', { name: '开始学习' }).click();
    await expect(page.getByRole('heading', { name: '一张表能记下什么' })).toBeVisible();
    await expect(page.getByRole('link', { name: /SQLite 官方文档/ })).toBeVisible();
    await page.getByRole('button', { name: '开始计时' }).click();
    await expect(page.getByText('计时中', { exact: false })).toBeVisible();
    await page.getByRole('link', { name: '知识库', exact: true }).click();
    await page.getByRole('link', { name: '返回计时课程' }).click();
    await page.getByRole('button', { name: '暂停计时' }).click();
    await expect(page.getByText('计时已暂停', { exact: false })).toBeVisible();
    await page.getByRole('button', { name: '结束计时' }).click();
    await expect(page.getByRole('link', { name: '返回计时课程' })).toHaveCount(0);
    await page.getByRole('button', { name: '笔记', exact: true }).click();
    await page.getByRole('button', { name: 'Markdown 原文' }).click();
    await page.getByLabel('本节笔记').fill('每行是一条记录。');
    await page.getByRole('button', { name: '保存笔记' }).click();
    await expect(page.getByText('笔记已保存')).toBeVisible();
    const notes = join(folder, 'library', 'notes');
    expect(readFileSync(join(notes, readdirSync(notes)[0]), 'utf8')).toBe('每行是一条记录。');
    const learned = page.getByRole('checkbox', { name: '已学完', exact: true });
    await learned.click();
    await expect(learned).toBeChecked();
    await learned.click();
    await expect(learned).not.toBeChecked();
    await learned.click();
    await expect(learned).toBeChecked();
    await expect(page.getByText(/首次复习安排在次日/)).toBeVisible();

    await page.getByRole('link', { name: '今日复习' }).click();
    await expect(page.getByText('今天没有到期复习')).toBeVisible();
    await page.getByRole('button', { name: '推进试用日期一天' }).click();
    await page.getByRole('link', { name: /区分行与列/ }).click();
    await page.getByRole('radio', { name: '一行', exact: true }).first().check();
    await page.getByRole('radio', { name: '一列', exact: true }).last().check();
    await page.getByRole('button', { name: '提交本组答案' }).click();
    await expect(page.getByRole('heading', { name: '独立通过' })).toBeVisible();

    await stop(service.child);
    service = start(folder);
    await page.goto(await service.url);
    await page.getByRole('link', { name: '今日复习' }).click();
    await expect(page.getByRole('link', { name: /选择可唯一定位的主键/ })).toBeVisible();
    await expect(page.getByText('区分行与列', { exact: true })).toBeVisible();
    await page.getByRole('link', { name: '知识库', exact: true }).click();
    await page.getByRole('button', { name: /理解表、行和列 · 笔记/ }).click();
    await expect(page.getByText('每行是一条记录。', { exact: true })).toBeVisible();
    expect(pageErrors).toEqual([]);
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

test('project board applies status and text filters before paging', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-board-'));
  const service = start(folder);
  try {
    const url = new URL(await service.url);
    const token = new URLSearchParams(url.hash.split('?')[1]).get('session');
    const headers = { Authorization: `Bearer ${token}` };
    const request = page.request;
    for (let index = 1; index <= 25; index += 1) {
      const id = `10000000-0000-4000-8000-${String(index).padStart(12, '0')}`;
      const project = await request.post(new URL('/api/projects', url).href, {
        headers, data: { id, topic: `验证项目 ${index}`, outcome: '练习项目看板', foundation: '' },
      });
      expect(project.ok()).toBe(true);
      if (index >= 13 && index <= 17) {
        expect((await request.put(new URL(`/api/projects/${id}/pause`, url).href, { headers, data: { paused: true } })).ok()).toBe(true);
      }
      if (index >= 18) {
        const { lessons } = await project.json();
        await expect.poll(async () => {
          const result = await request.get(new URL(`/api/projects/${id}`, url).href, { headers });
          return (await result.json()).task.status;
        }).toBe('ready');
        for (const lesson of lessons) {
          expect((await request.put(new URL(`/api/lessons/${lesson.id}/learned`, url).href, { headers, data: { learned: true } })).ok()).toBe(true);
        }
      }
    }
    url.hash = url.hash.replace('#/?', '#/projects?');
    await page.goto(url.href);
    const board = page.getByRole('main');
    await expect(board.getByRole('heading', { level: 3 })).toHaveCount(9);
    await expect(board.getByRole('heading', { name: '进行中 12' })).toBeVisible();
    await expect(board.getByRole('heading', { name: '已暂停 5' })).toBeVisible();
    await expect(board.getByRole('heading', { name: '已学完 8' })).toBeVisible();
    await board.getByRole('button', { name: '进行中下一页' }).click();
    await expect(board.getByRole('heading', { name: '验证项目 4', exact: true })).toBeVisible();
    await expect(board.getByRole('heading', { name: '验证项目 13', exact: true })).toBeVisible();
    await board.getByRole('link', { name: '验证项目 4', exact: true }).click();
    await page.getByRole('main').getByRole('link', { name: '全部项目' }).click();
    await expect(board.getByRole('heading', { name: '验证项目 4', exact: true })).toBeVisible();
    await expect(board.getByText('第 2 / 4 页')).toBeVisible();
    const search = board.getByLabel('搜索项目名称或目标');
    let releaseQuery!: () => void;
    let finishQuery!: () => void;
    const held = new Promise<void>(resolve => { releaseQuery = resolve; });
    const finished = new Promise<void>(resolve => { finishQuery = resolve; });
    const queryPath = '**/api/project-board/query';
    await page.route(queryPath, async route => {
      if (route.request().postDataJSON().view.search !== '验证项目 24') return route.continue();
      try {
        const response = await route.fetch();
        await held;
        await route.fulfill({ response });
      } finally {
        finishQuery();
      }
    });
    try {
      const older = page.waitForRequest(request => request.url().endsWith('/api/project-board/query') &&
        request.postDataJSON()?.view.search === '验证项目 24');
      await search.fill('验证项目 24');
      await older;
      await expect(board.getByRole('status')).toContainText('正在更新项目');
      await expect(board.getByRole('heading', { name: '验证项目 4', exact: true })).toBeVisible();
      await search.fill('验证项目 25');
      await expect(board.getByRole('heading', { name: '验证项目 25', exact: true })).toBeVisible();
      releaseQuery();
      await finished;
      await expect(board.getByRole('heading', { name: '验证项目 24', exact: true })).toHaveCount(0);
    } finally {
      releaseQuery();
      await page.unroute(queryPath);
    }
    await expect(board.getByRole('heading', { level: 3 })).toHaveCount(1);
    await board.getByLabel('搜索项目名称或目标').clear();
    await board.getByRole('button', { name: '列表' }).click();
    await expect(board.getByRole('heading', { level: 3 })).toHaveCount(12);
    await board.getByRole('button', { name: '卡片', exact: true }).click();
    await expect(board.getByRole('heading', { level: 3 })).toHaveCount(12);
    await board.getByLabel('每页项目数').selectOption('24');
    await expect(board.getByRole('heading', { level: 3 })).toHaveCount(24);
    await board.getByRole('button', { name: '看板', exact: true }).click();
    await page.setViewportSize({ width: 430, height: 780 });
    await expect(board.getByRole('heading', { level: 3 })).toHaveCount(9);
    const columns = board.getByRole('region', { name: '项目状态看板' });
    expect(await columns.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(430);
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

async function seedNoteLesson(page: Page, address: string) {
  const url = new URL(address);
  const session = new URLSearchParams(url.hash.split('?')[1]).get('session');
  const headers = { Authorization: 'Bearer ' + session };
  const project = await page.request.post(new URL('/api/projects', url).href, { headers, data: {
    id: '10000000-0000-4000-8000-000000000001', topic: 'SQLite 实践', outcome: '比较笔记版本',
  } });
  expect(project.ok()).toBeTruthy();
  const { id, lessons } = await project.json();
  await expect.poll(async () => {
    const response = await page.request.get(new URL('/api/projects/' + id, url).href, { headers });
    return (await response.json()).task.status;
  }).toBe('ready');
  return lessons[0].id as string;
}

test('independent and course notes share one file across linked lessons and preserve drafts when switching', async ({ page }) => {
  test.setTimeout(85_000);
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-note-catalog-'));
  const service = start(folder);
  try {
    const address = await service.url;
    const lesson = await seedNoteLesson(page, address);
    const url = new URL(address);
    const session = new URLSearchParams(url.hash.split('?')[1]).get('session');
    const choices = await page.request.get(new URL('/api/notes/lesson-options', url).href, { headers: { Authorization: 'Bearer ' + session } });
    expect(choices.ok()).toBeTruthy();
    const catalog: { id: string; title: string; projectTopic: string }[] = await choices.json();
    const first = catalog.find(entry => entry.id === lesson)!;
    const second = catalog.find(entry => entry.id !== lesson)!;
    const course = (id: string) => address.replace('#/?', '#/lessons/' + id + '?');
    const notesFolder = join(folder, 'library', 'notes');
    const pane = page.locator('.reading-side .note-panel');
    const editor = pane.getByRole('textbox', { name: '本节笔记' });
    const picker = pane.getByRole('combobox', { name: '选择笔记' });
    const originalTitle = first.title + ' · 笔记';
    const anotherTitle = '本节第二篇笔记';
    const standalone = '独立札记';
    const original = '这段正文属于原有笔记。';
    const another = '第二篇笔记中的补充思路。';
    const shared = '这段正文供两节课程共享。';

    await page.goto(course(lesson));
    await page.getByRole('button', { name: '笔记', exact: true }).click();
    await expect(picker.getByRole('option', { name: originalTitle })).toHaveCount(1);
    await pane.getByRole('button', { name: 'Markdown 原文' }).click();
    await editor.fill(original);
    await pane.getByRole('button', { name: '保存笔记' }).click();
    await expect(pane.getByText('笔记已保存')).toBeVisible();
    await pane.getByRole('button', { name: '新建笔记' }).click();
    const creation = page.getByRole('dialog', { name: '新建笔记' });
    await creation.getByRole('textbox', { name: '笔记标题' }).fill(anotherTitle);
    await creation.getByRole('button', { name: '创建笔记' }).click();
    await expect(picker.getByRole('option', { name: anotherTitle })).toHaveCount(1);
    await expect(picker.locator('option:checked')).toHaveText(anotherTitle);
    const otherId = await picker.inputValue();
    await pane.getByRole('button', { name: 'Markdown 原文' }).click();
    await editor.fill(another);
    await picker.selectOption({ label: originalTitle });
    await expect(editor).toHaveValue(original);
    await expect.poll(() => readFileSync(join(notesFolder, otherId + '.md'), 'utf8'), { timeout: 9000 }).toBe(another);
    await picker.selectOption({ label: anotherTitle });
    await expect(editor).toHaveValue(another);

    await page.getByRole('link', { name: '知识库', exact: true }).click();
    await page.getByRole('button', { name: '新建笔记', exact: true }).click();
    await creation.getByRole('textbox', { name: '笔记标题' }).fill(standalone);
    await creation.getByRole('button', { name: '创建笔记' }).click();
    const preview = page.locator('.library-preview');
    await expect(preview.getByRole('heading', { name: standalone })).toBeVisible();
    const fileId = await page.evaluate(() => new URLSearchParams(location.hash.split('?')[1]).get('file')!);
    const noteId = fileId.slice('note-'.length);
    const singleFile = join(notesFolder, noteId + '.md');
    expect(readFileSync(singleFile, 'utf8')).toBe('');
    await preview.getByRole('button', { name: 'Markdown 原文' }).click();
    await preview.getByRole('textbox', { name: '笔记正文' }).fill(shared);
    await preview.getByRole('button', { name: '保存笔记' }).click();
    await expect(preview.getByText('笔记已保存')).toBeVisible();

    await page.setViewportSize({ width: 410, height: 840 });
    await preview.getByRole('button', { name: '标题与课程关联' }).click();
    const details = page.getByRole('dialog', { name: '笔记标题与课程关联' });
    await expect(details.getByText('已选择 0 个知识点')).toBeVisible();
    const filter = details.getByRole('searchbox', { name: '搜索知识点或项目' });
    for (const row of [first, second]) {
      await filter.fill(row.title);
      await details.getByRole('checkbox', { name: row.title + ' ' + row.projectTopic, exact: true }).check();
    }
    await expect(details.getByText('已选择 2 个知识点')).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(410);
    await details.getByRole('button', { name: '保存设置' }).click();
    await expect(details).toBeHidden();
    await preview.getByRole('button', { name: '返回预览' }).click();
    await expect(preview.locator(".markdown").getByText(shared)).toBeVisible();

    await page.setViewportSize({ width: 1440, height: 940 });
    await page.goto(course(second.id));
    await page.getByRole('button', { name: '笔记', exact: true }).click();
    await expect(picker.getByRole('option')).toHaveCount(1);
    await expect(picker.locator('option:checked')).toHaveText(standalone);
    await pane.getByRole('button', { name: 'Markdown 原文' }).click();
    await expect(editor).toHaveValue(shared);
    const edited = shared + '\n第二节课中补写的说明。';
    await editor.fill(edited);
    await pane.getByRole('button', { name: '保存笔记' }).click();
    await expect(pane.getByText('笔记已保存')).toBeVisible();

    await page.goto(course(lesson));
    await page.getByRole('button', { name: '笔记', exact: true }).click();
    await expect(picker.getByRole('option')).toHaveCount(3);
    await picker.selectOption({ label: standalone });
    await pane.getByRole('button', { name: 'Markdown 原文' }).click();
    await expect(editor).toHaveValue(edited);

    const passage = '每一行是一条记录';
    const body = page.locator('.reading-panel .markdown p').filter({ hasText: passage });
    await body.scrollIntoViewIfNeeded();
    await body.evaluate((element, quote) => {
      const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
      let text = walker.nextNode();
      while (text && !text.textContent?.includes(quote)) text = walker.nextNode();
      if (!text) throw new Error('Excerpt text was not found');
      const start = text.textContent!.indexOf(quote);
      const range = document.createRange();
      range.setStart(text, start);
      range.setEnd(text, start + quote.length);
      const selection = window.getSelection()!;
      selection.removeAllRanges();
      selection.addRange(range);
    }, passage);
    await page.getByRole('button', { name: '摘录到笔记', exact: true }).click();
    await expect(editor).toHaveValue(/第二节课中补写的说明。\n\n> 每一行是一条记录/);
    await pane.getByRole('button', { name: '保存笔记' }).click();
    await expect.poll(() => readFileSync(singleFile, 'utf8')).toContain('> ' + passage);
    expect(readFileSync(join(notesFolder, lesson + '.md'), 'utf8')).toBe(original);
    expect(readFileSync(join(notesFolder, otherId + '.md'), 'utf8')).toBe(another);
    expect(readdirSync(notesFolder).filter(name => name.endsWith('.md')).sort()).toEqual([lesson, otherId, noteId].map(id => id + '.md').sort());
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

test('lecture excerpts drag selected prose and code, preserve formulas, and reopen the exact local source', async ({ page }) => {
  test.setTimeout(60_000);
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-lecture-excerpt-'));
  const service = start(folder);
  try {
    const address = await service.url;
    const lesson = await seedNoteLesson(page, address);
    const currentUrl = address.replace('#/?', '#/lessons/' + lesson + '?');
    const lessonFile = join(folder, 'library', 'courses', lesson + '.md');
    const noteFile = join(folder, 'library', 'notes', lesson + '.md');
    const chosenWords = '原文带有𠮷字';
    const chosenCode = 'SELECT id FROM learning_log;';
    const chosenFormula = '\\sum_{i=1}^n i';
    const inlineFormula = 'a^2+b^2=c^2';
    const original = [
      '\uFEFF# 摘录测试', '', '## 讲稿一', '', '前一句，' + chosenWords + '，后一句。', '',
      '~~~sql 摘录示例', 'SELECT topic FROM learning_log;', '  ' + chosenCode, '~~~', '',
      '$$', chosenFormula, '$$', '', '行内公式 $' + inlineFormula + '$ 和行内代码 \x60row_id\x60。',
    ].join('\r\n') + '\r\n';
    const revision = createHash('sha256').update(original).digest('hex');
    writeFileSync(lessonFile, original);

    await page.goto(currentUrl);
    const pane = page.locator('.note-panel');
    const editor = pane.getByRole('textbox', { name: '本节笔记' });
    const reading = page.locator('.reading-panel .markdown');
    const popup = page.getByRole('button', { name: '摘录到笔记', exact: true });
    const formula = reading.locator('.lecture-formula').first();
    await expect(formula.locator('annotation')).toHaveText(chosenFormula);
    await expect(pane).toBeHidden();

    async function selectText(block: Locator, choice: string) {
      await block.scrollIntoViewIfNeeded();
      await block.evaluate((element, selected) => {
        const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
        let text = walker.nextNode();
        while (text && !text.textContent?.includes(selected)) text = walker.nextNode();
        if (!text) throw new Error('Source text was not rendered');
        const start = text.textContent!.indexOf(selected);
        const range = document.createRange();
        range.setStart(text, start);
        range.setEnd(text, start + selected.length);
        const selection = window.getSelection()!;
        selection.removeAllRanges();
        selection.addRange(range);
      }, choice);
    }

    async function dragSelection() {
      const box = (await popup.boundingBox())!;
      const origin = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
      await page.mouse.move(origin.x, origin.y);
      await page.mouse.down();
      try {
        await page.mouse.move(origin.x + 18, origin.y, { steps: 4 });
        await expect(pane).toBeVisible();
        const target = (await pane.getByRole('heading', { name: '本节笔记' }).boundingBox())!;
        await page.mouse.move(target.x + target.width / 2, target.y + target.height / 2, { steps: 9 });
      } finally {
        await page.mouse.up();
      }
    }

    await selectText(reading.locator('p').filter({ hasText: chosenWords }), chosenWords);
    await expect(popup).toBeVisible();
    await dragSelection();
    await expect(editor.locator('blockquote')).toHaveText(chosenWords, { timeout: 11_000 });
    await expect(editor.getByRole('link', { name: '查看原文 · 第 5 行' })).toHaveAttribute('href',
      '#/library?file=course-' + lesson + '&at=5&revision=' + revision);

    await selectText(reading.locator('pre code'), '  ' + chosenCode);
    await expect(popup).toBeVisible();
    await dragSelection();
    await expect(editor.locator('pre code')).toHaveText(chosenCode);
    expect(await editor.locator('pre code').textContent()).toBe('  ' + chosenCode);
    await expect(editor.locator('pre code')).toHaveAttribute('class', /(?:^| )language-sql/);
    await expect(editor.getByRole('link', { name: '查看原文 · 第 9 行' })).toBeVisible();

    await page.evaluate(() => window.getSelection()?.removeAllRanges());
    await formula.locator('.katex-html').dragTo(pane.getByRole('heading', { name: '本节笔记' }));
    await expect(editor.locator('[data-type="block-math"] annotation')).toHaveText(chosenFormula);
    await expect(editor.getByRole('link', { name: '查看原文 · 第 12 行' })).toBeVisible();
    await expect(pane.getByText('笔记已保存')).toBeVisible({ timeout: 9000 });

    await pane.getByRole('button', { name: '关闭笔记' }).click();
    await page.setViewportSize({ width: 410, height: 840 });
    const inlineParagraph = reading.locator('p').filter({ hasText: '行内公式' });
    await inlineParagraph.scrollIntoViewIfNeeded();
    await inlineParagraph.evaluate(element => {
      const range = document.createRange();
      range.selectNodeContents(element);
      const selection = window.getSelection()!;
      selection.removeAllRanges();
      selection.addRange(range);
    });
    await popup.click();
    await expect(editor.locator('[data-type="inline-math"] annotation')).toHaveText(inlineFormula);
    await expect(editor.locator('p code')).toHaveText('row_id');
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(410);
    await expect(pane.getByText('笔记已保存')).toBeVisible({ timeout: 9000 });

    const saved = readFileSync(noteFile, 'utf8');
    expect(saved).toContain('> ' + chosenWords);
    expect(saved).not.toContain('前一句');
    expect(saved).toContain('  ' + chosenCode);
    expect(saved).not.toContain('SELECT topic');
    expect(saved).toContain('$$\n' + chosenFormula + '\n$$');
    expect(saved).toContain('$' + inlineFormula + '$');
    expect(saved.match(/revision=/g)).toHaveLength(4);

    await editor.getByRole('link', { name: '查看原文 · 第 9 行' }).click();
    const file = page.locator('.library-preview');
    await expect(file.locator('.file-anchor strong')).toHaveText('第 9 行');
    await expect(file.locator('.file-anchor pre')).toHaveText(chosenCode);
    await page.goBack();
    await page.getByRole('button', { name: '笔记', exact: true }).click();
    await pane.getByRole('link', { name: '在知识库打开笔记' }).click();
    const savedLink = file.getByRole('link', { name: '查看原文 · 第 5 行' });
    await expect(savedLink).toHaveAttribute('href', '#/library?file=course-' + lesson + '&at=5&revision=' + revision);
    writeFileSync(lessonFile, original.replace(chosenWords, '改过的原文'));
    await savedLink.click();
    await expect(file.getByRole('button', { name: '查看当前版本' })).toBeVisible();
    await expect(file.locator('.file-anchor')).toHaveCount(0);
    expect(readFileSync(noteFile, 'utf8')).toBe(saved);

    await page.setViewportSize({ width: 1440, height: 940 });
    await page.goto(currentUrl);
    await page.getByRole('button', { name: '笔记', exact: true }).click();
    await pane.getByRole('button', { name: 'Markdown 原文' }).click();
    const openFence = saved + '\n\x60\x60\x60text\n仍在编辑代码';
    await editor.fill(openFence);
    await selectText(reading.locator('p').filter({ hasText: '改过的原文' }), '改过的原文');
    await popup.click();
    await expect(pane.getByRole('alert')).toContainText('原文中的 Markdown 块尚未结束');
    await expect(editor).toHaveValue(openFence);
    await editor.fill(openFence + '\n\x60\x60\x60');
    await expect(editor).toHaveValue(/仍在编辑代码\n\x60\x60\x60\n\n> 改过的原文\n\n\[查看原文/);
    await expect(pane.getByText('笔记已保存')).toBeVisible({ timeout: 9000 });
    expect(readFileSync(noteFile, 'utf8').match(/revision=/g)).toHaveLength(5);
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

test('rich notes round-trip Markdown and autosave editable formulas, code, quotes, and links', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-rich-note-'));
  const service = start(folder);
  try {
    const address = await service.url;
    const lesson = await seedNoteLesson(page, address);
    const currentUrl = address.replace('#/?', '#/lessons/' + lesson + '?');
    const file = join(folder, 'library', 'notes', lesson + '.md');
    const code = ['~~~~markdown', '\x60\x60\x60sql', 'SELECT \'$5\';', '', '\x60\x60\x60', '~~~~'].join('\n');
    const italic = '这是 **加粗**、_斜体_ 和 [[数据模型]]。';
    const math = '$$\n\\sum_{i=1}^{n} i\n$$';
    const link = '[SQLite 资料](https://sqlite.org/lang_select.html "查询说明")';
    const original = ['# 学习整理', italic, '> 原讲义中的一句话。', code, '先看 $x^2+1$。', math, link, ''].join('\n\n');

    await page.goto(currentUrl);
    await page.getByRole('button', { name: '笔记', exact: true }).click();
    const richMode = page.getByRole('button', { name: '富文本', exact: true });
    const sourceMode = page.getByRole('button', { name: 'Markdown 原文' });
    const note = page.getByRole('textbox', { name: '本节笔记' });
    const save = page.getByRole('button', { name: '保存笔记' });
    await expect(richMode).toHaveAttribute('aria-pressed', 'true');
    await sourceMode.click();
    await note.fill(original);
    await save.click();
    await expect(page.getByText('笔记已保存')).toBeVisible();
    expect(readFileSync(file, 'utf8')).toBe(original);

    await richMode.click();
    await expect(note.getByRole('heading', { name: '学习整理' })).toBeVisible();
    await expect(note.locator('strong')).toHaveText('加粗');
    await expect(note.locator('em')).toHaveText('斜体');
    await expect(note.locator('blockquote')).toHaveText('原讲义中的一句话。');
    await expect(note.locator('pre code')).toHaveAttribute('class', 'language-markdown');
    expect(await note.locator('pre code').textContent()).toContain('\x60\x60\x60sql\nSELECT \'$5\';\n\n\x60\x60\x60');
    await expect(note.locator('[data-wiki-reference]')).toHaveText('[[数据模型]]');
    await expect(note.getByRole('link', { name: 'SQLite 资料' })).toHaveAttribute('title', '查询说明');
    await expect(note.locator('[data-type="block-math"] annotation')).toHaveText('\\sum_{i=1}^{n} i');
    await expect(note.locator('[data-type="inline-math"] annotation')).toHaveText('x^2+1');
    await expect(save).toBeDisabled();
    expect(readFileSync(file, 'utf8')).toBe(original);

    await note.locator('h1').click();
    await page.keyboard.press('End');
    await page.keyboard.press('Enter');
    await page.getByRole('button', { name: '加粗', exact: true }).click();
    await page.keyboard.insertText('富文本追加');
    await expect(note.getByText('富文本追加')).toHaveJSProperty('tagName', 'STRONG');
    await expect(page.getByText('草稿待保存')).toBeVisible();
    await expect(page.getByText('笔记已保存')).toBeVisible({ timeout: 8000 });
    await expect(note).toBeFocused();
    const written = readFileSync(file, 'utf8');
    expect(written).toContain('**富文本追加**');
    expect(written).toContain('[[数据模型]]');
    expect(written).toContain('> 原讲义中的一句话。');
    expect(written).toContain('\x60\x60\x60\x60markdown\n\x60\x60\x60sql\nSELECT \'$5\';\n\n\x60\x60\x60\n\x60\x60\x60\x60');
    expect(written).toContain(math);
    expect(written).toContain(link);

    await note.locator('[data-type="inline-math"]').click();
    await expect(page.getByRole('textbox', { name: 'LaTeX', exact: true })).toHaveValue('x^2+1');
    await page.getByRole('textbox', { name: 'LaTeX', exact: true }).fill('x^2 + y^2');
    await page.getByRole('button', { name: '应用', exact: true }).click();
    await expect(note.locator('[data-type="inline-math"] annotation')).toHaveText('x^2 + y^2');
    await expect.poll(() => readFileSync(file, 'utf8'), { timeout: 8000 }).toContain('$x^2 + y^2$');

    const url = 'https://example.org/a)$amount';
    await note.getByRole('link', { name: 'SQLite 资料' }).evaluate(element => {
      const selection = window.getSelection()!;
      const range = document.createRange();
      range.selectNodeContents(element);
      selection.removeAllRanges();
      selection.addRange(range);
    });
    await page.getByRole('button', { name: '链接', exact: true }).click();
    await page.getByRole('textbox', { name: '链接地址' }).fill(url);
    await page.getByRole('button', { name: '应用', exact: true }).click();
    await expect.poll(() => readFileSync(file, 'utf8'), { timeout: 8000 }).toContain('<' + url + '>');

    const amounts = '第一笔 $5，第二笔 $7。';
    await note.locator('h1').evaluate(element => {
      element.closest<HTMLElement>('[contenteditable="true"]')?.focus();
      window.getSelection()?.collapse(element, element.childNodes.length);
    });
    await page.keyboard.press('Enter');
    await page.keyboard.insertText(amounts);
    await expect(note.getByText(amounts, { exact: true })).toBeVisible();
    await expect.poll(() => readFileSync(file, 'utf8'), { timeout: 8000 }).toContain('第一笔 \\$5，第二笔 \\$7。');

    await page.reload();
    await page.getByRole('button', { name: '笔记', exact: true }).click();
    await expect(note.getByText(amounts, { exact: true })).toBeVisible();
    await expect(note.locator('[data-type="inline-math"]')).toHaveCount(1);
    await expect(note.getByRole('link', { name: 'SQLite 资料' })).toHaveAttribute('href', url);
    await expect(note.locator('[data-type="inline-math"] annotation')).toHaveText('x^2 + y^2');
    await expect(note.locator('[data-wiki-reference]')).toHaveText('[[数据模型]]');
    await sourceMode.click();
    await expect(note).toHaveValue(readFileSync(file, 'utf8'));
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

test('notes keep unsupported Markdown and rebase an edit made during a save onto its confirmed revision', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-note-source-'));
  const service = start(folder);
  let unblock = () => {};
  try {
    const address = await service.url;
    const lesson = await seedNoteLesson(page, address);
    const file = join(folder, 'library', 'notes', lesson + '.md');
    const original = '# 本机原文\n\n| 术语 | 解释 |\n| --- | --- |\n| 行 | 一条记录 |\n\n附注[^1]。\n\n[^1]: 保留脚注。\n';
    mkdirSync(join(folder, 'library', 'notes'), { recursive: true });
    writeFileSync(file, original);

    await page.goto(address.replace('#/?', '#/lessons/' + lesson + '?'));
    await page.getByRole('button', { name: '笔记', exact: true }).click();
    const sourceMode = page.getByRole('button', { name: 'Markdown 原文' });
    const richMode = page.getByRole('button', { name: '富文本', exact: true });
    const note = page.getByRole('textbox', { name: '本节笔记' });
    const save = page.getByRole('button', { name: '保存笔记' });
    await expect(sourceMode).toHaveAttribute('aria-pressed', 'true');
    await expect(note).toHaveValue(original);
    await richMode.click();
    await expect(sourceMode).toHaveAttribute('aria-pressed', 'true');
    await expect(page.getByText(/需在 Markdown 原文中编辑/)).toBeVisible();
    await expect(save).toBeDisabled();
    expect(readFileSync(file, 'utf8')).toBe(original);

    const endpoint = '**/api/lessons/' + lesson + '/note';
    const gate = new Promise<void>(resolve => { unblock = resolve; });
    let firstWrite = true;
    let failNextWrite = false;
    await page.route(endpoint, async route => {
      if (route.request().method() === 'PUT') {
        if (failNextWrite) {
          failNextWrite = false;
          await route.fulfill({ status: 503, json: { detail: '本机文件暂时不可写。' } });
          return;
        }
        if (firstWrite) {
          firstWrite = false;
          await gate;
        }
      }
      await route.continue();
    });
    const next = original + '\n先保存一段。';
    const later = next + '\n自动保存这段补充。';
    await note.fill(next);
    await save.click();
    await expect(page.getByText('正在保存笔记', { exact: true })).toBeVisible();
    await note.fill(later);
    await expect(note).toBeFocused();
    unblock();
    await expect.poll(() => readFileSync(file, 'utf8')).toBe(next);
    await expect.poll(() => readFileSync(file, 'utf8'), { timeout: 8000 }).toBe(later);
    await expect(page.getByRole('dialog', { name: '处理笔记冲突' })).toHaveCount(0);
    await expect(note).toHaveValue(later);

    failNextWrite = true;
    const retry = later + '\n保留保存失败时的内容。';
    await note.fill(retry);
    await expect(page.getByText('草稿保存失败')).toBeVisible({ timeout: 8000 });
    await expect(page.getByRole('alert').filter({ hasText: '本机文件暂时不可写。' })).toBeVisible();
    expect(readFileSync(file, 'utf8')).toBe(later);
    await expect(note).toHaveValue(retry);
    await save.click();
    await expect.poll(() => readFileSync(file, 'utf8')).toBe(retry);

    const nearLimit = 'array[0] '.repeat(5400);
    writeFileSync(file, nearLimit);
    await page.reload();
    await page.getByRole('button', { name: '笔记', exact: true }).click();
    await expect(sourceMode).toHaveAttribute('aria-pressed', 'true');
    await expect(note).toHaveValue(nearLimit);
    await expect(save).toBeDisabled();
  } finally {
    unblock();
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

test('note drafts keep their original revision and saved conflicts can be merged after restart', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-note-conflict-'));
  let service = start(folder);
  let lessonId: string;
  const lessonUrl = (address: string) => address.replace('#/?', `#/lessons/${lessonId}?`);
  try {
    const address = await service.url;
    lessonId = await seedNoteLesson(page, address);
    await page.goto(lessonUrl(address));
    await page.getByRole('button', { name: '笔记', exact: true }).click();
    await page.getByRole('button', { name: 'Markdown 原文' }).click();
    const editor = page.getByRole('textbox', { name: '本节笔记' });
    await editor.fill('初始笔记');
    await page.getByRole('button', { name: '保存笔记' }).click();
    await expect(page.getByText('笔记已保存')).toBeVisible();
    const path = join(folder, 'library', 'notes', `${lessonId}.md`);

    await editor.fill('应用草稿中的例子');
    writeFileSync(path, '外部编辑新增的描述');
    await page.reload();
    await page.getByRole('button', { name: '笔记 · 未保存' }).click();
    await page.getByRole('button', { name: 'Markdown 原文' }).click();
    await expect(editor).toHaveValue('应用草稿中的例子');
    const dialog = page.getByRole('dialog', { name: '处理笔记冲突' });
    await expect(dialog.getByLabel('版本差异')).toContainText('-外部编辑新增的描述', { timeout: 8000 });
    await expect(dialog.getByLabel('版本差异')).toContainText('+应用草稿中的例子');
    expect(readFileSync(path, 'utf8')).toBe('外部编辑新增的描述');

    await stop(service.child);
    service = start(folder);
    await page.goto(lessonUrl(await service.url));
    await page.getByRole('button', { name: '笔记', exact: true }).click();
    await page.getByRole('button', { name: /查看冲突/ }).click();
    await dialog.getByRole('button', { name: '手动合并' }).click();
    await dialog.getByRole('textbox', { name: '合并笔记' }).fill('外部描述和应用例子的合并笔记');
    await dialog.getByRole('button', { name: '保存合并结果' }).click();
    await expect(dialog).toHaveCount(0);
    await page.getByRole('button', { name: 'Markdown 原文' }).click();
    await expect(editor).toHaveValue('外部描述和应用例子的合并笔记');
    expect(readFileSync(path, 'utf8')).toBe('外部描述和应用例子的合并笔记');

    await page.getByRole('link', { name: '知识库', exact: true }).click();
    await page.getByRole('searchbox', { name: '搜索知识库' }).fill('合并笔记');
    await page.getByRole('button', { name: /理解表、行和列 · 笔记/ }).click();
    await expect(page.getByText('外部描述和应用例子的合并笔记', { exact: true }).last()).toBeVisible();

    await page.goto(lessonUrl(await service.url));
    await page.getByRole('button', { name: '笔记', exact: true }).click();
    await page.getByRole('button', { name: 'Markdown 原文' }).click();
    await page.getByText('版本历史', { exact: true }).click();
    await page.getByRole('button', { name: /初始笔记/ }).click();
    const history = page.getByRole('dialog', { name: '查看笔记版本' });
    await expect(history.getByLabel('版本差异')).toContainText('+初始笔记');
    await history.getByRole('button', { name: '恢复此版本' }).click();
    await expect(editor).toHaveValue('初始笔记');
    expect(readFileSync(path, 'utf8')).toBe('初始笔记');
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

test('project filters apply on confirmation and saved views survive restart', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-views-'));
  let service = start(folder);
  const toBoard = (address: string) => {
    const url = new URL(address);
    url.hash = url.hash.replace('#/?', '#/projects?');
    return url.href;
  };
  try {
    const url = new URL(await service.url);
    const token = new URLSearchParams(url.hash.split('?')[1]).get('session');
    const headers = { Authorization: `Bearer ${token}` };
    for (const [suffix, topic] of [['1', '机器学习实践'], ['2', '个人知识库']]) {
      const response = await page.request.post(new URL('/api/projects', url).href, {
        headers, data: { id: `10000000-0000-4000-8000-00000000000${suffix}`, topic, outcome: '整理一份学习记录' },
      });
      expect(response.status()).toBe(201);
    }
    await page.goto(toBoard(url.href));
    const main = page.getByRole('main');
    await main.getByRole('link', { name: '机器学习实践' }).click();
    await main.getByRole('button', { name: '编辑标签与优先级' }).click();
    const metadata = page.getByRole('dialog', { name: '标签与优先级' });
    await metadata.getByLabel('项目优先级').selectOption('high');
    await metadata.getByLabel('添加标签').fill('AI');
    await metadata.getByRole('button', { name: '添加标签' }).click();
    await metadata.getByRole('button', { name: '保存' }).click();
    await expect(metadata).toHaveCount(0);
    await main.getByRole('link', { name: '全部项目' }).click();
    await expect(main.getByText('共 2 个匹配项目')).toBeVisible();

    const openFilters = () => main.getByRole('button', { name: /^筛选/ }).click();
    const dialog = page.getByRole('dialog', { name: '筛选学习项目' });
    await openFilters();
    await dialog.getByRole('button', { name: '添加条件' }).click();
    await dialog.getByRole('button', { name: '应用筛选' }).click();
    await expect(dialog.getByRole('alert')).toContainText('条件 1');
    await dialog.getByLabel('条件 1 字段').selectOption('tag');
    await dialog.getByRole('checkbox', { name: 'AI', exact: true }).check();
    await dialog.getByRole('button', { name: '添加条件' }).click();
    await dialog.getByLabel('条件 2 字段').selectOption('priority');
    await dialog.getByLabel('条件 2 优先级').selectOption('high');
    await expect(main.getByText('共 2 个匹配项目')).toBeVisible();
    await dialog.getByRole('button', { name: '应用筛选' }).click();
    await expect(main.getByText('共 1 个匹配项目')).toBeVisible();
    await expect(main.getByRole('heading', { name: '机器学习实践' })).toBeVisible();

    await openFilters();
    await dialog.getByRole('button', { name: '清空条件' }).click();
    await dialog.getByRole('button', { name: '取消' }).click();
    await expect(main.getByText('共 1 个匹配项目')).toBeVisible();
    await main.getByRole('button', { name: '分组：状态' }).click();
    const grouping = page.getByRole('dialog', { name: '分组设置' });
    await grouping.getByRole('button', { name: '已学完上移' }).click();
    await grouping.getByRole('checkbox', { name: '已暂停' }).uncheck();
    await grouping.getByRole('button', { name: '应用分组' }).click();
    await expect(main.getByRole('region', { name: '已暂停', exact: true })).toHaveCount(0);
    await main.getByRole('button', { name: '保存视图', exact: true }).click();
    const save = page.getByRole('dialog', { name: '保存视图' });
    await save.getByLabel('视图名称').fill('AI 学习');
    await save.getByRole('button', { name: '保存视图', exact: true }).click();
    await expect(main.getByLabel('项目视图').locator('option:checked')).toHaveText('AI 学习');

    await stop(service.child);
    service = start(folder);
    await page.goto(toBoard(await service.url));
    await expect(main.getByText('共 2 个匹配项目')).toBeVisible();
    await main.getByLabel('项目视图').selectOption({ label: 'AI 学习' });
    await expect(main.getByText('共 1 个匹配项目')).toBeVisible();
    await expect(main.getByRole('region', { name: '已暂停', exact: true })).toHaveCount(0);
    await main.getByRole('button', { name: '移除条件：优先级 高' }).click();
    await expect(main.getByText('有未保存更改')).toBeVisible();
    await main.getByRole('button', { name: '更新视图', exact: true }).click();
    const update = page.getByRole('dialog', { name: '更新视图' });
    await update.getByRole('button', { name: '更新视图', exact: true }).click();
    await expect(main.getByText('有未保存更改')).toHaveCount(0);
    await main.getByRole('button', { name: '删除视图', exact: true }).click();
    await page.getByRole('dialog', { name: '删除视图' }).getByRole('button', { name: '删除视图', exact: true }).click();
    await expect(main.getByLabel('项目视图').locator('option', { hasText: 'AI 学习' })).toHaveCount(0);
    await main.getByRole('button', { name: '清除筛选', exact: true }).click();
    await expect(main.getByText('共 2 个匹配项目')).toBeVisible();
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

test('narrow layout keeps search, navigation, and text import usable', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-narrow-'));
  const service = start(folder);
  try {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(await service.url);
    await expect(page.getByRole('heading', { name: '今天，想弄懂什么？' })).toBeVisible();
    await page.getByLabel('学习问题或目标').fill('我想练习法语口语');
    await page.getByRole('button', { name: '开始探索' }).click();
    await expect(page.getByRole('heading', { name: '我想练习法语口语', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: '学习目标' })).toHaveCount(0);
    await page.getByRole('button', { name: '整理为学习目标' }).click();
    await expect(page.getByRole('textbox', { name: '学习主题', exact: true })).toHaveValue('法语口语');
    await page.getByRole('textbox', { name: '预期成果', exact: true }).fill('能用法语介绍自己');
    await page.getByRole('button', { name: '提交并生成路线' }).click();
    await expect(page.getByText('3 / 3 节已就绪')).toBeVisible();
    await page.getByRole('navigation', { name: '主要导航' }).getByRole('link', { name: '全部项目' }).click();
    await page.getByLabel('搜索项目名称或目标').fill('法语');
    await expect(page.getByRole('heading', { name: '法语口语' })).toBeVisible();
    await page.getByRole('link', { name: '知识库', exact: true }).click();
    const mixed = 'Straße '.repeat(90) + '现在记录中文复习计划和重点。' + ' 后续正文'.repeat(48);
    await page.getByLabel('导入资料').setInputFiles({
      name: '练习.txt', mimeType: 'text/plain', buffer: Buffer.from('一段练习资料。\n' + mixed),
    });
    await page.getByRole('button', { name: /练习.txt/ }).click();
    await expect(page.locator('.library-preview .plain-file')).toContainText('一段练习资料。');

    const search = page.getByRole('searchbox', { name: '搜索知识库' });
    for (const word of ['复习', '复习计划']) {
      await search.fill(word);
      const hit = page.getByRole('button', { name: /练习\.txt.*第 2 行/ });
      await expect(hit.locator('.hit-excerpt')).toContainText('中文复习计划');
      await hit.click();
      await expect(page.locator('.file-anchor')).toContainText('现在记录中文复习计划和重点。');
    }
    await page.evaluate(() => {
      const params = new URLSearchParams(location.hash.split('?')[1]);
      params.set('at', '9');
      params.delete('revision');
      location.hash = '/library?' + params;
    });
    const preview = page.locator('.library-preview');
    await expect(preview.getByRole('alert')).toContainText('找不到文件中的这一行或段落');
    await expect(preview.locator('.file-anchor')).toHaveCount(0);
    await preview.getByRole('button', { name: '查看当前版本' }).click();
    await expect(preview.locator('.plain-file')).toContainText('中文复习计划');
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

test('knowledge library preserves search context through graph, full file, and reload', async ({ page }, info) => {
  test.setTimeout(48000);
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-library-context-'));
  const service = start(folder);
  const query = '保留搜索上下文';
  const body = '第二行记录如何保留搜索上下文。';
  const filename = '上下文参考.txt';
  try {
    await page.setViewportSize({ width: 390, height: 1040 });
    const origin = new URL(await service.url);
    const key = new URLSearchParams(origin.hash.split('?')[1]).get('session');
    const headers = { Authorization: 'Bearer ' + key };
    async function api(path: string, method: string, data?: object) {
      const response = await page.request.fetch(origin.origin + '/api' + path, { method, headers, data });
      expect(response.ok()).toBeTruthy();
      return response.json();
    }

    const project = await api('/projects', 'POST', {
      id: '00000000-0000-4000-8000-000000000019', topic: 'SQLite 入门', outcome: '验证项目筛选可以续接',
    });
    const target = await api('/files', 'POST', { name: filename, content: '前言\n' + body });
    await api('/files', 'POST', { name: '范围以外.txt', content: query + '的另一份资料' });
    await api('/files/tags', 'POST', { ids: [target.id], tag: '核对', action: 'add' });
    await api('/files/' + target.id + '/favorite', 'PUT', { favorite: true });
    await page.goto(origin.href);
    await page.evaluate(() => { window.location.hash = '#/library?folder=library%2Fimports'; });
    const search = page.getByRole('searchbox', { name: '搜索知识库' });
    const mode = page.getByRole('combobox', { name: '搜索方式' });
    const scope = page.getByRole('combobox', { name: '搜索范围' });
    const type = page.getByRole('combobox', { name: '文件类型' });
    const tag = page.getByRole('combobox', { name: '文件标签' });
    const shelf = page.getByRole('combobox', { name: '文件视图' });
    await mode.selectOption('hybrid');
    await scope.selectOption('all');
    await type.selectOption('txt');
    await tag.selectOption({ label: '核对' });
    await shelf.selectOption('favorites');
    await search.fill(query);
    const visibleNames = page.locator('.library-list .library-file strong');
    const file = page.getByRole('button', { name: /上下文参考\.txt.*第 2 行/ });
    await expect(file).toBeVisible();
    await expect(visibleNames).toHaveText([filename]);

    const views = page.getByRole('navigation', { name: '知识库视图' });
    await views.getByRole('link', { name: '图谱', exact: true }).click();
    await expect(views.getByRole('link', { name: '图谱', exact: true })).toHaveAttribute('aria-current', 'page');
    await views.getByRole('link', { name: '文件', exact: true }).click();
    async function expectSearch() {
      await expect(search).toHaveValue(query);
      await expect(mode).toHaveValue('hybrid');
      await expect(scope).toHaveValue('all');
      await expect(type).toHaveValue('txt');
      await expect(tag).toHaveValue('核对');
      await expect(shelf).toHaveValue('favorites');
      await expect(visibleNames).toHaveText([filename]);
      expect(new URLSearchParams(new URL(page.url()).hash.split('?')[1]).get('folder')).toBe('library/imports');
    }
    await expectSearch();

    await file.dblclick();
    await expect(page.getByRole('button', { name: '返回文件列表' })).toBeVisible();
    await expect(page.locator('.file-anchor')).toContainText(body);
    const anchored = new URLSearchParams(new URL(page.url()).hash.split('?')[1]);
    expect(anchored.get('at')).toBe('2');
    expect(anchored.get('revision')).toBeTruthy();
    await page.reload();
    await expect(page.locator('.file-anchor')).toContainText(body);
    await page.getByRole('button', { name: '返回文件列表' }).click();
    await expectSearch();
    await page.reload();
    await expectSearch();
    const filters = page.locator('.library-toolbar');
    await filters.evaluate(node => node.scrollIntoView({ block: 'center' }));
    await filters.screenshot({ path: info.outputPath('检索条件恢复.png') });

    const category = page.locator('.library-types-list').getByRole('button', { name: '导入资料', exact: true });
    await category.click();
    await scope.selectOption('current');
    const projectFilter = page.getByRole('combobox', { name: '学习项目' });
    await projectFilter.selectOption(project.id);
    await expect(page.getByText('没有匹配的内容', { exact: true })).toBeVisible();
    await views.getByRole('link', { name: '图谱', exact: true }).click();
    await views.getByRole('link', { name: '文件', exact: true }).click();
    await expect(projectFilter).toHaveValue(project.id);
    await expect(category).toHaveAttribute('aria-pressed', 'true');
    await expect(scope).toHaveValue('current');
    await expect(search).toHaveValue(query);
  } finally {
    await stop(service.child);
    const fixture = realpathSync(folder);
    if (dirname(fixture) !== realpathSync(tmpdir()) || !fixture.split(/[\\/]/).at(-1)?.startsWith('zhixue-library-context-')) {
      throw new Error('Unexpected library search fixture directory');
    }
    rmSync(fixture, { recursive: true, force: true });
  }
});

test('knowledge library imports a PDF, filters content hits, opens its original page, and can reuse a duplicate', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-library-'));
  const service = start(folder);
  const source = execFileSync(resolve(python), ['-c', [
    'import pymupdf, sys',
    'with pymupdf.open() as document:',
    '    document.new_page().insert_text((72, 72), "First page")',
    '    document.new_page().insert_text((72, 72), "知识库增量索引", fontname="china-s")',
    '    document.new_page()',
    '    sys.stdout.buffer.write(document.tobytes())',
  ].join('\n')]);
  try {
    await page.goto(await service.url);
    await page.getByRole('link', { name: '知识库', exact: true }).click();
    const input = page.getByLabel('导入资料');
    await input.setInputFiles({ name: '参考教程.pdf', mimeType: 'application/pdf', buffer: source });
    await expect(page.getByRole('heading', { name: '参考教程.pdf' })).toBeVisible();
    await expect(page.getByText('第 3 页无可提取文字')).toBeVisible();
    await input.setInputFiles({ name: '重复教程.pdf', mimeType: 'application/pdf', buffer: source });
    await expect(page.getByText('相同内容已存在')).toBeVisible();
    await page.getByRole('button', { name: '复用已有文件' }).click();
    await expect(page.getByText('相同内容已存在')).toHaveCount(0);

    await page.getByRole('button', { name: '课程', exact: true }).click();
    await page.getByRole('searchbox', { name: '搜索知识库' }).fill('增量索引');
    await expect(page.getByText('没有匹配的内容')).toBeVisible();
    await page.getByLabel('搜索范围').selectOption('all');
    await page.getByLabel('文件类型').selectOption('pdf');
    await page.getByRole('button', { name: /参考教程\.pdf.*第 2 页/ }).click();
    await expect(page.getByText('知识库增量索引', { exact: true }).last()).toBeVisible();
    await expect(page.getByRole('img', { name: 'PDF 原件第 2 页' })).toBeVisible();
    await expect.poll(() => page.getByRole('img', { name: 'PDF 原件第 2 页' }).evaluate(image => (image as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
    const download = page.waitForEvent('download');
    await page.getByRole('button', { name: '下载原件' }).click();
    const downloaded = await download;
    expect(downloaded.suggestedFilename()).toBe('参考教程.pdf');
    expect(readFileSync(await downloaded.path())).toEqual(source);
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

test('knowledge library expands the complete local semantic hit and opens its pinned source line', async ({ page }, info) => {
  const assets = process.env.ZHIXUE_TEST_SEMANTIC_ASSETS;
  if (!assets) {
    test.skip(true, 'Requires the pinned local semantic model files');
    return;
  }
  test.setTimeout(65000);
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-semantic-excerpt-'));
  let service: ReturnType<typeof start> | undefined;
  const context = 'Before looking up the answer, open the appendix and locate the examples. The glossary explains the representation used throughout the document. Each example shows a different operation. ';
  const lastSentence = 'Chunk long documents into overlapping passages before indexing.';
  const query = 'how to split long documents into overlapping sections';
  try {
    const model = join(folder, '.semantic', 'multilingual-e5-small');
    mkdirSync(model, { recursive: true });
    for (const name of ['model_quantized.onnx', 'tokenizer.json']) {
      copyFileSync(join(assets, name), join(model, name));
    }
    service = start(folder);
    await page.setViewportSize({ width: 390, height: 840 });
    await page.goto(await service.url);
    await page.getByRole('link', { name: '知识库', exact: true }).click();
    await page.getByLabel('导入资料').setInputFiles({
      name: '长文.txt', mimeType: 'text/plain', buffer: Buffer.from('前言\n' + context + lastSentence),
    });
    await expect(page.getByRole('heading', { name: '长文.txt' })).toBeVisible();
    await page.getByRole('combobox', { name: '搜索方式' }).selectOption('hybrid');
    await page.getByRole('button', { name: '启用语义搜索', exact: true }).click();
    await page.getByRole('searchbox', { name: '搜索知识库' }).fill(query);
    const hit = page.getByRole('button', { name: /长文\.txt.*第 2 行.*语义相关/ });
    await expect(hit).toBeVisible({ timeout: 16000 });
    await expect(hit.locator('.hit-excerpt')).not.toContainText(lastSentence);
    const disclosure = page.getByLabel('查看长文.txt 第 2 行的语义命中片段', { exact: true });
    const previous = page.url();
    await expect(disclosure).toBeVisible({ timeout: 4000 });
    await disclosure.focus();
    await page.keyboard.press('Enter');
    await expect(disclosure.locator('..').getByText(context + lastSentence, { exact: true })).toBeVisible();
    await expect(page).toHaveURL(previous);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
    await disclosure.locator('..').screenshot({ path: info.outputPath('完整语义命中片段.png') });

    await hit.click();
    await expect(page.locator('.file-anchor')).toContainText(lastSentence);
    const location = new URLSearchParams(new URL(page.url()).hash.split('?')[1]);
    expect(location.get('at')).toBe('2');
    expect(location.get('revision')).toBeTruthy();

    await stop(service.child);
    service = undefined;
    const cache = join(folder, '.semantic', 'e5-small-v1.sqlite3');
    writeFileSync(cache, 'Invalid index data '.repeat(240));
    service = start(folder);
    await page.goto(await service.url);
    await page.getByRole('link', { name: '知识库', exact: true }).click();
    await page.getByRole('combobox', { name: '搜索方式' }).selectOption('hybrid');
    await expect(page.getByText('本地语义检索暂时不可用。请重试语义索引。')).toBeVisible();
    await page.getByRole('searchbox', { name: '搜索知识库' }).fill('appendix and locate the examples');
    await expect(page.getByRole('button', { name: /长文\.txt.*第 2 行/ })).toBeVisible();
    await page.getByRole('button', { name: '重建索引', exact: true }).click();
    await expect(page.getByText('索引已重建', { exact: true })).toBeVisible();
    await page.getByRole('searchbox', { name: '搜索知识库' }).fill(query);
    await expect(hit).toBeVisible({ timeout: 22000 });
    await expect(disclosure).toBeVisible();
    await disclosure.click();
    await expect(disclosure.locator('..').getByText(context + lastSentence, { exact: true })).toBeVisible();

    const literalName = query + '.txt';
    const isVisit = (request: { method(): string; url(): string }) =>
      request.method() === 'POST' && /\/api\/files\/[^/]+\/opened$/.test(request.url());
    const firstVisit = page.waitForResponse(response => isVisit(response.request()) && response.ok());
    await page.getByLabel('导入资料').setInputFiles({
      name: literalName, mimeType: 'text/plain', buffer: Buffer.from(' \n'),
    });
    await expect(page.getByRole('heading', { name: literalName, exact: true })).toBeVisible();
    await firstVisit;
    const fileView = page.getByRole('combobox', { name: '文件视图' });
    const resultTitles = page.locator('.library-list .library-file strong');
    const visits: string[] = [];
    const trackVisit = (request: { method(): string; url(): string }) => {
      if (isVisit(request)) visits.push(new URL(request.url()).pathname);
    };
    page.on('request', trackVisit);
    await fileView.selectOption('recent');
    await page.getByRole('searchbox', { name: '搜索知识库' }).fill(query);
    await expect(resultTitles).toHaveText([literalName, '长文.txt']);
    expect(visits).toEqual([]);

    const readAgain = page.waitForResponse(response => isVisit(response.request()) && response.ok());
    await hit.click();
    await readAgain;
    await expect(resultTitles).toHaveText(['长文.txt', literalName]);
    await expect(page.locator('.file-anchor')).toContainText(lastSentence);
    await page.locator('.library-list').screenshot({ path: info.outputPath('最近访问混合排序.png') });
    await fileView.selectOption('all');
    await expect(resultTitles).toHaveText([literalName, '长文.txt']);
    expect(visits).toHaveLength(1);
    page.off('request', trackVisit);
  } finally {
    if (service) await stop(service.child);
    const fixture = realpathSync(folder);
    if (dirname(fixture) !== realpathSync(tmpdir()) || !fixture.split(/[\\/]/).at(-1)?.startsWith('zhixue-semantic-excerpt-')) {
      throw new Error('Unexpected semantic search fixture directory');
    }
    rmSync(fixture, { recursive: true, force: true });
  }
});

test('knowledge library repairs a copied file only after confirming the candidate preview', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-library-repair-'));
  const service = start(folder);
  try {
    await page.goto(await service.url);
    await page.getByRole('link', { name: '知识库', exact: true }).click();
    await page.getByLabel('导入资料').setInputFiles({
      name: '维修定位练习.txt', mimeType: 'text/plain', buffer: Buffer.from('原引用继续来自这份资料。'),
    });
    await expect(page.getByRole('heading', { name: '维修定位练习.txt' })).toBeVisible();
    const hash = new URL(page.url()).hash;
    const identity = new URLSearchParams(hash.split('?')[1]).get('file');
    expect(identity).toBeTruthy();
    const previous = join(folder, 'library', 'imports', `${identity}.txt`);
    const relocated = join(folder, 'library', '整理', '复制的资料.txt');
    mkdirSync(join(folder, 'library', '整理'), { recursive: true });
    copyFileSync(previous, relocated);
    unlinkSync(previous);

    await page.reload();
    await page.getByRole('button', { name: '修复文件位置', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '修复文件位置' });
    await expect(dialog.getByRole('button', { name: '确认关联' })).toBeDisabled();
    await dialog.getByLabel('从知识库选择同类型文件').selectOption('library/整理/复制的资料.txt');
    await dialog.getByRole('button', { name: '预览候选' }).click();
    await expect(dialog.getByText('内容与已知版本一致')).toBeVisible();
    await dialog.getByRole('button', { name: '确认关联' }).click();
    await expect(dialog).toHaveCount(0);
    await expect(page.getByRole('heading', { name: '复制的资料.txt' })).toBeVisible();
    await expect(page.getByText('原引用继续来自这份资料。')).toBeVisible();
    expect(new URL(page.url()).hash).toBe(hash);
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

test('knowledge library trashes, restores beside an occupied path, and only purges after confirmation', async ({ page }, info) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-library-trash-'));
  const service = start(folder);
  try {
    await page.goto(await service.url);
    await page.getByRole('link', { name: '知识库', exact: true }).click();
    await page.getByLabel('导入资料').setInputFiles({
      name: '回收站练习.txt', mimeType: 'text/plain', buffer: Buffer.from('恢复后引用的原件内容。'),
    });
    await expect(page.getByRole('heading', { name: '回收站练习.txt' })).toBeVisible();
    const activeUrl = page.url();
    const identity = new URLSearchParams(new URL(activeUrl).hash.split('?')[1]).get('file');
    const old = join(folder, 'library', 'imports', `${identity}.txt`);
    const address = new URL(await service.url);
    const token = new URLSearchParams(address.hash.split('?')[1]).get('session');
    const authored = await page.request.post(address.origin + '/api/files', {
      headers: { Authorization: 'Bearer ' + token },
      data: {
        name: '引用回收原件.md',
        content: '此处参考 [[' + identity + '|回收站练习]] 和 [[library/imports/' + identity + '.txt|旧路径引用]]。',
      },
    });
    expect(authored.ok()).toBeTruthy();
    await page.getByRole('button', { name: '移入回收站', exact: true }).click();
    const review = page.getByRole('dialog', { name: '移入回收站' });
    await review.getByText('1 份文件通过双链引用此原件', { exact: true }).click();
    await expect(review.getByText('引用回收原件.md', { exact: true })).toBeVisible();
    await expect(review.getByText('1 处原文位置', { exact: true })).toBeVisible();
    await review.screenshot({ path: info.outputPath('回收站双链影响.png') });
    await review.getByRole('button', { name: '取消' }).click();
    expect(readFileSync(old, 'utf8')).toBe('恢复后引用的原件内容。');
    const deleteFile = async () => {
      await page.getByRole('button', { name: '移入回收站', exact: true }).click();
      const confirm = page.getByRole('dialog', { name: '移入回收站' });
      await confirm.getByRole('button', { name: '确认移入回收站' }).click();
      await expect(page.getByRole('heading', { name: '回收站', exact: true })).toBeVisible();
    };

    await deleteFile();
    const wikiSource = (await authored.json()).id as string;
    const sourceUrl = address.origin + '/#/library?' + new URLSearchParams({ file: wikiSource });
    const preview = page.locator('.library-preview');
    const originalLink = preview.getByRole('link', { name: '[[旧路径引用]]', exact: true });
    await page.goto(sourceUrl);
    await originalLink.click();
    await expect(preview.getByText('文件已移入知识库回收站。', { exact: true })).toBeVisible();
    const other = await page.request.post(address.origin + '/api/files/upload?name=作为占用者的普通文件.txt', {
      headers: { Authorization: 'Bearer ' + token }, data: '占用原路径的新内容。',
    });
    expect(other.ok()).toBeTruthy();
    const replacement = (await other.json()).id as string;
    const occupiedSource = join(folder, 'library', 'imports', replacement + '.txt');
    renameSync(occupiedSource, old);
    await page.goto(sourceUrl);
    await originalLink.click();
    const archivedChoice = preview.locator('.wiki-options').getByRole('link').filter({ hasText: '已移入回收站' });
    await expect(archivedChoice).toBeVisible();
    await expect(preview.locator('.wiki-options').getByRole('link')).toHaveCount(2);
    await preview.screenshot({ path: info.outputPath('回收站双链候选.png') });
    await archivedChoice.click();
    await preview.getByRole('button', { name: '查看回收站', exact: true }).click();
    await page.getByRole('button', { name: '恢复文件', exact: true }).click();
    const transfer = page.getByRole('dialog', { name: '恢复文件' });
    await transfer.getByRole('button', { name: '确认恢复' }).click();
    await expect(transfer.getByRole('alert')).toContainText('已占用');
    await transfer.getByLabel('恢复位置').fill('library/imports/恢复的资料.txt');
    await transfer.getByRole('button', { name: '确认恢复' }).click();
    await expect(page.getByRole('heading', { name: '恢复的资料.txt' })).toBeVisible();
    await expect(page.getByText('恢复后引用的原件内容。', { exact: true })).toBeVisible();
    expect(readFileSync(old, 'utf8')).toBe('占用原路径的新内容。');
    expect(page.url()).toBe(activeUrl);
    await preview.getByText('反向链接', { exact: true }).click();
    await preview.getByRole('link', { name: '引用回收原件.md · 第 1 行' }).click();
    await expect(preview.locator('.file-anchor')).toContainText('此处参考');
    await originalLink.click();
    await preview.locator('.wiki-options').getByRole('link').filter({ hasText: '恢复的资料.txt' }).click();
    await expect(preview.getByRole('heading', { name: '恢复的资料.txt' })).toBeVisible();

    await deleteFile();
    await page.getByRole('button', { name: '手动清理' }).click();
    const purge = page.getByRole('dialog', { name: '清理回收站原件' });
    await purge.getByRole('button', { name: '取消' }).click();
    await expect(page.getByRole('button', { name: '恢复文件' })).toBeVisible();
    await page.getByRole('button', { name: '手动清理' }).click();
    await purge.getByRole('button', { name: '确认清理' }).click();
    await expect(page.getByText('回收站为空')).toBeVisible();
    await page.goto(activeUrl);
    await expect(page.getByText('回收站原件已清理。学习记录与引用仍保留。')).toBeVisible();
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});


test('knowledge library reviews an unchanged trash original after its file identity changes', async ({ page }, info) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-trash-identity-review-'));
  const service = start(folder);
  try {
    const address = new URL(await service.url);
    await page.goto(address.href);
    const token = new URLSearchParams(address.hash.split('?')[1]).get('session');
    const headers = { Authorization: 'Bearer ' + token };
    const body = '旧版换机后保留的回收原件。';
    const imported = await page.request.post(address.origin + '/api/files/upload?name=换机资料.txt', { headers, data: body });
    expect(imported.ok()).toBeTruthy();
    const { id } = await imported.json();
    const response = await page.request.get(address.origin + '/api/files/' + id, { headers });
    expect(response.ok()).toBeTruthy();
    const { path, revision } = await response.json();
    const trashed = await page.request.post(address.origin + '/api/files/' + id + '/trash', { headers, data: { path, revision } });
    expect(trashed.ok()).toBeTruthy();
    const entry = await trashed.json();
    const archived = join(folder, entry.archivePath);
    const copied = join(dirname(archived), 'copied.txt');
    writeFileSync(copied, body);
    renameSync(copied, archived);
    await page.evaluate(trash => { window.location.hash = '#/library?' + new URLSearchParams({ trash }); }, entry.id);
    await page.getByRole('button', { name: '恢复文件', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '恢复文件' });
    await dialog.getByLabel('恢复位置').fill('library/imports/换机找回.txt');
    await dialog.getByRole('button', { name: '确认恢复', exact: true }).click();
    await expect(dialog.getByText('当前内容与回收记录的版本一致。', { exact: false })).toBeVisible();
    await dialog.screenshot({ path: info.outputPath('换机回收原件核对.png') });
    await dialog.getByRole('button', { name: '取消', exact: true }).click();
    expect(readFileSync(archived, 'utf8')).toBe(body);
    await page.getByRole('button', { name: '恢复文件', exact: true }).click();
    await dialog.getByLabel('恢复位置').fill('library/imports/换机找回.txt');
    await dialog.getByRole('button', { name: '确认恢复', exact: true }).click();
    await expect(dialog.getByRole('button', { name: '按核对版本恢复' })).toBeVisible();
    await dialog.getByLabel('恢复位置').fill('library/imports/确认的新位置.txt');
    await expect(dialog.getByRole('button', { name: '按核对版本恢复' })).toHaveCount(0);
    await dialog.getByRole('button', { name: '确认恢复', exact: true }).click();
    await dialog.getByRole('button', { name: '按核对版本恢复' }).click();
    await expect(page.getByRole('heading', { name: '确认的新位置.txt' })).toBeVisible();
    await expect(page.getByText(body, { exact: true })).toBeVisible();
    expect(new URLSearchParams(new URL(page.url()).hash.split('?')[1]).get('file')).toBe(id);
    const restored = await page.request.get(address.origin + '/api/files/' + id, { headers });
    expect((await restored.json()).revision).toBe(revision);
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

test('batch trash shows a stale file separately and requires a fresh preview to finish it', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-trash-batch-'));
  const service = start(folder);
  try {
    await page.goto(await service.url);
    await page.getByRole('link', { name: '知识库', exact: true }).click();
    async function add(name: string) {
      await page.getByLabel('导入资料').setInputFiles({
        name, mimeType: 'text/plain', buffer: Buffer.from(`${name} 的批量回收测试内容`),
      });
      await expect(page.getByRole('heading', { name })).toBeVisible();
      const id = new URLSearchParams(new URL(page.url()).hash.split('?')[1]).get('file');
      return join(folder, 'library', 'imports', `${id}.txt`);
    }
    const first = await add('批量甲.txt');
    const second = await add('批量乙.txt');
    await page.getByLabel('导入资料').setInputFiles({
      name: '批量引用说明.md', mimeType: 'text/markdown', buffer: Buffer.from('优先保留 [[批量乙]]。'),
    });
    await expect(page.getByRole('heading', { name: '批量引用说明.md' })).toBeVisible();
    await page.getByRole('button', { name: '多选文件' }).click();
    await page.getByRole('button', { name: /批量甲\.txt/ }).click();
    await page.getByRole('button', { name: /批量乙\.txt/ }).click();
    await page.getByRole('button', { name: '移入回收站 · 2 项' }).click();
    const dialog = page.getByRole('dialog', { name: '移入回收站' });
    await expect(dialog.getByRole('button', { name: '确认移入 2 个文件' })).toBeEnabled();
    await dialog.getByText('1 份文件通过双链引用此原件', { exact: true }).click();
    await expect(dialog.getByText('批量引用说明.md', { exact: true })).toBeVisible();
    writeFileSync(second, '确认期间的外部更新');
    await dialog.getByRole('button', { name: '确认移入 2 个文件' }).click();
    await expect(dialog.getByText('已移入回收站', { exact: true })).toBeVisible();
    await expect(dialog.getByRole('alert')).toContainText('版本已变化');
    expect(readFileSync(second, 'utf8')).toBe('确认期间的外部更新');
    expect(() => readFileSync(first)).toThrow();

    await dialog.getByRole('button', { name: '重新预览未处理文件' }).click();
    await dialog.getByRole('button', { name: '确认移入 1 个文件' }).click();
    await expect(page.getByRole('heading', { name: '回收站', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: '恢复文件' })).toHaveCount(2);
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

test('study plan keeps weekly hours when one day is shortened and moves overflow with its due date', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-daily-plan-'));
  const service = start(folder);
  try {
    const url = new URL(await service.url);
    const token = new URLSearchParams(url.hash.split('?')[1]).get('session');
    const headers = { Authorization: `Bearer ${token}` };
    await page.goto(url.href);
    const created = await page.request.post(`${url.origin}/api/projects`, { headers, data: {
      id: '00000000-0000-4000-8000-000000000019', topic: 'SQLite 入门', outcome: '能筛选本地学习记录',
    } });
    expect(created.status()).toBe(201);
    const project = await created.json();
    await expect.poll(async () => {
      const response = await page.request.get(`${url.origin}/api/projects/${project.id}`, { headers });
      return (await response.json()).task.status;
    }).toBe('ready');
    const learned = await page.request.put(`${url.origin}/api/lessons/${project.lessons[0].id}/learned`, {
      headers, data: { learned: true },
    });
    expect(learned.ok()).toBeTruthy();
    const advanced = await page.request.post(`${url.origin}/api/demo/next-day`, { headers });
    const { today } = await advanced.json();
    const displayDate = today.replaceAll('-', '.');
    const weekday = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][new Date(`${today}T00:00:00Z`).getUTCDay()];
    const originalQueue = await (await page.request.get(`${url.origin}/api/reviews`, { headers })).json();

    await page.getByRole('link', { name: '查看课程表', exact: true }).click();
    await expect(page.getByRole('heading', { name: '今日任务', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: '周视图', exact: true })).toHaveAttribute('aria-pressed', 'true');
    const day = page.getByRole('article', { name: `${displayDate} ${weekday}`, exact: true });
    await expect(day.getByText(`原到期日 ${displayDate}`, { exact: false })).toHaveCount(2);

    await page.getByRole('button', { name: '每周学习时间', exact: true }).click();
    const weekly = page.getByRole('dialog', { name: '每周学习时间', exact: true });
    await weekly.getByLabel(`${weekday}第 1 段开始时间`, { exact: true }).fill('10:00');
    await weekly.getByLabel(`${weekday}第 1 段结束时间`, { exact: true }).fill('10:10');
    await weekly.getByRole('button', { name: '保存时段', exact: true }).click();
    await expect(weekly).toBeHidden();
    await expect(day.getByText('预算 10 分钟', { exact: false })).toBeVisible();

    await day.getByRole('button', { name: '调整当天时段' }).click();
    const override = page.getByRole('dialog', { name: '调整当天时段', exact: true });
    await override.getByLabel(/第 1 段结束时间/).fill('10:05');
    await override.getByRole('button', { name: '保存时段', exact: true }).click();
    await expect(override).toBeHidden();
    await expect(day.getByText('当日调整', { exact: true })).toBeVisible();
    await expect(day.getByText('预算 5 分钟', { exact: false })).toBeVisible();
    await expect(day.getByText(`原到期日 ${displayDate}`, { exact: false })).toHaveCount(1);
    await expect(page.getByText(`原到期日 ${displayDate}`, { exact: false })).toHaveCount(2);

    await page.reload();
    await expect(day.getByText('预算 5 分钟', { exact: false })).toBeVisible();
    await page.setViewportSize({ width: 390, height: 780 });
    await day.getByRole('button', { name: '查看当天' }).click();
    await expect(page.getByRole('button', { name: '日视图' })).toHaveAttribute('aria-pressed', 'true');
    await expect(page.getByRole('article')).toHaveCount(1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);

    await day.getByRole('button', { name: '调整当天时段' }).click();
    await override.getByRole('button', { name: '恢复每周时段' }).click();
    await expect(day.getByText('预算 10 分钟', { exact: false })).toBeVisible();
    await expect(day.getByText(`原到期日 ${displayDate}`, { exact: false })).toHaveCount(2);
    const queueAfterChanges = await (await page.request.get(`${url.origin}/api/reviews`, { headers })).json();
    expect(queueAfterChanges).toEqual(originalQueue);
    await day.getByRole('link', { name: '区分行与列', exact: true }).click();
    await expect(page.getByRole('heading', { name: '区分行与列', exact: true })).toBeVisible();
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

test('dragging a review previews the reflow and a confirmed lock survives restart', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-locked-plan-'));
  let service = start(folder);
  try {
    const url = new URL(await service.url);
    const token = new URLSearchParams(url.hash.split('?')[1]).get('session');
    const headers = { Authorization: `Bearer ${token}` };
    await page.goto(url.href);
    const created = await page.request.post(`${url.origin}/api/projects`, { headers, data: {
      id: '00000000-0000-4000-8000-000000000020', topic: 'SQLite 入门', outcome: '能安排读课与复习时间',
    } });
    expect(created.status()).toBe(201);
    const project = await created.json();
    await expect.poll(async () => {
      const response = await page.request.get(`${url.origin}/api/projects/${project.id}`, { headers });
      return (await response.json()).task.status;
    }).toBe('ready');
    const learned = await page.request.put(`${url.origin}/api/lessons/${project.lessons[0].id}/learned`, {
      headers, data: { learned: true },
    });
    expect(learned.ok()).toBeTruthy();
    const advanced = await page.request.post(`${url.origin}/api/demo/next-day`, { headers });
    expect(advanced.ok()).toBeTruthy();
    const { today } = await advanced.json();
    const next = new Date(`${today}T00:00:00Z`);
    next.setUTCDate(next.getUTCDate() + 1);
    const tomorrow = next.toISOString().slice(0, 10);
    const todayLabel = today.replaceAll('-', '.');
    const tomorrowLabel = tomorrow.replaceAll('-', '.');

    await page.getByRole('link', { name: '查看课程表', exact: true }).click();
    const first = page.getByRole('article', { name: new RegExp(`^${todayLabel}`) });
    const second = page.getByRole('article', { name: new RegExp(`^${tomorrowLabel}`) });
    const draggable = first.getByRole('button', { name: '改期 区分行与列', exact: true });
    const moveDialog = page.getByRole('dialog', { name: '调整排期', exact: true });
    const dueText = `原到期日 ${todayLabel}`;
    await expect(first.getByText(dueText, { exact: false })).toHaveCount(2);

    await draggable.dragTo(second.getByRole('heading'), { targetPosition: { x: 12, y: 10 } });
    await expect(moveDialog).toBeVisible();
    await expect(moveDialog.getByLabel('目标日期')).toHaveValue(tomorrow);
    await expect(moveDialog.getByText(/项自动任务会调整/)).toBeVisible();
    await expect(moveDialog.getByRole('button', { name: '确认并锁定' })).toBeEnabled();
    await expect(second.getByText('已锁定', { exact: true })).toHaveCount(0);
    await moveDialog.getByRole('button', { name: '取消', exact: true }).click();
    await expect(first.getByRole('link', { name: '区分行与列', exact: true })).toBeVisible();

    await draggable.focus();
    await page.keyboard.press('Enter');
    await moveDialog.getByLabel('目标日期').fill(tomorrow);
    await moveDialog.getByLabel('开始时间').fill('19:00');
    await moveDialog.getByRole('button', { name: '预览改期' }).click();
    await moveDialog.getByRole('button', { name: '确认并锁定' }).click();
    await expect(second.getByText('已锁定', { exact: true })).toBeVisible();
    await expect(first.getByText(dueText, { exact: false })).toHaveCount(1);
    await expect(second.getByText(dueText, { exact: false })).toHaveCount(1);

    await first.getByRole('button', { name: '改期 选择可唯一定位的主键', exact: true }).click();
    await moveDialog.getByLabel('目标日期').fill(tomorrow);
    await moveDialog.getByLabel('开始时间').fill('19:00');
    await moveDialog.getByRole('button', { name: '预览改期' }).click();
    await expect(moveDialog.getByRole('alert')).toContainText('与「区分行与列」的锁定时段重叠。');
    await expect(moveDialog.getByRole('button', { name: '确认并锁定' })).toHaveCount(0);
    await moveDialog.getByRole('button', { name: '取消' }).click();

    await second.getByRole('button', { name: '调整当天时段' }).click();
    const hours = page.getByRole('dialog', { name: '调整当天时段', exact: true });
    await hours.getByLabel(/第 1 段开始时间/).fill('19:10');
    await hours.getByRole('button', { name: '保存时段' }).click();
    await expect(hours.getByRole('alert')).toContainText('请先改期或解除锁定');
    await hours.getByRole('button', { name: '取消' }).click();

    await stop(service.child);
    service = start(folder);
    await page.goto(await service.url);
    await page.getByRole('link', { name: '查看课程表', exact: true }).click();
    await expect(second.getByText('已锁定', { exact: true })).toBeVisible();
    await expect(second.getByText(dueText, { exact: false })).toHaveCount(1);
    await second.getByRole('button', { name: '解除锁定 区分行与列', exact: true }).click();
    const release = page.getByRole('dialog', { name: '解除锁定', exact: true });
    await release.getByRole('button', { name: '确认解除锁定', exact: true }).click();
    await expect(second.getByText('已锁定', { exact: true })).toHaveCount(0);
    await expect(first.getByText(dueText, { exact: false })).toHaveCount(2);
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

test('study report locates a missed course booking and refreshes its forecast after release', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-prediction-lock-'));
  const service = start(folder);
  try {
    const url = new URL(await service.url);
    const token = new URLSearchParams(url.hash.split('?')[1]).get('session');
    const headers = { Authorization: 'Bearer ' + token };
    async function request(path: string, method = 'GET', data?: object) {
      const response = await page.request.fetch(url.origin + '/api' + path, { headers, method, data });
      expect(response.ok(), await response.text()).toBeTruthy();
      return response.json();
    }
    await page.setViewportSize({ width: 390, height: 820 });
    await page.goto(url.href);
    const project = await request('/projects', 'POST', {
      id: '00000000-0000-4000-8000-000000000261', topic: 'SQLite 入门', outcome: '修正一次错过的课程预约',
    });
    await expect.poll(async () => (await request('/projects/' + project.id)).task.status, { timeout: 12000 }).toBe('ready');
    const plan = await request('/study-plan');
    const task = plan.days[0].tasks[0];
    const requestMove = { taskId: task.id, placement: { date: plan.today, start: task.start } };
    const preview = await request('/study-plan/moves/preview', 'POST', requestMove);
    expect(preview.confirmation).toBeTruthy();
    await request('/study-plan/moves/confirm', 'POST', { ...requestMove, confirmation: preview.confirmation });
    await request('/demo/next-day', 'POST');
    await page.goto(new URL('/#/study-report?period=month', url).href);

    await expect(page.getByText('先处理锁定任务', { exact: true })).toBeVisible();
    await expect(page.getByText(/已过计划日期/)).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
    await page.getByRole('link', { name: '查看预约 ' + task.title, exact: true }).click();
    const pending = page.locator('[id="study-lock-' + task.id + '"]');
    await expect(pending).toBeFocused();
    await expect(pending.getByText('已过计划日期', { exact: true })).toBeVisible();
    const release = pending.getByRole('button', { name: '解除锁定 ' + task.title, exact: true });
    await release.click();
    const dialog = page.getByRole('dialog', { name: '解除锁定', exact: true });
    await dialog.getByRole('button', { name: '取消', exact: true }).click();
    await expect(pending).toBeVisible();
    expect((await request('/study-plan')).pendingLocks[0].id).toBe(task.id);

    await release.click();
    await dialog.getByRole('button', { name: '确认解除锁定', exact: true }).click();
    await expect(page.getByRole('status').filter({ hasText: '已解除锁定。' })).toBeVisible();
    await page.goBack();
    await expect(page.getByRole('heading', { name: '课程完成预估', exact: true })).toBeVisible();
    expect(new URL(page.url()).hash).toBe('#/study-report?period=month');
    await expect(page.getByText(/使用默认估时/)).toBeVisible();
    await expect(page.getByRole('link', { name: '查看预约 ' + task.title, exact: true })).toHaveCount(0);
  } finally {
    await stop(service.child);
    const verified = realpathSync(folder);
    if (isAbsolute(relative(realpathSync(tmpdir()), verified)) || dirname(verified) !== realpathSync(tmpdir())
        || !verified.split(/[\\/]/).at(-1)?.startsWith('zhixue-prediction-lock-')) {
      throw new Error('Unexpected prediction fixture directory');
    }
    rmSync(verified, { recursive: true, force: true });
  }
});

test('study report separates plan coverage and compares reading time with date detail', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-report-comparison-'));
  const clockFile = join(folder, 'clock.txt');
  const date = (instant: string) => writeFileSync(clockFile, instant);
  date('2027-01-25T08:00:00');
  const service = start(folder, clockFile);
  try {
    const url = new URL(await service.url);
    const token = new URLSearchParams(url.hash.split('?')[1]).get('session');
    const headers = { Authorization: 'Bearer ' + token };
    async function request(path: string, method = 'GET', data?: object) {
      const response = await page.request.fetch(url.origin + '/api' + path, { headers, method, data });
      expect(response.ok(), await response.text()).toBeTruthy();
      return response.json();
    }
    const project = await request('/projects', 'POST', {
      id: '00000000-0000-4000-8000-000000000279', topic: 'SQLite 入门', outcome: '复盘有计划与无计划日期的学习时长',
    });
    await expect.poll(async () => (await request('/projects/' + project.id)).task.status, { timeout: 12000 }).toBe('ready');

    async function readLesson(index: number, from: string, until: string) {
      date(from);
      const lessonId = project.lessons[index].id;
      const started = await request('/timer', 'POST', { action: 'start', lessonId });
      date(until);
      await request('/timer', 'POST', { action: 'finish', timerId: started.active.id });
      await request('/lessons/' + lessonId + '/learned', 'PUT', { learned: true });
      return started.active.id;
    }

    await readLesson(0, '2027-01-25T08:00:00', '2027-01-25T08:08:00');
    await readLesson(1, '2027-01-27T08:00:00', '2027-01-27T08:20:00');
    const current = await readLesson(2, '2027-02-01T08:00:00', '2027-02-01T08:06:00');
    date('2027-02-01T15:55:00');
    const note = await request('/timer', 'POST', { action: 'start', category: 'notes', lessonId: project.lessons[2].id });
    date('2027-02-01T16:05:00');
    await request('/timer', 'POST', { action: 'finish', timerId: note.active.id });

    await page.setViewportSize({ width: 390, height: 820 });
    await page.goto(url.href);
    await page.getByRole('link', { name: '学习报告', exact: true }).click();
    const comparison = page.locator('.report-columns section').filter({ has: page.getByRole('heading', { name: '计划与实际', exact: true }) });
    const metric = (label: string) => comparison.getByText(label, { exact: true }).locator('..').locator('dd');
    await expect(comparison.getByText('所选日期中 1 / 2 天有计划记录。')).toBeVisible();
    await expect(metric('有计划日期的确认时长')).toHaveText('11 分钟');
    await expect(metric('无计划记录日期的确认时长')).toHaveText('5 分钟');
    await expect(metric('上一周期（2027.01.25 至 2027.01.26） · 1 个知识点')).toHaveText('8 分钟');
    await expect(metric('较上一周期')).toHaveText('缩短 2 分钟');
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);

    await page.getByRole('link', { name: '查看 2027.02.02 日报', exact: true }).click();
    await expect(page).toHaveURL(/#\/study-report\?period=day&on=2027-02-02$/);
    await expect(comparison.getByText('这个时段还没有保存的计划估时。')).toBeVisible();
    await expect(metric('无计划记录日期的确认时长')).toHaveText('5 分钟');
    await expect(page.getByRole('link', { name: '查看 2027.02.02 日报', exact: true })).toHaveCount(0);
    await page.goBack();
    await expect(page.getByRole('button', { name: '周', exact: true })).toHaveAttribute('aria-pressed', 'true');
    await expect(metric('较上一周期')).toHaveText('缩短 2 分钟');

    const correction = '/study-report/timers/' + current + '/correction';
    const fields = { sourceOn: '2027-02-01', reportOn: '2027-02-01', category: 'learning' };
    await request(correction, 'PUT', { ...fields, seconds: 600, baseRevision: 0 });
    await page.reload();
    await expect(metric('较上一周期')).toHaveText('增加 2 分钟');
    await request(correction, 'PUT', { ...fields, seconds: 480, baseRevision: 1 });
    await page.reload();
    await expect(metric('较上一周期')).toHaveText('持平');
    await page.getByRole('button', { name: '月', exact: true }).click();
    await expect(metric('上一周期（2027.01.01 至 2027.01.02） · 0 个知识点')).toHaveText('暂无可比计时');
    await expect(metric('较上一周期')).toHaveCount(0);
  } finally {
    await stop(service.child);
    const verified = realpathSync(folder);
    if (dirname(verified) !== realpathSync(tmpdir()) || !verified.split(/[\\/]/).at(-1)?.startsWith('zhixue-report-comparison-')) {
      throw new Error('Unexpected report fixture directory');
    }
    rmSync(verified, { recursive: true, force: true });
  }
});

test('study report previews correction history and safely reuses the original after a conflict', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-timer-history-'));
  const clockFile = join(folder, 'clock.txt');
  writeFileSync(clockFile, '2027-02-01T08:00:00');
  const service = start(folder, clockFile);
  try {
    const url = new URL(await service.url);
    const token = new URLSearchParams(url.hash.split('?')[1]).get('session');
    const headers = { Authorization: 'Bearer ' + token };
    async function request(path: string, method = 'GET', data?: object) {
      const response = await page.request.fetch(url.origin + '/api' + path, { headers, method, data });
      expect(response.ok(), await response.text()).toBeTruthy();
      return response.json();
    }
    const project = await request('/projects', 'POST', {
      id: '00000000-0000-4000-8000-000000000289', topic: 'SQLite 入门', outcome: '核对计时校正的各个版本',
    });
    await expect.poll(async () => (await request('/projects/' + project.id)).task.status, { timeout: 12000 }).toBe('ready');
    const lesson = project.lessons[0];
    const startTimer = await request('/timer', 'POST', { action: 'start', lessonId: lesson.id, category: 'learning' });
    const timerId = startTimer.active.id;
    writeFileSync(clockFile, '2027-02-01T08:02:00');
    await request('/timer', 'POST', { action: 'finish', timerId });
    const path = '/study-report/timers/' + timerId;
    const day = { sourceOn: '2027-02-01', reportOn: '2027-02-01' };
    await request(path + '/correction', 'PUT', { ...day, seconds: 60, category: 'notes', baseRevision: 0 });
    await request(path + '/correction', 'PUT', { ...day, seconds: 90, category: 'assistant', baseRevision: 1 });

    await page.setViewportSize({ width: 390, height: 820 });
    await page.goto(url.href);
    await page.getByRole('link', { name: '学习报告', exact: true }).click();
    const records = page.getByText('已确认计时记录（1）', { exact: true });
    await records.click();
    const edit = page.getByRole('button', { name: '校正 ' + lesson.title + ' 2027-02-01', exact: true });
    const timerEditor = page.getByRole('dialog', { name: '校正计时', exact: true });
    const openHistory = () => timerEditor.getByRole('button', { name: '查看校正历史', exact: true }).click();
    const version = timerEditor.getByRole('combobox', { name: '校正版本' });
    const load = () => timerEditor.getByRole('button', { name: '载入此版本', exact: true }).click();
    const commit = () => timerEditor.getByRole('button', { name: '保存校正', exact: true }).click();
    await edit.click();
    await openHistory();
    await expect(version).toHaveValue('2');
    await version.selectOption('1');
    await expect(timerEditor.getByText('2027.02.01 · 笔记 · 1 分钟', { exact: true })).toBeVisible();
    await load();
    await expect(timerEditor.getByLabel('有效分钟')).toBeFocused();
    await expect(timerEditor.getByLabel('活动类别')).toHaveValue('notes');
    await timerEditor.getByRole('button', { name: '取消', exact: true }).click();
    await expect(page.getByText('1 分钟 30 秒 · 原始 2 分钟', { exact: true })).toBeVisible();

    await edit.click();
    await openHistory();
    await version.selectOption('0');
    await load();
    await expect(timerEditor.getByLabel('有效分钟')).toHaveValue('2');
    await request(path + '/correction', 'PUT', { ...day, seconds: 30, category: 'notes', baseRevision: 2 });
    await commit();
    await expect(timerEditor.getByRole('alert').filter({ hasText: '此计时已校正' })).toBeVisible();
    await expect(timerEditor.getByLabel('有效分钟')).toHaveValue('2');

    await openHistory();
    await expect(version).toHaveValue('3');
    await version.selectOption('0');
    await load();
    await commit();
    await expect(page.getByRole('status').filter({ hasText: '计时校正已保存。' })).toBeVisible();
    await expect(page.getByText('2 分钟 · 原始 2 分钟', { exact: true })).toBeVisible();
    const history = await request(path + '/corrections?sourceOn=2027-02-01');
    expect(history.versions.map((entry: { revision: number }) => entry.revision)).toEqual([4, 3, 2, 1, 0]);
    expect((await request('/timer')).lastSaved.elapsed).toBe(120);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  } finally {
    await stop(service.child);
    const verified = realpathSync(folder);
    if (dirname(verified) !== realpathSync(tmpdir()) || !verified.split(/[\\/]/).at(-1)?.startsWith('zhixue-timer-history-')) {
      throw new Error('Unexpected timer history fixture directory');
    }
    rmSync(verified, { recursive: true, force: true });
  }
});

test('study report filters long timer lists from activity totals without changing totals', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-timer-filters-'));
  const clockFile = join(folder, 'clock.txt');
  const clock = new Date('2027-02-01T08:00:00Z');
  const writeClock = () => writeFileSync(clockFile, clock.toISOString().slice(0, 19));
  writeClock();
  const service = start(folder, clockFile);
  try {
    const url = new URL(await service.url);
    const token = new URLSearchParams(url.hash.split('?')[1]).get('session');
    const headers = { Authorization: 'Bearer ' + token };
    async function request(path: string, method = 'GET', data?: object) {
      const response = await page.request.fetch(url.origin + '/api' + path, { headers, method, data });
      expect(response.ok(), await response.text()).toBeTruthy();
      return response.json();
    }

    const topics = ['SQLite 入门', 'SQLite 索引回顾'];
    const projects = [];
    for (const [index, topic] of topics.entries()) {
      const id = index === 0 ? '00000000-0000-4000-8000-000000000297' : '00000000-0000-4000-8000-000000000298';
      const project = await request('/projects', 'POST', { id, topic, outcome: '查找学习计时明细' });
      projects.push(project);
      await expect.poll(async () => (await request('/projects/' + project.id)).task.status, { timeout: 12000 }).toBe('ready');
    }
    async function record(lessonId: string, category: 'notes' | 'assistant') {
      const timer = await request('/timer', 'POST', { action: 'start', lessonId, category });
      clock.setUTCSeconds(clock.getUTCSeconds() + 60);
      writeClock();
      await request('/timer', 'POST', { action: 'finish', timerId: timer.active.id });
    }
    for (let index = 0; index < 22; index++) await record(projects[0].lessons[0].id, 'notes');
    await record(projects[1].lessons[0].id, 'assistant');
    execFileSync(python, ['-c', [
      'import sqlite3, sys',
      'from contextlib import closing',
      'with closing(sqlite3.connect(sys.argv[1])) as db, db:',
      '    db.execute("INSERT INTO timers (id, lesson_id, status, elapsed, updated_at) VALUES (?, ?, ?, ?, ?)",',
      '               ("00000000-0000-4000-8000-000000000299", sys.argv[2], "finished", 90, "2027-02-01 08:30:00"))',
    ].join('\n'), join(folder, 'zhixue.sqlite3'), projects[1].lessons[0].id], { windowsHide: true });

    await page.setViewportSize({ width: 390, height: 820 });
    await page.goto(new URL('/#/study-report?period=month&session=' + token, url).href);
    const overview = page.locator('.report-summary section').filter({ has: page.getByRole('heading', { name: '学习时长', exact: true }) });
    await expect(overview.getByText('23 分钟', { exact: true })).toBeVisible();
    await overview.getByRole('button', { name: '查看笔记计时记录', exact: true }).click();
    const records = page.getByRole('region', { name: '计时记录', exact: true });
    const activity = records.getByRole('combobox', { name: /活动类别/ });
    const search = records.getByRole('searchbox', { name: '搜索课程或项目' });
    const confirmed = records.locator('details').first();
    const rows = confirmed.getByRole('listitem');
    await expect(activity).toHaveValue('notes');
    await expect(activity).toBeFocused();
    await expect(confirmed.getByText('已确认计时记录（22）', { exact: true })).toBeVisible();
    await expect(rows).toHaveCount(20);
    await confirmed.getByRole('button', { name: '显示更多计时记录', exact: true }).click();
    await expect(rows).toHaveCount(22);

    await search.fill('索引回顾');
    await expect(confirmed.getByText('没有符合筛选条件的计时记录。')).toBeVisible();
    await activity.selectOption('assistant');
    await expect(rows).toHaveCount(1);
    await expect(rows.first().getByText(/SQLite 索引回顾/)).toBeVisible();
    await page.reload();
    await expect(activity).toHaveValue('assistant');
    await expect(search).toHaveValue('索引回顾');
    await expect(rows).toHaveCount(1);
    await rows.first().getByRole('button', { name: /^校正 / }).click();
    const editor = page.getByRole('dialog', { name: '校正计时', exact: true });
    await expect(editor.getByText(/SQLite 索引回顾 · 原始计时 1 分钟/)).toBeVisible();
    await editor.getByRole('button', { name: '取消', exact: true }).click();
    await activity.selectOption('unclassified');
    await expect(confirmed.getByText('已确认计时记录（0）')).toBeVisible();
    const undated = records.locator('details').nth(1);
    await expect(undated.getByText('日期待补录的旧计时（1）', { exact: true })).toBeVisible();
    const legacy = undated.getByRole('listitem');
    await expect(legacy).toHaveCount(1);
    await expect(legacy.getByText(/SQLite 索引回顾 · 日期待补录/)).toBeVisible();
    await legacy.getByRole('button', { name: /旧计时$/ }).click();
    await expect(editor.getByLabel('归入日期')).toHaveValue('');
    await editor.getByRole('button', { name: '取消', exact: true }).click();
    await records.getByRole('button', { name: '清除筛选', exact: true }).click();
    await expect(activity).toHaveValue('');
    await expect(confirmed.getByText('已确认计时记录（23）', { exact: true })).toBeVisible();
    await expect(rows).toHaveCount(20);
    await expect(overview.getByText('23 分钟', { exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  } finally {
    await stop(service.child);
    const verified = realpathSync(folder);
    if (dirname(verified) !== realpathSync(tmpdir()) || !verified.split(/[\\/]/).at(-1)?.startsWith('zhixue-timer-filters-')) {
      throw new Error('Unexpected timer filters fixture directory');
    }
    rmSync(verified, { recursive: true, force: true });
  }
});

test('study report connects activity timers with daily totals, corrections, and formal review outcomes', async ({ page }, info) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-study-report-'));
  const service = start(folder);
  try {
    const url = new URL(await service.url);
    const token = new URLSearchParams(url.hash.split('?')[1]).get('session');
    const headers = { Authorization: `Bearer ${token}` };
    await page.goto(url.href);
    const response = await page.request.post(`${url.origin}/api/projects`, { headers, data: {
      id: '00000000-0000-4000-8000-000000000021', topic: 'SQLite 入门', outcome: '按活动和日期核对学习过程',
    } });
    expect(response.status()).toBe(201);
    const project = await response.json();
    const lesson = project.lessons[0].id;
    await expect.poll(async () => {
      const row = await page.request.get(`${url.origin}/api/projects/${project.id}`, { headers });
      return (await row.json()).task.status;
    }).toBe('ready');
    await page.evaluate(id => { window.location.hash = `#/lessons/${id}`; }, lesson);
    await page.getByRole('button', { name: '笔记', exact: true }).click();
    await page.getByRole('button', { name: '开始计时', exact: true }).click();
    await expect(page.getByText('计时中', { exact: false })).toBeVisible();
    await expect.poll(async () => {
      const row = await page.request.get(`${url.origin}/api/timer`, { headers });
      const active = (await row.json()).active;
      return active?.category === 'notes' ? active.elapsed : 0;
    }).toBeGreaterThanOrEqual(1);
    const learned = page.getByRole('checkbox', { name: '已学完', exact: true });
    await learned.click();
    await expect(learned).toBeChecked();
    await expect(page.getByRole('button', { name: '结束计时', exact: true })).toBeVisible();
    await page.getByRole('button', { name: '结束计时', exact: true }).click();
    await expect(page.getByRole('link', { name: '返回计时课程' })).toHaveCount(0);

    await page.getByRole('link', { name: '今日复习', exact: true }).click();
    await page.getByRole('button', { name: '推进试用日期一天' }).click();
    await page.getByRole('link', { name: /区分行与列/ }).click();
    await page.getByRole('button', { name: '开始计时', exact: true }).click();
    await expect(page.getByRole('link', { name: '返回计时复习' })).toBeVisible();
    await expect.poll(async () => {
      const row = await page.request.get(`${url.origin}/api/timer`, { headers });
      const active = (await row.json()).active;
      return active?.reviewKey === 'rows-columns' ? active.elapsed : 0;
    }).toBeGreaterThanOrEqual(1);
    await page.getByRole('button', { name: '给点提示' }).click();
    await page.getByRole('radio', { name: '一行', exact: true }).first().check();
    await page.getByRole('radio', { name: '一列', exact: true }).last().check();
    await page.getByRole('button', { name: '提交本组答案' }).click();
    await expect(page.getByRole('heading', { name: '已完成辅助练习' })).toBeVisible();
    await page.getByRole('button', { name: '结束计时', exact: true }).click();
    await expect(page.getByRole('link', { name: '返回计时复习' })).toHaveCount(0);

    await page.getByRole('link', { name: '学习报告', exact: true }).click();
    await expect(page.getByRole('button', { name: '周', exact: true })).toHaveAttribute('aria-pressed', 'true');
    await page.getByRole('button', { name: '月', exact: true }).click();
    await expect(page.getByRole('heading', { name: '课程完成预估' })).toBeVisible();
    await page.getByRole('button', { name: '日', exact: true }).click();
    const review = page.locator('section').filter({ has: page.getByRole('heading', { name: '正式复习表现', exact: true }) })
      .filter({ hasNot: page.getByRole('heading', { name: '学习报告', exact: true }) });
    for (const [label, count] of [['辅助作答', '1'], ['独立通过', '0'], ['未评分', '0']]) {
      const category = review.getByRole('button', { name: '查看' + label + '的正式复习记录' });
      await expect(category.locator('../..').locator('dd')).toHaveText(count);
    }
    await expect(page.getByRole('link', { name: '复习课程 区分行与列' })).toBeVisible();
    const viewport = page.viewportSize()!;
    await page.setViewportSize({ width: 390, height: 800 });
    await review.getByRole('button', { name: '查看辅助作答的正式复习记录' }).click();
    const records = page.getByRole('region', { name: '正式复习记录', exact: true });
    const outcome = records.getByRole('combobox', { name: '复习结果' });
    await expect(outcome).toHaveValue('hinted');
    await expect(outcome).toBeFocused();
    const row = records.getByRole('listitem');
    await expect(row).toHaveCount(1);
    await expect(row).toContainText('区分行与列');
    await expect(row).toContainText('第 1 轮');
    await expect(row.getByRole('link', { name: '理解表、行和列' })).toHaveAttribute('href', '#/lessons/' + lesson);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
    await page.reload();
    await expect(outcome).toBeVisible();
    await expect(outcome).toHaveValue('hinted');
    await expect(row).toHaveCount(1);
    await records.screenshot({ path: info.outputPath('正式复习记录.png') });
    await outcome.selectOption('ungraded');
    await expect(records.getByText('本周期没有符合筛选条件的正式复习记录。')).toBeVisible();
    await outcome.selectOption('');
    await expect(row).toHaveCount(1);
    await outcome.selectOption('hinted');
    await page.setViewportSize(viewport);

    await page.getByRole('button', { name: '上一周期' }).click();
    await expect(outcome).toHaveValue('hinted');
    await expect(records.getByText('本周期没有符合筛选条件的正式复习记录。')).toBeVisible();
    await page.getByText('已确认计时记录（1）', { exact: true }).click();
    await expect(page.getByText(/· 笔记$/)).toBeVisible();
    await page.getByRole('button', { name: `校正 ${project.lessons[0].title}` }).click();
    const editor = page.getByRole('dialog', { name: '校正计时' });
    await expect(editor.getByText(/原始计时/)).toBeVisible();
    await editor.getByLabel('有效分钟').fill('6');
    await editor.getByLabel('有效秒数').fill('0');
    await editor.getByLabel('活动类别').selectOption('learning');
    await editor.getByRole('button', { name: '保存校正' }).click();
    await expect(page.getByRole('status').filter({ hasText: '计时校正已保存。' })).toBeVisible();
    await expect(page.getByText(/6 分钟 · 原始/)).toBeVisible();
    await expect(page.getByText('少 24 分钟', { exact: true })).toBeVisible();
    await page.setViewportSize({ width: 390, height: 800 });
    await page.reload();
    await expect(page.getByRole('heading', { name: '学习时长' })).toBeVisible();
    await page.getByText('已确认计时记录（1）', { exact: true }).click();
    await expect(page.getByText(/6 分钟 · 原始/)).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

test('local reminders wait for foreground input, respect settings, and keep the original learning dates', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-reminders-'));
  let service = start(folder);
  const daily = page.getByRole('region', { name: '每日学习提醒', exact: true });
  try {
    let url = new URL(await service.url);
    const auth = (address: URL) => ({
      Authorization: `Bearer ${new URLSearchParams(address.hash.split('?')[1]).get('session')}`,
    });
    let headers = auth(url);
    let checks = 0;
    page.on('request', request => { if (request.url().endsWith('/api/reminders/check')) checks += 1; });
    const opened = page.waitForResponse(response => response.url().endsWith('/api/reminders/check'));
    await page.goto(url.href);
    await opened;

    const created = await page.request.post(`${url.origin}/api/projects`, { headers, data: {
      id: '00000000-0000-4000-8000-000000000022', topic: 'SQLite 入门', outcome: '核对前台复习提醒',
    } });
    expect(created.status()).toBe(201);
    const project = await created.json();
    await expect.poll(async () => {
      const item = await page.request.get(`${url.origin}/api/projects/${project.id}`, { headers });
      return (await item.json()).task.status;
    }).toBe('ready');
    const learned = await page.request.put(`${url.origin}/api/lessons/${project.lessons[0].id}/learned`, {
      headers, data: { learned: true },
    });
    expect(learned.ok()).toBeTruthy();
    const advanced = await page.request.post(`${url.origin}/api/demo/next-day`, { headers });
    const { today: due } = await advanced.json();
    const beforeIdle = checks;
    await page.clock.install();
    await page.clock.fastForward(60_000);
    expect(checks).toBe(beforeIdle);
    await expect(daily).toHaveCount(0);
    const delivered: { id: string; delivery: string }[] = [];
    await page.route('**/api/reminders/check', async route => {
      const response = await route.fetch();
      const payload = await response.json();
      if (payload.reminders.length && delivered.length < 2) {
        delivered.push({ id: payload.reminders[0].id, delivery: route.request().postDataJSON().delivery });
        if (delivered.length === 1) return route.abort('failed');
      }
      await route.fulfill({ response });
    });
    await page.keyboard.press('Shift');
    await expect(daily.getByText('2 项复习到期')).toBeVisible({ timeout: 12000 });
    expect(delivered).toHaveLength(2);
    expect(delivered[0].delivery).toMatch(/^[a-f0-9-]{36}$/);
    expect(delivered[1]).toEqual(delivered[0]);
    await page.unroute('**/api/reminders/check');
    await daily.getByRole('button', { name: '明天', exact: true }).click();
    await expect(daily).toHaveCount(0);

    await stop(service.child);
    service = start(folder);
    url = new URL(await service.url);
    headers = auth(url);
    const reopened = page.waitForResponse(response => response.url().endsWith('/api/reminders/check'));
    await page.goto(url.href);
    expect((await (await reopened).json()).reminders).toEqual([]);
    await page.getByRole('link', { name: '今日复习', exact: true }).click();
    await page.getByRole('button', { name: '推进试用日期一天' }).click();
    await expect(daily.getByText('2 项复习到期')).toBeVisible();
    await daily.getByRole('link', { name: '查看到期复习' }).click();
    await expect(page.getByRole('heading', { name: '今日复习', exact: true })).toBeVisible();
    let reviews = await page.request.get(`${url.origin}/api/reviews`, { headers });
    expect((await reviews.json()).reviews.every((review: { dueOn: string }) => review.dueOn === due)).toBeTruthy();
    const timer = await page.request.get(`${url.origin}/api/timer`, { headers });
    expect((await timer.json()).active).toBeNull();

    await page.getByRole('link', { name: '管理中心', exact: true }).click();
    await page.getByRole('checkbox', { name: '启用学习提醒' }).uncheck();
    await page.getByRole('checkbox', { name: '使用免打扰时段' }).check();
    await page.getByLabel('开始时间', { exact: true }).fill('20:00');
    await page.getByLabel('结束时间', { exact: true }).fill('07:00');
    await page.getByRole('button', { name: '保存提醒设置' }).click();
    await expect(page.getByText('提醒设置已保存。')).toBeVisible();
    await expect(daily).toHaveCount(0);

    await page.reload();
    await expect(page.getByRole('checkbox', { name: '启用学习提醒' })).not.toBeChecked();
    await expect(page.getByLabel('开始时间', { exact: true })).toHaveValue('20:00');
    await page.getByRole('link', { name: '今日复习', exact: true }).click();
    await page.getByRole('button', { name: '推进试用日期一天' }).click();
    await expect(daily).toHaveCount(0);
    reviews = await page.request.get(`${url.origin}/api/reviews`, { headers });
    expect((await reviews.json()).reviews.every((review: { dueOn: string }) => review.dueOn === due)).toBeTruthy();
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

test('a fixed lesson appears at its appointment and snoozes without starting a timer', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-fixed-reminder-'));
  const clockFile = join(folder, 'clock.txt');
  writeFileSync(clockFile, '2027-02-01T10:59:00');
  const service = start(folder, clockFile);
  try {
    const url = new URL(await service.url);
    const headers = { Authorization: `Bearer ${new URLSearchParams(url.hash.split('?')[1]).get('session')}` };
    const created = await page.request.post(`${url.origin}/api/projects`, { headers, data: {
      id: '00000000-0000-4000-8000-000000000023', topic: 'SQLite 入门', outcome: '在固定预约时段开始学习',
    } });
    expect(created.status()).toBe(201);
    const project = await created.json();
    await expect.poll(async () => {
      const item = await page.request.get(`${url.origin}/api/projects/${project.id}`, { headers });
      return (await item.json()).task.status;
    }).toBe('ready');
    const response = await page.request.get(`${url.origin}/api/study-plan`, { headers });
    const plan = await response.json();
    const task = plan.days[0].tasks[0];
    const move = { taskId: task.id, placement: { date: plan.today, start: '19:00' } };
    const preview = await page.request.post(`${url.origin}/api/study-plan/moves/preview`, { headers, data: move });
    const { confirmation } = await preview.json();
    expect(confirmation).toBeTruthy();
    const locked = await page.request.post(`${url.origin}/api/study-plan/moves/confirm`, {
      headers, data: { ...move, confirmation },
    });
    expect(locked.ok()).toBeTruthy();

    await page.clock.install({ time: new Date('2027-02-01T10:59:00Z') });
    const opened = page.waitForResponse(reply => reply.url().endsWith('/api/reminders/check'));
    await page.goto(url.href);
    expect((await (await opened).json()).nextCheckSeconds).toBe(60);
    const fixed = page.getByRole('region', { name: `固定课程提醒 ${task.title}`, exact: true });
    await expect(fixed).toHaveCount(0);

    writeFileSync(clockFile, '2027-02-01T11:00:00');
    await page.clock.fastForward(60_200);
    await expect(fixed.getByText(/2027.02.01 19:00/)).toBeVisible();
    const scheduled = page.waitForResponse(reply =>
      reply.url().endsWith('/api/reminders/check') && reply.request().postDataJSON().trigger === 'scheduled');
    await fixed.getByRole('button', { name: '30 分钟后', exact: true }).click();
    expect((await (await scheduled).json()).nextCheckSeconds).toBe(1800);
    await expect(fixed).toHaveCount(0);
    writeFileSync(clockFile, '2027-02-01T11:30:00');
    await page.clock.fastForward(1_800_200);
    await fixed.getByRole('link', { name: '打开预约课程' }).click();
    await expect(page).toHaveURL(new RegExp(`#/lessons/${task.lessonId}$`));
    const timer = await page.request.get(`${url.origin}/api/timer`, { headers });
    expect((await timer.json()).active).toBeNull();
    await fixed.getByRole('button', { name: '关闭提醒' }).click();
    await expect(fixed).toHaveCount(0);
    await page.reload();
    await expect(page.getByRole('button', { name: '开始计时', exact: true })).toBeVisible();
    await expect(fixed).toHaveCount(0);
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

test('a rebooked fixed lesson shows a fresh reminder in the same window', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-rebooked-reminder-'));
  const clockFile = join(folder, 'clock.txt');
  writeFileSync(clockFile, '2027-02-01T11:00:00');
  const service = start(folder, clockFile);
  try {
    const url = new URL(await service.url);
    const token = new URLSearchParams(url.hash.split('?')[1]).get('session');
    const headers = { Authorization: 'Bearer ' + token };
    async function request(path: string, method = 'GET', data?: object) {
      const response = await page.request.fetch(url.origin + '/api' + path, { headers, method, data });
      expect(response.ok(), await response.text()).toBeTruthy();
      return response.json();
    }
    const project = await request('/projects', 'POST', {
      id: '00000000-0000-4000-8000-000000000269', topic: 'SQLite 入门', outcome: '重新预约后获得新的固定课程提醒',
    });
    await expect.poll(async () => (await request('/projects/' + project.id)).task.status, { timeout: 12000 }).toBe('ready');
    const plan = await request('/study-plan');
    const task = plan.days[0].tasks[0];
    const movement = { taskId: task.id, placement: { date: plan.today, start: '19:00' } };
    const initial = await request('/study-plan/moves/preview', 'POST', movement);
    await request('/study-plan/moves/confirm', 'POST', { ...movement, confirmation: initial.confirmation });

    await page.clock.install({ time: new Date('2027-02-01T11:00:00Z') });
    await page.goto(url.href);
    const originalPage = await page.evaluate(() => performance.timeOrigin);
    const fixed = page.getByRole('region', { name: '固定课程提醒 ' + task.title, exact: true });
    await fixed.getByRole('button', { name: '关闭提醒', exact: true }).click();
    await expect(fixed).toHaveCount(0);
    await page.getByRole('link', { name: '今日任务', exact: true }).click();
    await page.getByRole('button', { name: '解除锁定 ' + task.title, exact: true }).click();
    await page.getByRole('button', { name: '确认解除锁定', exact: true }).click();
    await expect(page.getByText('已解除锁定。', { exact: true })).toBeVisible();
    await expect(fixed).toHaveCount(0);

    await page.getByRole('button', { name: '改期 ' + task.title, exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '调整排期', exact: true });
    await expect(dialog.getByLabel('目标日期', { exact: true })).toHaveValue(plan.today);
    await dialog.getByLabel('开始时间', { exact: true }).fill('19:00');
    await dialog.getByRole('button', { name: '预览改期', exact: true }).click();
    await dialog.getByRole('button', { name: '确认并锁定', exact: true }).click();
    await expect(fixed).toBeVisible();
    expect(await page.evaluate(() => performance.timeOrigin)).toBe(originalPage);
    await fixed.getByRole('link', { name: '打开预约课程', exact: true }).click();
    await expect(page).toHaveURL(new RegExp('#/lessons/' + task.lessonId + '$'));
    expect((await request('/timer')).active).toBeNull();
  } finally {
    await stop(service.child);
    const verified = realpathSync(folder);
    if (dirname(verified) !== realpathSync(tmpdir()) || !verified.split(/[\\/]/).at(-1)?.startsWith('zhixue-rebooked-reminder-')) {
      throw new Error('Unexpected rebooking fixture directory');
    }
    rmSync(verified, { recursive: true, force: true });
  }
});

test('the phone opens a fixed lesson and waits for foreground interaction after a reminder crosses days', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-phone-notice-'));
  const clockFile = join(folder, 'clock.txt');
  writeFileSync(clockFile, '2027-02-01T10:59:00');
  const service = start(folder, clockFile);
  const endpoint = '/api/mobile/reminders/check';
  const daily = page.getByRole('region', { name: '每日学习提醒', exact: true });
  try {
    const address = new URL(await service.url);
    const headers = { Authorization: 'Bearer ' + new URLSearchParams(address.hash.split('?')[1]).get('session') };
    const api = async (path: string, method = 'GET', data?: object) => {
      const response = await page.request.fetch(address.origin + '/api' + path, { headers, method, data });
      expect(response.ok(), await response.text()).toBe(true);
      return response.json();
    };
    const project = await api('/projects', 'POST', {
      id: '00000000-0000-4000-8000-000000000027', topic: 'SQLite 入门', outcome: '验证手机预约与跨日提醒',
    });
    await expect.poll(async () => (await api('/projects/' + project.id)).task.status).toBe('ready');
    const plan = await api('/study-plan');
    const task = plan.days[0].tasks[0];
    const move = { taskId: task.id, placement: { date: plan.today, start: '19:00' } };
    const { confirmation } = await api('/study-plan/moves/preview', 'POST', move);
    await api('/study-plan/moves/confirm', 'POST', { ...move, confirmation });

    let requests = 0;
    page.on('request', req => { if (req.url().endsWith(endpoint)) requests++; });
    await page.clock.install({ time: new Date('2027-02-01T10:59:00Z') });
    const opened = page.waitForResponse(reply => reply.url().endsWith(endpoint));
    await page.goto(new URL('/mobile/' + address.hash, address).href);
    expect((await (await opened).json()).nextCheckSeconds).toBe(60);
    const fixed = page.getByRole('region', { name: '固定课程提醒 ' + task.title, exact: true });
    writeFileSync(clockFile, '2027-02-01T11:00:00');
    await page.clock.fastForward(60_200);
    await fixed.getByRole('button', { name: '打开预约课程' }).click();
    await expect(page.getByRole('heading', { name: task.title, level: 2, exact: true })).toBeVisible();
    expect((await api('/timer')).active).toBeNull();
    await fixed.getByRole('button', { name: '关闭提醒' }).click();
    await expect(fixed).toHaveCount(0);

    await api('/lessons/' + task.lessonId + '/learned', 'PUT', { learned: true });
    writeFileSync(clockFile, '2027-02-02T11:00:00');
    await page.clock.fastForward(6000);
    await page.keyboard.press('Shift');
    await expect(daily.getByText(/项复习到期/)).toBeVisible();
    await daily.getByRole('button', { name: '明天', exact: true }).click();
    await expect(daily).toHaveCount(0);
    writeFileSync(clockFile, '2027-02-03T11:00:00');
    const rollover = page.waitForResponse(reply => reply.url().endsWith(endpoint)
      && reply.request().postDataJSON().trigger === 'scheduled');
    await page.clock.fastForward(12_200);
    const result = await (await rollover).json();
    expect(result.reminders).toEqual([]);
    expect(result.day).toBe('2027-02-03');
    const idleRequests = requests;
    await page.clock.fastForward(12_200);
    expect(requests).toBe(idleRequests);
    await expect(daily).toHaveCount(0);
    await page.keyboard.press('Shift');
    await expect(daily.getByText(/项复习到期/)).toBeVisible();
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

test('daily model quota offers a manual resume and the worker finishes the saved phase', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-budget-recovery-'));
  const evidence = '固定后的活字应保留在版框中，再检查字行是否平整。';
  const wire: string[] = [];
  const provider = createServer((request, response) => {
    let body = '';
    request.setEncoding('utf8');
    request.on('data', chunk => { body += chunk; });
    request.on('end', () => {
      const { model } = JSON.parse(body);
      wire.push(model);
      const output = model === 'budget-reviewer' ? { sections: [
        { index: 0, verdict: 'supported', reason: '定位到原始说明。', citations: [{ source: 'S1', quote: evidence }], dependsOn: [] },
      ] } : { sections: [{ title: '固定版框再检查', text: evidence, checks: ['版框的用途'] }] };
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({ choices: [{ message: { content: JSON.stringify(output) } }] }));
    });
  });
  provider.listen(0, '127.0.0.1');
  await once(provider, 'listening');
  const binding = provider.address();
  if (!binding || typeof binding === 'string') throw new Error('Expected a loopback port');
  const service = start(folder);
  try {
    const url = new URL(await service.url);
    const headers = { Authorization: `Bearer ${new URLSearchParams(url.hash.split('?')[1]).get('session')}` };
    async function post(path: string, data: object) {
      const reply = await page.request.post(`${url.origin}/api${path}`, { headers, data });
      expect(reply.ok()).toBeTruthy();
      return reply.json();
    }
    const main = await post('/models/connections', { name: '本机起草', provider: 'custom', protocol: 'chat-completions',
      endpoint: `http://127.0.0.1:${binding!.port}/v1`, model: 'budget-main' });
    const review = await post('/models/connections', { name: '本机独立核验', provider: 'custom', protocol: 'chat-completions',
      endpoint: `http://127.0.0.1:${binding!.port}/v1`, model: 'budget-reviewer' });
    const roles = await page.request.patch(`${url.origin}/api/models/roles`, { headers, data: { main: main.id, review: review.id } });
    expect(roles.ok()).toBeTruthy();
    const saved = await page.request.put(`${url.origin}/api/generation/settings`, { headers, data: { dailyLimit: 1, concurrency: 2 } });
    expect((await saved.json()).autoContinueDailyBudget).toBe(false);

    const project = await post('/projects', { id: '00000000-0000-4000-8000-000000000024', topic: 'SQLite 入门', outcome: '验证额度恢复操作' });
    await expect.poll(async () => {
      const item = await page.request.get(`${url.origin}/api/projects/${project.id}`, { headers });
      return (await item.json()).task.status;
    }).toBe('ready');
    const reference = await post('/files', { name: '额度验证资料.txt', content: evidence });
    const located = await page.request.get(`${url.origin}/api/files/${reference.id}?at=1`, { headers });
    const { revision } = await located.json();
    const endpoint = `/lessons/${project.lessons[0].id}/prerequisites`;
    const data = { topic: '版框基础知识', maxCalls: 3, sources: [{ fileId: reference.id, revision, at: 1 }] };
    const preview = await post(endpoint + '/preview', data);
    const queued = await post(endpoint + '/generate', { ...data, confirmation: preview.confirmation });
    const pollJob = async () => {
      const found = await page.request.get(`${url.origin}/api${endpoint}`, { headers, params: { topic: data.topic } });
      return found.json();
    };
    await expect.poll(async () => (await pollJob()).job?.reason).toBe('daily_budget');
    expect(wire).toEqual(['budget-main']);

    // Represent yesterday's real, already-completed draft without waiting for a wall-clock day.
    execFileSync(resolve(python), ['-c', [
      'from contextlib import closing',
      'from datetime import timedelta, datetime, UTC',
      'import sqlite3, sys',
      'from backend.model_usage import billing_day',
      'day = (billing_day(datetime.now(UTC)) - timedelta(days=1)).isoformat()',
      'with closing(sqlite3.connect(sys.argv[1], timeout=5)) as db, db:',
      '    assert db.execute("UPDATE prerequisite_tasks SET daily_blocked_on=? WHERE id=? AND reason=?", (day, sys.argv[2], "daily_budget")).rowcount == 1',
      '    assert db.execute("UPDATE prerequisite_calls SET billing_day=? WHERE task_id=?", (day, sys.argv[2])).rowcount == 1',
      '    assert db.execute("UPDATE model_charges SET billing_day=? WHERE kind=\'prerequisite\' AND scope_id=?", (day, sys.argv[2])).rowcount == 1',
    ].join('\n'), join(folder, 'zhixue.sqlite3'), queued.job.id]);

    await page.goto(url.href);
    const notice = page.getByRole('region', { name: '模型额度恢复提醒' });
    await expect(notice.getByText('1 项模型任务可继续')).toBeVisible();
    await notice.getByRole('link', { name: '查看可继续任务' }).click();
    await expect(page.getByRole('heading', { name: '额度恢复', exact: true })).toBeVisible();
    expect(wire).toEqual(['budget-main']);
    await page.getByRole('button', { name: /继续.*前置知识讲解/ }).click();
    await expect(page.getByText('1 项任务已排队。', { exact: true })).toBeVisible();
    await expect(notice).toHaveCount(0);
    await expect.poll(async () => (await pollJob()).kind).toBe('explanation');
    expect(wire).toEqual(['budget-main', 'budget-reviewer']);

    await page.getByRole('link', { name: '查看模型额度设置' }).click();
    const automatic = page.getByRole('checkbox', { name: '额度恢复后自动继续' });
    await expect(automatic).not.toBeChecked();
    await automatic.check();
    await page.getByRole('button', { name: '保存生成额度' }).click();
    await expect(page.getByText('生成额度已保存', { exact: true })).toBeVisible();
    await page.reload();
    await expect(automatic).toBeChecked();
    expect(wire).toHaveLength(2);
  } finally {
    await stop(service.child);
    await new Promise<void>(resolveClose => provider.close(() => resolveClose()));
    rmSync(folder, { recursive: true, force: true });
  }
});

test('a completed stage can be skipped, remediated and retaken with durable static evidence', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-stage-e2e-'));
  let service = start(folder);
  try {
    await page.goto(await service.url);
    await page.getByRole('button', { name: '体验 SQLite 样例' }).click();
    await page.getByRole('button', { name: '提交并生成路线' }).click();
    await expect(page.getByText('3 / 3 节已就绪')).toBeVisible();
    const projectUrl = page.url();
    const stagePanel = page.getByRole('region', { name: '阶段综合测评' });
    await expect(stagePanel).toHaveCount(0);

    await page.getByRole('link', { name: '开始学习' }).click();
    await page.getByRole('checkbox', { name: '已学完', exact: true }).click();
    await expect(page.getByRole('checkbox', { name: '已学完', exact: true })).toBeChecked();
    await page.goto(projectUrl);
    const first = stagePanel.getByRole('button', { name: '01 · 建立基础' });
    await expect(stagePanel.getByText('待验收', { exact: true })).toBeVisible();
    await first.click();
    const dialog = page.getByRole('dialog', { name: '01 · 建立基础' });
    await dialog.getByRole('button', { name: '跳过测评' }).click();
    await expect(dialog.getByText('已跳过', { exact: true })).toBeVisible();
    await dialog.getByRole('button', { name: '关闭' }).click();
    await expect(page.getByRole('link', { name: '开始学习' })).toBeVisible();

    await first.click();
    await dialog.getByRole('button', { name: '继续测评' }).click();
    await dialog.getByRole('button', { name: '开始答题' }).click();
    await dialog.getByRole('radio', { name: '覆盖第一次学习的记录' }).check();
    await dialog.getByRole('radio', { name: '每份资料独占一张日志表' }).check();
    await dialog.getByRole('button', { name: '提交答案' }).click();
    await expect(dialog.getByRole('link', { name: '补学 理解表、行和列' })).toBeVisible();
    await dialog.getByRole('button', { name: '重新测评' }).click();
    await dialog.getByRole('button', { name: '开始答题' }).click();
    await dialog.getByRole('radio', { name: '该次学习的唯一编号' }).check();
    await dialog.getByRole('radio', { name: '一次完整的学习记录' }).check();
    await dialog.getByRole('button', { name: '提交答案' }).click();
    await expect(dialog.getByText('测评通过', { exact: true })).toBeVisible();
    await dialog.getByRole('button', { name: '查看第 1 次测评' }).click();
    await expect(dialog.getByText('你的答案 · 覆盖第一次学习的记录', { exact: true })).toBeVisible();
    await dialog.getByRole('button', { name: '关闭' }).click();

    await page.getByRole('link', { name: '开始学习' }).click();
    await page.getByRole('checkbox', { name: '已学完', exact: true }).click();
    await expect(page.getByRole('checkbox', { name: '已学完', exact: true })).toBeChecked();
    await page.goto(projectUrl);
    const second = stagePanel.getByRole('button', { name: '02 · 掌握查询' });
    await second.click();
    const evidence = page.getByRole('dialog', { name: '02 · 掌握查询' });
    await evidence.getByRole('button', { name: '提交成果' }).click();
    await evidence.getByLabel('证据类型').selectOption('project');
    await evidence.getByLabel('添加证据文件').setInputFiles([
      { name: 'query.sql', mimeType: 'text/plain', buffer: Buffer.from('SELECT minutes FROM learning_log WHERE minutes >= 20;') },
    ]);
    await expect(evidence.getByText('query.sql', { exact: true })).toBeVisible();
    await evidence.getByRole('button', { name: '保存成果与证据' }).click();
    await expect(evidence.getByText('仅静态审查', { exact: true })).toBeVisible();

    await stop(service.child);
    service = start(folder);
    await page.goto(await service.url);
    await page.evaluate(hash => { window.location.hash = hash; }, new URL(projectUrl).hash);
    await second.click();
    await page.setViewportSize({ width: 390, height: 780 });
    await evidence.getByText('query.sql', { exact: true }).click();
    await expect(evidence.getByText('SELECT minutes FROM learning_log WHERE minutes >= 20;', { exact: true })).toBeVisible();
    await evidence.getByLabel('自评结果').selectOption('met');
    await evidence.getByLabel('判断理由').fill('语句只选取 minutes，并通过 WHERE 保留达到阈值的行。');
    await evidence.getByLabel('引用证据').selectOption('0');
    await evidence.getByLabel('证据原文').fill('WHERE minutes >= 20');
    await evidence.getByRole('button', { name: '保存成果自评' }).click();
    await expect(evidence.getByText('成果自评通过', { exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
    await expect(evidence.getByText('仅静态审查', { exact: true })).toBeVisible();
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

test('stage evidence review previews exact lines and rejects a model citation outside them', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-stage-model-'));
  const code = 'CREATE TABLE learning_log (id INTEGER PRIMARY KEY, topic TEXT);';
  const privateLine = '原文件中只保存在本机的私有行';
  const traffic: { model: string; prompt: string }[] = [];
  let releaseReview = () => {};
  const reviewFinish = new Promise<void>(resolve => { releaseReview = resolve; });
  const provider = createServer((request, response) => {
    let raw = '';
    request.setEncoding('utf8');
    request.on('data', chunk => { raw += chunk; });
    request.on('end', async () => {
      const request = JSON.parse(raw);
      traffic.push({ model: request.model, prompt: request.messages[0].content });
      const findings = [{
        criterion: 1, verdict: 'met', note: '该语句把记录编号声明为主键。',
        citations: [{ excerpt: 1, quote: traffic.length === 1 ? privateLine : 'id INTEGER PRIMARY KEY' }],
      }];
      if (traffic.length === 2) await reviewFinish;
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({
        choices: [{ message: { content: JSON.stringify({ findings }) } }],
        usage: { prompt_tokens: 116, completion_tokens: 54 },
      }));
    });
  });
  provider.listen(0, '127.0.0.1');
  await once(provider, 'listening');
  const binding = provider.address();
  if (!binding || typeof binding === 'string') throw new Error('Expected a loopback port');
  let service = start(folder);
  try {
    const url = new URL(await service.url);
    const token = new URLSearchParams(url.hash.split('?')[1]).get('session');
    const headers = { Authorization: `Bearer ${token}` };
    async function connection(model: string, name: string) {
      const reply = await page.request.post(`${url.origin}/api/models/connections`, { headers, data: { name, model,
        provider: 'custom', protocol: 'chat-completions', endpoint: `http://127.0.0.1:${binding!.port}/v1` } });
      expect(reply.ok()).toBeTruthy();
      return reply.json();
    }
    const [main, review] = await Promise.all([connection('stage-main', '本机阶段主模型'), connection('stage-review', '本机阶段复核')]);
    const roles = await page.request.patch(`${url.origin}/api/models/roles`, { headers, data: { main: main.id, review: review.id } });
    expect(roles.ok()).toBeTruthy();

    await page.goto(url.href);
    await page.getByRole('button', { name: '体验 SQLite 样例' }).click();
    await page.getByRole('button', { name: '提交并生成路线' }).click();
    await expect(page.getByText('3 / 3 节已就绪')).toBeVisible();
    const projectUrl = page.url();
    await page.getByRole('link', { name: '开始学习' }).click();
    const learned = page.getByRole('checkbox', { name: '已学完', exact: true });
    await learned.click();
    await expect(learned).toBeChecked();
    await page.goto(projectUrl);
    const stage = page.getByRole('region', { name: '阶段综合测评' }).getByRole('button', { name: '01 · 建立基础' });
    await stage.click();
    const dialog = page.getByRole('dialog', { name: '01 · 建立基础' });
    await dialog.getByRole('button', { name: '提交成果' }).click();
    await dialog.getByLabel('证据类型').selectOption('code');
    await dialog.getByLabel('添加证据文件').setInputFiles({
      name: 'schema.sql', mimeType: 'text/plain', buffer: Buffer.from(code + '\n' + privateLine),
    });
    await expect(dialog.getByText('schema.sql', { exact: true })).toBeVisible();
    await dialog.getByRole('button', { name: '保存成果与证据' }).click();
    await dialog.getByRole('button', { name: '用复核模型评审' }).click();
    await dialog.getByLabel('证据文件').selectOption('0');
    await dialog.getByLabel('起始行').fill('1');
    await dialog.getByLabel('结束行').fill('1');
    await dialog.getByLabel('本次评审调用上限').fill('2');
    await dialog.getByRole('button', { name: '预览发送范围' }).click();
    const preview = dialog.getByRole('region', { name: '本次发送范围' });
    await expect(preview.getByText('本机阶段复核 · stage-review', { exact: true })).toBeVisible();
    await expect(preview.getByText(code, { exact: true })).toBeVisible();
    await expect(preview.getByText(privateLine, { exact: true })).toHaveCount(0);
    expect(traffic).toHaveLength(0);
    await dialog.getByRole('button', { name: '确认发送所选证据' }).click();
    await expect(dialog.getByRole('alert')).toContainText('评审结果缺少有效的要求或原文引用');
    expect(traffic).toHaveLength(1);

    await dialog.getByRole('button', { name: '重新选择评审范围' }).click();
    await dialog.getByRole('button', { name: '预览发送范围' }).click();
    await dialog.getByRole('button', { name: '确认发送所选证据' }).click();
    await expect.poll(() => traffic.length).toBe(2);
    await dialog.getByRole('button', { name: '关闭' }).click();
    await expect(stage).toContainText('模型正在评审');
    releaseReview();
    await expect(stage).toContainText('已通过', { timeout: 10000 });
    await stage.click();
    await expect(dialog.getByText('模型评审通过', { exact: true })).toBeVisible({ timeout: 10000 });
    await expect(dialog.getByText('仅静态审查', { exact: true })).toBeVisible();
    await expect(dialog.locator('cite')).toHaveText('schema.sql · 第 1 行');
    expect(traffic.map(item => item.model)).toEqual(['stage-review', 'stage-review']);
    expect(traffic.every(item => item.prompt.includes(code) && !item.prompt.includes(privateLine))).toBeTruthy();

    const stageHash = new URL(page.url()).hash;
    await stop(service.child);
    service = start(folder);
    await page.setViewportSize({ width: 390, height: 780 });
    await page.goto(await service.url);
    await page.evaluate(hash => { window.location.hash = hash; }, stageHash);
    await expect(dialog.getByText('模型评审通过', { exact: true })).toBeVisible();
    await expect(dialog.getByText('id INTEGER PRIMARY KEY', { exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
    expect(traffic).toHaveLength(2);
  } finally {
    releaseReview();
    await stop(service.child);
    await new Promise<void>(done => provider.close(() => done()));
    rmSync(folder, { recursive: true, force: true });
  }
});

test('explicit main and review backup authorization resumes the same route and course scopes', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-authorized-failover-'));
  const quote = '排好活字后，将字版固定，试印前检查字行是否齐整。';
  const wire: { model: string; prompt: string }[] = [];
  const route = { stages: [{ name: '印刷基础', lessons: [
    { key: 'proof', title: '检查活字版', outcome: '解释试印前的检查' },
  ] }] };
  const draft = { sections: [{ title: '检查字行', text: '试印前先检查字行是否齐整。', checks: ['检查时机'] }] };
  const findings = { sections: [{ index: 0, verdict: 'supported', reason: '资料写明试印前检查字行。',
    citations: [{ source: 'S1', quote }], dependsOn: [] }] };
  const provider = createServer((request, response) => {
    let input = '';
    request.setEncoding('utf8');
    request.on('data', chunk => { input += chunk; });
    request.on('end', () => {
      const { model, messages } = JSON.parse(input);
      const prompt = messages[0].content as string;
      wire.push({ model, prompt });
      const status = model === 'route-primary' ? 503 : model === 'review-primary' ? 401 : 200;
      const output = model === 'review-backup' ? findings : prompt.includes('制定学习路线') ? route : draft;
      response.writeHead(status, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify(status === 200 ? { choices: [{ message: { content: JSON.stringify(output) } }] } : {}));
    });
  });
  provider.listen(0, '127.0.0.1');
  await once(provider, 'listening');
  const binding = provider.address();
  if (!binding || typeof binding === 'string') throw new Error('Expected a loopback port');
  let service = start(folder);
  try {
    const url = new URL(await service.url);
    const token = new URLSearchParams(url.hash.split('?')[1]).get('session');
    const headers = { Authorization: `Bearer ${token}` };
    async function connect(name: string, model: string) {
      const saved = await page.request.post(`${url.origin}/api/models/connections`, { headers, data: { name, model,
        provider: 'custom', protocol: 'chat-completions', endpoint: `http://127.0.0.1:${binding!.port}/v1` } });
      expect(saved.ok()).toBeTruthy();
      return saved.json();
    }
    const [main, mainBackup, review, reviewBackup] = await Promise.all([
      connect('生成主连接', 'route-primary'), connect('生成备用连接', 'route-backup'),
      connect('复核主连接', 'review-primary'), connect('复核备用连接', 'review-backup'),
    ]);
    const roleAssignment = await page.request.patch(`${url.origin}/api/models/roles`, { headers,
      data: { main: main.id, mainBackup: mainBackup.id, review: review.id, reviewBackup: reviewBackup.id } });
    expect(roleAssignment.ok()).toBeTruthy();

    await page.goto(url.href);
    await page.getByLabel('学习问题或目标').fill('我想学习活字印刷');
    await page.getByRole('button', { name: '开始探索' }).click();
    await page.getByLabel('添加资料').setInputFiles({
      name: '试印说明.txt', mimeType: 'text/plain', buffer: Buffer.from(`仅本机抬头\n${quote}\n私人附注`),
    });
    await expect(page.getByRole('link', { name: '试印说明.txt' })).toBeVisible();
    await page.getByRole('button', { name: '整理为学习目标' }).click();
    await page.getByLabel('预期成果', { exact: true }).fill('说明试印前的检查');
    await page.getByRole('button', { name: '用主模型生成路线' }).click();
    const consent = page.getByRole('dialog', { name: '确认模型发送范围' });
    await consent.getByLabel('本任务调用次数上限').fill('5');
    const chooseBackup = consent.getByRole('checkbox', { name: '授权主模型备用连接' });
    await expect(chooseBackup).not.toBeChecked();
    await chooseBackup.check();
    await consent.getByRole('button', { name: '查看发送范围' }).click();
    await expect(consent.getByText('备用 · 生成备用连接 · route-backup', { exact: true })).toBeVisible();
    expect(wire).toHaveLength(0);
    await consent.getByRole('button', { name: '确认并生成路线' }).click();
    const unreviewed = page.getByRole('button', { name: '查看待核验草稿' });
    await expect(unreviewed).toBeVisible({ timeout: 15000 });
    const projectHash = new URL(page.url()).hash;
    expect(wire.map(item => item.model)).toEqual(['route-primary', 'route-backup', 'route-backup']);
    expect(wire[0].prompt).toBe(wire[1].prompt);
    await expect(page.getByText('备用主模型 · 生成备用连接 · route-backup', { exact: true })).toBeVisible();
    await expect(page.getByRole('link', { name: '开始学习' })).toHaveCount(0);

    await page.getByRole('button', { name: '选择复核资料', exact: true }).click();
    const reviewDialog = page.getByRole('dialog', { name: '选择独立复核资料' });
    await reviewDialog.getByLabel('知识库资料').selectOption({ label: '试印说明.txt' });
    await reviewDialog.getByLabel('页、行或段落编号').fill('2');
    await reviewDialog.getByRole('button', { name: '加入此定位' }).click();
    await reviewDialog.getByRole('checkbox', { name: '授权复核备用连接' }).check();
    await reviewDialog.getByRole('button', { name: '预览复核发送范围' }).click();
    await expect(reviewDialog.getByText('备用 · 复核备用连接 · review-backup', { exact: true })).toBeVisible();
    expect(wire).toHaveLength(3);
    await reviewDialog.getByRole('button', { name: '确认并启用复核' }).click();
    const read = page.getByRole('link', { name: '开始学习' });
    await expect(read).toBeVisible({ timeout: 15000 });
    expect(wire.map(item => item.model)).toEqual(['route-primary', 'route-backup', 'route-backup', 'review-primary', 'review-backup']);
    expect(wire[3].prompt).toBe(wire[4].prompt);
    expect(wire[4].prompt).toContain(quote);
    expect(wire[4].prompt).not.toContain('私人附注');
    await expect(page.getByText(/已计入 5 次调用/)).toBeVisible();

    await stop(service.child);
    service = start(folder);
    await page.goto(await service.url);
    await page.evaluate(hash => { window.location.hash = hash; }, projectHash);
    await expect(page.getByText('备用复核模型 · 复核备用连接 · review-backup', { exact: true })).toBeVisible();
    await read.click();
    await expect(page.getByRole('heading', { name: '检查字行' })).toBeVisible();
    expect(wire).toHaveLength(5);
  } finally {
    await stop(service.child);
    await new Promise<void>(done => provider.close(() => done()));
    rmSync(folder, { recursive: true, force: true });
  }
});

test('prerequisite backup resumes the saved draft after its shared call limit is raised', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-prerequisite-backup-'));
  const quote = '参数由驱动绑定到占位符，不直接拼入语句。';
  const privateLine = '与本次学习无关的私有附注';
  const explanation = '占位符由驱动绑定参数，语句结构保持不变。';
  const calls: { model: string; prompt: string }[] = [];
  const provider = createServer((request, response) => {
    let raw = '';
    request.setEncoding('utf8');
    request.on('data', chunk => { raw += chunk; });
    request.on('end', () => {
      const { model, messages } = JSON.parse(raw);
      calls.push({ model, prompt: messages[0].content });
      const status = model === 'quick-origin' ? 401 : model === 'quick-verifier' ? 503 : 200;
      const output = model === 'quick-standby-review' ? { sections: [{
        index: 0, verdict: 'supported', reason: '给定资料明确说明驱动绑定。',
        citations: [{ source: 'S1', quote }], dependsOn: [],
      }] } : { sections: [{ title: '占位符的作用', text: explanation, checks: ['驱动绑定'] }] };
      response.writeHead(status, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify(status === 200 ? { choices: [{ message: { content: JSON.stringify(output) } }] } : {}));
    });
  });
  provider.listen(0, '127.0.0.1');
  await once(provider, 'listening');
  const binding = provider.address();
  if (!binding || typeof binding === 'string') throw new Error('Expected a loopback port');
  const service = start(folder);
  try {
    const url = new URL(await service.url);
    const token = new URLSearchParams(url.hash.split('?')[1]).get('session');
    const headers = { Authorization: `Bearer ${token}` };
    async function connect(name: string, model: string) {
      const response = await page.request.post(`${url.origin}/api/models/connections`, { headers, data: {
        name, model, provider: 'custom', protocol: 'chat-completions', endpoint: `http://127.0.0.1:${binding!.port}/v1`,
      } });
      expect(response.ok()).toBeTruthy();
      return response.json();
    }
    const [main, backup, review, reviewBackup] = await Promise.all([
      connect('讲解主连接', 'quick-origin'), connect('讲解备用', 'quick-standby-main'),
      connect('独立核验', 'quick-verifier'), connect('核验备用', 'quick-standby-review'),
    ]);
    const roles = await page.request.patch(`${url.origin}/api/models/roles`, { headers,
      data: { main: main.id, mainBackup: backup.id, review: review.id, reviewBackup: reviewBackup.id } });
    expect(roles.ok()).toBeTruthy();

    await page.goto(url.href);
    await page.getByRole('button', { name: '体验 SQLite 样例' }).click();
    await page.getByRole('button', { name: '提交并生成路线' }).click();
    await expect(page.getByText('3 / 3 节已就绪')).toBeVisible();
    await page.getByRole('link', { name: '开始学习' }).click();
    await page.getByRole('button', { name: '补前置知识', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '前置知识' });
    await dialog.getByLabel('前置知识主题').fill('驱动怎样绑定参数');
    await dialog.getByRole('button', { name: '查找讲解' }).click();
    await dialog.getByLabel('导入核验资料').setInputFiles({
      name: '驱动资料.txt', mimeType: 'text/plain', buffer: Buffer.from(`原件标题\n${quote}\n${privateLine}`),
    });
    await dialog.getByLabel('页、行或段落编号').fill('2');
    await dialog.getByRole('button', { name: '加入此定位' }).click();
    await dialog.getByLabel('本次讲解调用次数上限').fill('3');
    const mainChoice = dialog.getByRole('checkbox', { name: '授权主模型备用连接' });
    const reviewChoice = dialog.getByRole('checkbox', { name: '授权复核备用连接' });
    await expect(mainChoice).not.toBeChecked();
    await expect(reviewChoice).not.toBeChecked();
    await mainChoice.check();
    await reviewChoice.check();
    await dialog.getByRole('button', { name: '预览前置讲解发送范围' }).click();
    await expect(dialog.getByText('主模型备用 · 讲解备用 · quick-standby-main', { exact: true })).toBeVisible();
    await expect(dialog.getByText('复核备用 · 核验备用 · quick-standby-review', { exact: true })).toBeVisible();
    expect(calls).toHaveLength(0);
    await dialog.getByRole('button', { name: '确认并生成前置讲解' }).click();
    await expect(dialog.getByRole('alert')).toContainText('调用次数上限', { timeout: 10000 });
    expect(calls.map(item => item.model)).toEqual(['quick-origin', 'quick-standby-main', 'quick-verifier']);
    await dialog.getByText('查看待处理草稿', { exact: true }).click();
    await expect(dialog.getByText(explanation, { exact: true })).toBeVisible();

    await dialog.getByRole('button', { name: '修改资料与额度' }).click();
    await expect(mainChoice).toBeChecked();
    await expect(reviewChoice).toBeChecked();
    await dialog.getByLabel('本次讲解调用次数上限').fill('4');
    await dialog.getByRole('button', { name: '预览前置讲解发送范围' }).click();
    await dialog.getByRole('button', { name: '确认并继续讲解任务' }).click();
    await expect(dialog.getByText(/已保存到知识库/)).toBeVisible({ timeout: 10000 });
    await expect(dialog.getByText(explanation, { exact: true })).toBeVisible();
    expect(calls.map(item => item.model)).toEqual(['quick-origin', 'quick-standby-main', 'quick-verifier', 'quick-standby-review']);
    expect(calls[0].prompt).toBe(calls[1].prompt);
    expect(calls[2].prompt).toBe(calls[3].prompt);
    expect(calls.every(item => item.prompt.includes(quote) && !item.prompt.includes(privateLine))).toBeTruthy();
  } finally {
    await stop(service.child);
    await new Promise<void>(done => provider.close(() => done()));
    rmSync(folder, { recursive: true, force: true });
  }
});

async function holdSecondPageOcrSave(page: Page) {
  const path = '**/api/files/*/pages/2/ocr';
  const { promise: started, resolve: markStarted } = Promise.withResolvers<void>();
  const { promise: released, resolve: release } = Promise.withResolvers<void>();
  await page.route(path, async route => {
    if (route.request().method() !== 'PUT') return route.fallback();
    markStarted();
    await released;
    await route.continue();
  });
  return { started, release, stop: () => page.unroute(path) };
}

test('knowledge library reviews real local OCR and opens a confirmed hit on the scanned page', async ({ page }) => {
  test.setTimeout(90000);
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-library-ocr-'));
  const service = start(folder);
  try {
    const source = execFileSync(resolve(python), ['-c', [
      'import pymupdf, sys',
      'with pymupdf.open() as original, pymupdf.open() as scanned:',
      '    page = original.new_page(width=560, height=210)',
      '    page.insert_text((35, 65), "Zhixue Local Scan 2026", fontsize=32)',
      '    page.insert_text((35, 140), "中文学习材料", fontsize=34, fontname="china-s")',
      '    image = page.get_pixmap(dpi=200)',
      '    scanned.new_page(width=560, height=210).insert_image(page.rect, pixmap=image)',
      '    scanned.new_page(width=560, height=210)',
      '    assert not scanned[0].get_text().strip()',
      '    sys.stdout.buffer.write(scanned.tobytes(garbage=4, deflate=True))',
    ].join('\n')]);

    await page.setViewportSize({ width: 410, height: 900 });
    await page.goto(await service.url);
    await page.getByRole('link', { name: '知识库', exact: true }).click();
    await page.getByLabel('导入资料').setInputFiles({ name: '扫描复习.pdf', mimeType: 'application/pdf', buffer: source });
    await expect(page.getByText('第 1、2 页无可提取文字')).toBeVisible();
    await page.getByRole('button', { name: '识别本页' }).click();
    const draft = page.getByRole('textbox', { name: '校对文字' });
    await expect(draft).toHaveValue(/Zhixue Local Scan 2026/);
    await expect(draft).toHaveValue(/中文学习材料/);
    const search = page.getByRole('searchbox', { name: '搜索知识库' });
    await search.fill('Zhixue Local Scan');
    await expect(page.getByText('没有匹配的内容')).toBeVisible();
    await draft.fill('人工校对 搜索定位 2026');
    await page.getByRole('button', { name: '保存校对文字' }).click();
    await expect(page.getByText('第 2 页无可提取文字')).toBeVisible();
    await expect(page.getByText('人工校对 搜索定位 2026', { exact: true })).toBeVisible();

    await search.fill('搜索定位');
    await page.getByRole('button', { name: /扫描复习\.pdf.*第 1 页 · 已校对 OCR/ }).click();
    const original = page.getByRole('img', { name: 'PDF 原件第 1 页' });
    await expect.poll(() => original.evaluate(image => (image as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
    await page.reload();
    await expect(search).toHaveValue('搜索定位');
    await expect(page.locator('.library-preview .file-anchor')).toContainText('人工校对 搜索定位 2026');

    const fileId = new URLSearchParams(new URL(page.url()).hash.split('?')[1]).get('file');
    expect(fileId).toBeTruthy();
    await page.evaluate(id => { location.hash = '/library?file=' + id + '&at=1'; }, fileId);
    const preview = page.locator('.library-preview');
    const editor = preview.getByRole('region', { name: '识别与校对' });
    const revisionPath = new URL('/api/files/' + fileId + '?at=1', page.url());
    const headers = { Authorization: 'Bearer ' + await page.evaluate(() => sessionStorage.getItem('zhixue-session')) };
    const current = await page.request.get(revisionPath.href, { headers });
    expect(current.ok()).toBeTruthy();
    const first = await current.json();
    const ocrPath = new URL('/api/files/' + fileId + '/pages/1/ocr', page.url()).href;
    await editor.getByRole('button', { name: '编辑校对文字' }).click();
    await expect(draft).toHaveValue('人工校对 搜索定位 2026');
    await draft.fill('第一页的本机待保存校对');
    const otherText = '另一窗口已保存的校对';
    const other = await page.request.put(ocrPath, {
      headers, data: { revision: first.revision, textRevision: first.anchor.textRevision, text: otherText },
    });
    expect(other.ok()).toBeTruthy();
    await preview.getByRole('button', { name: '收藏文件' }).click();
    await expect(preview.locator('.file-anchor')).toContainText(otherText);
    await expect(draft).toHaveValue('第一页的本机待保存校对');
    const saving = page.waitForResponse(response => response.url() === ocrPath && response.request().method() === 'PUT');
    await editor.getByRole('button', { name: '保存校对文字' }).click();
    expect((await saving).status()).toBe(409);
    await expect(editor.getByRole('alert')).toContainText('识别文本已在其他窗口保存');
    await expect(draft).toHaveValue('第一页的本机待保存校对');
    await expect(preview.locator('.file-anchor')).toContainText(otherText);
    const latest = await page.request.get(revisionPath.href, { headers });
    expect((await latest.json()).anchor.text).toBe(otherText);
    await editor.getByRole('button', { name: '取消', exact: true }).click();
    await page.evaluate(id => { location.hash = '/library/?file=' + id + '&at=1'; }, fileId);
    await expect(preview.locator('.file-anchor')).toContainText(otherText);
    await editor.getByRole('button', { name: '编辑校对文字' }).click();
    await expect(draft).toHaveValue(otherText);
    expect(new URL(page.url()).hash.split('?')[0]).toBe('#/library/');
    await draft.fill('对照新版重新校对完成');
    await editor.getByRole('button', { name: '保存校对文字' }).click();
    await expect(preview.locator('.file-anchor')).toContainText('对照新版重新校对完成');

    await editor.getByRole('button', { name: '校对版本', exact: true }).click();
    const history = page.getByRole('dialog', { name: '校对版本', exact: true });
    await expect(history.getByRole('combobox', { name: '选择校对版本' }).locator('option')).toHaveCount(3);
    await history.getByRole('combobox', { name: '选择校对版本' }).selectOption(first.anchor.textRevision);
    await expect(history.getByLabel('版本差异')).toContainText('+人工校对 搜索定位 2026');
    await expect(history.getByLabel('版本差异')).toContainText('-对照新版重新校对完成');
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(410);
    await history.getByRole('button', { name: '恢复此校对版本', exact: true }).click();
    await expect(history).toBeHidden();
    await expect(preview.locator('.file-anchor')).toContainText('人工校对 搜索定位 2026');
    await page.reload();
    await expect(preview.locator('.file-anchor')).toContainText('人工校对 搜索定位 2026');
    await search.fill('搜索定位');
    await expect(page.getByRole('button', { name: /扫描复习\.pdf.*第 1 页 · 已校对 OCR/ })).toBeVisible();

    await page.getByRole('button', { name: '下一页' }).click();
    await expect(page.getByRole('img', { name: 'PDF 原件第 2 页' })).toBeVisible();
    await page.getByRole('button', { name: '识别本页' }).click();
    await expect(page.getByRole('alert')).toContainText('本页未识别到文字');
    await page.getByRole('button', { name: '手动录入' }).click();
    await expect(page.getByRole('textbox', { name: '校对文字' })).toBeEditable();

    await draft.fill('旧第二页独有的历史校对');
    await editor.getByRole('button', { name: '保存校对文字' }).click();
    await expect(preview.locator('.file-anchor')).toContainText('旧第二页独有的历史校对');
    const secondPath = new URL('/api/files/' + fileId + '?at=2', page.url()).href;
    const second = await (await page.request.get(secondPath, { headers })).json();
    await editor.getByRole('button', { name: '编辑校对文字' }).click();
    await draft.fill('第二页重新确认的文字');
    await editor.getByRole('button', { name: '保存校对文字' }).click();
    await expect(preview.locator('.file-anchor')).toContainText('第二页重新确认的文字');

    await page.getByRole('button', { name: '上一页' }).click();
    await expect(page.getByRole('img', { name: 'PDF 原件第 1 页' })).toBeVisible();
    await editor.getByRole('button', { name: '校对版本', exact: true }).click();
    await history.getByRole('combobox', { name: '选择校对页', exact: true }).selectOption('2', { timeout: 15000 });
    await history.getByRole('combobox', { name: '选择校对版本', exact: true }).selectOption(second.anchor.textRevision);
    await expect(history.getByLabel('版本差异')).toContainText('+旧第二页独有的历史校对');
    await history.getByRole('button', { name: '恢复此校对版本', exact: true }).click();
    await expect(history).toBeHidden();
    await expect(page.getByRole('img', { name: 'PDF 原件第 2 页' })).toBeVisible();
    await expect(preview.locator('.file-anchor')).toContainText('旧第二页独有的历史校对');

    const lateText = '已离开页面后的新校对';
    await search.fill(lateText);
    await expect(page.getByText('没有匹配的内容')).toBeVisible();
    const heldSave = await holdSecondPageOcrSave(page);
    await editor.getByRole('button', { name: '编辑校对文字' }).click();
    await draft.fill(lateText);
    await editor.getByRole('button', { name: '保存校对文字' }).click();
    try {
      await heldSave.started;
      await page.getByRole('button', { name: '上一页' }).click();
      await expect(original).toBeVisible();
    } finally {
      heldSave.release();
    }
    const lateHit = page.getByRole('button', { name: /扫描复习\.pdf.*第 2 页 · 已校对 OCR/ });
    await expect(lateHit).toBeVisible();
    await expect(original).toBeVisible();
    await expect(search).toHaveValue(lateText);
    await heldSave.stop();
    await lateHit.click();
    await expect(preview.locator('.file-anchor')).toContainText(lateText);

    const replacement = execFileSync(resolve(python), ['-c', [
      'import pymupdf, sys',
      'with pymupdf.open(stream=sys.stdin.buffer.read(), filetype="pdf") as previous, pymupdf.open() as current:',
      '    current.insert_pdf(previous, from_page=0, to_page=0)',
      '    sys.stdout.buffer.write(current.tobytes())',
    ].join('\n')], { input: source });
    const disk = realpathSync(resolve(folder, first.path));
    const inside = relative(realpathSync(folder), disk);
    expect(inside && !inside.startsWith('..') && !isAbsolute(inside)).toBe(true);
    writeFileSync(disk, replacement);
    await page.evaluate(id => { location.hash = '/library?file=' + id + '&at=1'; }, fileId);
    await expect(page.getByRole('button', { name: '下一页' })).toBeDisabled();
    await editor.getByRole('button', { name: '校对版本', exact: true }).click();
    const pages = history.getByRole('combobox', { name: '选择校对页', exact: true });
    await expect(pages.getByRole('option', { name: '第 2 页 · 仅历史', exact: true })).toHaveCount(1);
    await pages.selectOption('2');
    await history.getByRole('combobox', { name: '选择校对版本', exact: true }).selectOption(second.anchor.textRevision);
    await expect(history.getByText('当前原件已没有第 2 页，可查看历史校对。', { exact: true })).toBeVisible();
    await expect(history.getByText('旧第二页独有的历史校对', { exact: true })).toBeVisible();
    await expect(history.getByRole('button', { name: '恢复此校对版本', exact: true })).toHaveCount(0);
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

test('external recognition asks separately for the recording and selected page before sending', async ({}, info) => {
  test.setTimeout(75000);
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-recognition-browser-'));
  const requests: { path?: string; contentType?: string; body: Buffer }[] = [];
  let releasePage: (() => void) | undefined;
  let pageReplyGate: Promise<void> | undefined;
  const provider = createServer(async (request, response) => {
    const chunks: Buffer[] = [];
    for await (const chunk of request) chunks.push(Buffer.from(chunk));
    requests.push({ path: request.url, contentType: request.headers['content-type'], body: Buffer.concat(chunks) });
    const reply = request.url === '/v1/audio/transcriptions' ? { text: '外部录音识别草稿' }
      : { choices: [{ message: { content: '外部单页识别草稿' }, finish_reason: 'length' }],
          usage: { prompt_tokens: 20, completion_tokens: 8 } };
    if (request.url === '/v1/chat/completions' && pageReplyGate) await pageReplyGate;
    response.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify(reply));
  });
  provider.listen(0, '127.0.0.1');
  await once(provider, 'listening');
  const binding = provider.address();
  if (!binding || typeof binding === 'string') throw new Error('Expected a loopback port');
  const recipient = `http://127.0.0.1:${binding.port}/v1`;
  const service = start(folder);
  let browser: Browser | undefined;
  try {
    browser = await chromium.launch({ channel: 'chrome', args: [
      '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream',
    ] });
    const page = await browser.newPage({ viewport: { width: 410, height: 900 } });
    page.setDefaultTimeout(8500);
    const address = new URL(await service.url);
    const token = new URLSearchParams(address.hash.split('?')[1]).get('session');
    const headers = { Authorization: 'Bearer ' + token };
    const connected = await page.request.post(address.origin + '/api/models/connections', { headers, data: {
      name: '识别测试接收方', protocol: 'chat-completions', provider: 'custom', endpoint: recipient, model: 'vision-test',
    } });
    expect(connected.ok()).toBeTruthy();
    await page.route('**/api/voice', route => route.fulfill({ json: { available: true, maxSeconds: 60 } }));
    let localAttempts = 0;
    await page.route('**/api/voice/transcribe?*', route => {
      localAttempts++;
      return route.fulfill({ status: 503, json: { detail: '本机识别暂不可用' } });
    });
    await page.goto(address.href);
    const input = page.getByLabel('学习问题或目标');
    await input.fill('保留原有问题');
    await page.getByRole('button', { name: '学习问题语音输入' }).click();
    const dialog = page.getByRole('dialog', { name: '录音转文字' });
    await dialog.getByLabel('录音语言').selectOption('en');
    await dialog.getByRole('button', { name: '开始录音' }).click();
    await expect(dialog.getByText('正在录音', { exact: true })).toBeVisible();
    await new Promise(resolve => setTimeout(resolve, 650));
    await dialog.getByRole('button', { name: '停止并识别' }).click();
    await expect(dialog.getByRole('alert')).toContainText('本机识别暂不可用');
    expect(localAttempts).toBe(1);
    expect(requests).toHaveLength(0);
    const preview = async (surface: Locator) => {
      await surface.getByRole('button', { name: '选择外部识别服务' }).click();
      await expect(surface.getByRole('button', { name: /^发送/ })).toHaveCount(0);
      await surface.getByLabel('识别服务', { exact: true }).selectOption({ label: '识别测试接收方 · vision-test' });
      await surface.getByRole('button', { name: '预览发送范围' }).click();
    };
    await preview(dialog);
    await expect(dialog.getByText(recipient + '/audio/transcriptions', { exact: true })).toBeVisible();
    await expect(dialog.getByText(/单段 WAV 音频/)).toBeVisible();
    await dialog.screenshot({ path: info.outputPath('录音授权.png') });
    await dialog.getByRole('button', { name: '保存授权' }).click();
    const sendRecording = dialog.getByRole('button', { name: '发送此段录音', exact: true });
    await expect(sendRecording).toBeEnabled();
    expect(requests).toHaveLength(0);
    const voice = dialog.getByRole('textbox', { name: '语音草稿' });
    await voice.fill('已经手动填写的录音草稿');
    await expect(sendRecording).toHaveCount(0);
    await dialog.getByRole('button', { name: '发送此段录音并替换草稿' }).click();
    await expect(voice).toHaveValue('外部录音识别草稿');
    expect(requests.map(request => request.path)).toEqual(['/v1/audio/transcriptions']);
    expect(requests[0].contentType).toContain('multipart/form-data; boundary=');
    expect(requests[0].body.includes(Buffer.from('RIFF'))).toBeTruthy();
    await voice.fill('已核对录音文字');
    await dialog.getByRole('button', { name: '填入文字' }).click();
    await expect(input).toHaveValue('保留原有问题\n已核对录音文字');
    await expect(page.getByRole('heading', { name: '今天，想弄懂什么？' })).toBeVisible();

    const pdf = execFileSync(resolve(python), ['-c', [
      'import sys, pymupdf',
      'with pymupdf.open() as document:',
      '    for fill in [(0, 0, 1), (1, 0, 0)]:',
      '        page = document.new_page(width=250, height=150)',
      '        page.draw_rect(page.rect, color=None, fill=fill)',
      '    sys.stdout.buffer.write(document.tobytes())',
    ].join('\n')]);
    await page.getByRole('link', { name: '知识库', exact: true }).click();
    await page.getByLabel('导入资料').setInputFiles({ name: '外部识别扫描.pdf', mimeType: 'application/pdf', buffer: pdf });
    await expect(page.getByText('第 1、2 页无可提取文字')).toBeVisible();
    await page.getByRole('button', { name: '下一页' }).click();
    const original = page.getByRole('img', { name: 'PDF 原件第 2 页' });
    await expect.poll(() => original.evaluate(image => (image as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
    const ocr = page.getByRole('region', { name: '识别与校对' });
    await preview(ocr);
    await expect(ocr.getByText(recipient + '/chat/completions', { exact: true })).toBeVisible();
    await expect(ocr.getByText(/单页 PNG 图像/)).toBeVisible();
    await expect(ocr.getByText(/PDF 第 2 页/)).toBeVisible();
    await ocr.screenshot({ path: info.outputPath('单页授权.png') });
    await ocr.getByRole('button', { name: '保存授权' }).click();
    const sendPage = ocr.getByRole('button', { name: '发送所选页图' });
    await expect(sendPage).toBeEnabled();
    expect(requests).toHaveLength(1);
    await sendPage.click();
    const draft = ocr.getByRole('textbox', { name: '校对文字' });
    await expect(draft).toHaveValue('外部单页识别草稿');
    await expect(ocr.getByText('识别结果可能只包含本页部分文字，请对照原件补全。')).toBeVisible();
    await ocr.screenshot({ path: info.outputPath('单页草稿.png') });
    expect(requests.map(request => request.path)).toEqual(['/v1/audio/transcriptions', '/v1/chat/completions']);
    const message = JSON.parse(requests[1].body.toString()).messages[0].content;
    expect(message.map((part: { type: string }) => part.type)).toEqual(['text', 'image_url']);
    expect(message[1].image_url.url).toMatch(/^data:image\/png;base64,/);
    expect(requests[1].body.includes(Buffer.from('外部识别扫描.pdf'))).toBeFalsy();
    const search = page.getByRole('searchbox', { name: '搜索知识库' });
    await search.fill('外部单页识别草稿');
    await expect(page.getByText('没有匹配的内容')).toBeVisible();
    const reviewText = '校对后可搜索的文字';
    const term = '校对后可搜索';
    const heldSave = await holdSecondPageOcrSave(page);
    await draft.fill(reviewText);
    await ocr.getByRole('button', { name: '保存校对文字' }).click();
    try {
      await heldSave.started;
      await search.fill(term);
      await page.getByRole('combobox', { name: '搜索范围' }).selectOption('all');
      await expect.poll(() => page.evaluate(() => {
        const params = new URLSearchParams(location.hash.split('?')[1]);
        return [params.get('q'), params.get('scope')];
      })).toEqual([term, 'all']);
    } finally {
      heldSave.release();
    }
    await expect(draft).toHaveCount(0);
    await expect(page.locator('.file-anchor')).toContainText(reviewText);
    await expect(search).toHaveValue(term);
    await expect(page.getByRole('combobox', { name: '搜索范围' })).toHaveValue('all');
    await heldSave.stop();
    await page.getByRole('button', { name: /外部识别扫描\.pdf.*第 2 页 · 已校对 OCR/ }).click();
    await expect(original).toBeVisible();
    await page.reload();
    await expect(original).toBeVisible();

    const fileId = new URLSearchParams(new URL(page.url()).hash.split('?')[1]).get('file');
    expect(fileId).toBeTruthy();
    await page.evaluate(id => { location.hash = '/library?file=' + id + '&at=2'; }, fileId);
    await ocr.getByRole('button', { name: '选择外部识别服务' }).click();
    await expect(sendPage).toBeEnabled();
    expect(requests).toHaveLength(2);
    pageReplyGate = new Promise<void>(resolve => { releasePage = resolve; });
    await sendPage.click();
    await expect.poll(() => requests.length).toBe(3);
    const current = await page.request.get(address.origin + '/api/files/' + fileId + '?at=2', { headers });
    expect(current.ok()).toBeTruthy();
    const base = await current.json();
    const ocrPath = address.origin + '/api/files/' + fileId + '/pages/2/ocr';
    const concurrentlySaved = '识别返回前另一窗口确认的文字';
    const changed = await page.request.put(ocrPath, {
      headers, data: { revision: base.revision, textRevision: base.anchor.textRevision, text: concurrentlySaved },
    });
    expect(changed.ok()).toBeTruthy();
    const file = page.locator('.library-preview');
    await file.getByRole('button', { name: '收藏文件' }).click();
    await expect(file.locator('.file-anchor')).toContainText(concurrentlySaved);
    releasePage!();
    await expect(draft).toHaveValue('外部单页识别草稿');
    const saving = page.waitForResponse(response => response.url() === ocrPath && response.request().method() === 'PUT');
    await ocr.getByRole('button', { name: '保存校对文字' }).click();
    expect((await saving).status()).toBe(409);
    await expect(ocr.getByRole('alert')).toContainText('识别文本已在其他窗口保存');
    await expect(draft).toHaveValue('外部单页识别草稿');
    await expect(file.locator('.file-anchor')).toContainText(concurrentlySaved);

    await ocr.getByRole('button', { name: '撤销授权' }).click();
    await expect(ocr.getByRole('button', { name: /^发送所选页图/ })).toHaveCount(0);
    const voiceStatus = await page.request.get(address.origin + '/api/recognition/voice', { headers });
    expect((await voiceStatus.json()).authorization).not.toBeNull();
    expect(requests).toHaveLength(3);
  } finally {
    releasePage?.();
    await browser?.close();
    await stop(service.child);
    await new Promise<void>(done => provider.close(() => done()));
    rmSync(folder, { recursive: true, force: true });
  }
});

test('records speech locally, releases the microphone, and inserts only reviewed text', async () => {
  test.setTimeout(45000);
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-voice-browser-'));
  const service = start(folder);
  let browser: Browser | undefined;
  try {
    // The first five seconds of whisper.cpp's public JFK sample feed a synthetic Chromium microphone.
    browser = await chromium.launch({ channel: 'chrome', args: [
      '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream',
      `--use-file-for-fake-audio-capture=${resolve('e2e/fixtures/voice-jfk.wav')}`,
    ] });
    const page = await browser.newPage();
    await page.addInitScript(() => {
      if (!navigator.mediaDevices?.getUserMedia) return;
      const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
      const streams: MediaStreamTrack[] = [];
      Object.defineProperty(window, 'voiceTestTracks', { value: streams });
      const addModule = AudioWorklet.prototype.addModule;
      let holdOpening = true;
      AudioWorklet.prototype.addModule = async function (...args) {
        if (holdOpening) {
          holdOpening = false;
          await new Promise<void>(resolve => { Reflect.set(window, 'resumeVoiceWorklet', resolve); });
        }
        return addModule.apply(this, args);
      };
      navigator.mediaDevices.getUserMedia = async constraints => {
        const stream = await original(constraints);
        streams.push(...stream.getTracks());
        return stream;
      };
    });
    let transcripts = 0;
    page.on('request', request => { if (new URL(request.url()).pathname === '/api/voice/transcribe') transcripts++; });
    await page.setViewportSize({ width: 410, height: 900 });
    await page.goto(await service.url);
    const input = page.getByLabel('学习问题或目标');
    await input.fill('我想学习数据库');
    const microphone = page.getByRole('button', { name: '学习问题语音输入' });
    const dialog = page.getByRole('dialog', { name: '录音转文字' });
    await microphone.click();
    const startRecording = dialog.getByRole('button', { name: '开始录音' });
    await startRecording.click();
    await expect(dialog.getByText('正在打开麦克风')).toBeVisible();
    await expect.poll(() => page.evaluate(() => typeof Reflect.get(window, 'resumeVoiceWorklet'))).toBe('function');
    const disconnected = await page.evaluate(() => {
      const [track] = Reflect.get(window, 'voiceTestTracks') as MediaStreamTrack[];
      const previous = track.readyState;
      track.stop();
      track.dispatchEvent(new Event('ended'));
      Reflect.get(window, 'resumeVoiceWorklet')();
      return [previous, track.readyState];
    });
    expect(disconnected).toEqual(['live', 'ended']);
    await expect(dialog.getByText('麦克风已断开，请重新录音。')).toBeVisible();
    await expect(startRecording).toBeEnabled();
    expect(transcripts).toBe(0);
    await expect(input).toHaveValue('我想学习数据库');

    await startRecording.click();
    await expect(dialog.getByText('正在录音', { exact: true })).toBeVisible();
    await dialog.getByRole('button', { name: '取消' }).click();
    await expect(dialog).toHaveCount(0);
    await expect.poll(() => page.evaluate(() => Reflect.get(window, 'voiceTestTracks')
      .map((track: MediaStreamTrack) => track.readyState))).toEqual(['ended', 'ended']);
    expect(transcripts).toBe(0);
    await expect(input).toHaveValue('我想学习数据库');

    await microphone.click();
    await dialog.getByLabel('录音语言').selectOption('en');
    await dialog.getByRole('button', { name: '开始录音' }).click();
    await expect(dialog.getByText('正在录音', { exact: true })).toBeVisible();
    await new Promise(resolve => setTimeout(resolve, 4100));
    await dialog.getByRole('button', { name: '停止并识别' }).click();
    const draft = dialog.getByRole('textbox', { name: '语音草稿' });
    await expect(draft).toHaveValue(/fellow Americans/i, { timeout: 12000 });
    await expect.poll(() => page.evaluate(() => Reflect.get(window, 'voiceTestTracks')
      .map((track: MediaStreamTrack) => track.readyState))).toEqual(['ended', 'ended', 'ended']);
    await draft.fill('我希望能够理解数据库如何检索。');
    await dialog.getByRole('button', { name: '填入文字' }).click();
    await expect(input).toHaveValue('我想学习数据库\n我希望能够理解数据库如何检索。');
    await expect(page.getByRole('heading', { name: '今天，想弄懂什么？' })).toBeVisible();
    expect(transcripts).toBe(1);

    await page.getByRole('button', { name: '开始探索' }).click();
    await expect(page.getByRole('button', { name: '对话语音输入' })).toBeVisible();
    await page.getByRole('button', { name: '整理为学习目标' }).click();
    await page.getByRole('button', { name: '语音改写 预期成果' }).click();
    await dialog.getByLabel('录音语言').selectOption('en');
    await dialog.getByRole('button', { name: '开始录音' }).click();
    await expect(dialog.getByText('正在录音', { exact: true })).toBeVisible();
    await new Promise(resolve => setTimeout(resolve, 4100));
    await dialog.getByRole('button', { name: '停止并识别' }).click();
    await expect(draft).toHaveValue(/fellow Americans/i, { timeout: 12000 });
    await draft.fill('能解释索引查找的步骤');
    await dialog.getByRole('button', { name: '替换字段文字' }).click();
    await expect(page.getByLabel('预期成果', { exact: true })).toHaveValue('能解释索引查找的步骤');
    await expect(page.getByText('目标卡有待保存的改动。')).toBeVisible();
    expect(transcripts).toBe(2);
  } finally {
    await browser?.close();
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

test('knowledge library imports a webpage, locates extracted text, and downloads the captured bytes', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-web-library-'));
  const fixture = join(folder, 'page.html');
  const original = Buffer.from(`<!doctype html>
    <title>公开网页测试</title><nav>页面导航</nav>
    <main><h1>开始阅读</h1><p>第一节的介绍。</p>
    <h2>搜索与定位</h2><p>唯一网页正文标记。</p>
    <script>window.fixtureExecuted = true</script></main>`);
  writeFileSync(fixture, original);
  const startWebFixture = `
from pathlib import Path
import sys
from backend import launch, library_api
from backend.web_fetch import Snapshot

page = Path(sys.argv.pop(1)).read_bytes()
library_api.download_page = lambda address: Snapshot(
    address, "https://example.test/guide", "2026-11-15T14:00:00+00:00",
    "utf-8", "公开网页测试", page,
)
raise SystemExit(launch.main())
`;
  const service = startService(folder, ['-c', startWebFixture, fixture]);
  try {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(await service.url);
    await page.getByRole('link', { name: '知识库', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '导入公开网页' });

    async function importPage() {
      await page.getByRole('button', { name: '导入网页', exact: true }).click();
      await dialog.getByLabel('网页地址').fill('https://example.test/start');
      await dialog.getByRole('button', { name: '保存网页快照' }).click();
    }

    await importPage();
    await expect(page.getByRole('heading', { name: '公开网页测试.html' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'https://example.test/guide' })).toHaveAttribute('href', 'https://example.test/guide');
    const sections = page.getByLabel('网页分节');
    await sections.selectOption('2');
    await expect(page.locator('.library-preview .file-anchor')).toContainText('唯一网页正文标记。');
    await expect(page.locator('.library-preview')).not.toContainText('第一节的介绍。');
    expect(await page.evaluate(() => Reflect.has(window, 'fixtureExecuted'))).toBe(false);

    await page.getByLabel('文件类型').selectOption('html');
    await page.getByRole('searchbox', { name: '搜索知识库' }).fill('唯一网页正文');
    const match = page.getByRole('button', { name: /公开网页测试\.html.*网页第 2 节 · 搜索与定位/ });
    await expect(match).toBeVisible();
    await match.click();
    await expect(sections).toHaveValue('2');
    await page.reload();
    await expect(page.locator('.library-preview .file-anchor')).toContainText('唯一网页正文标记。');
    const download = page.waitForEvent('download');
    await page.getByRole('button', { name: '下载网页快照' }).click();
    const saved = await download;
    expect(saved.suggestedFilename()).toBe('公开网页测试.html');
    expect(readFileSync(await saved.path())).toEqual(original);

    await importPage();
    await expect(dialog.getByText('已有相同的网页快照')).toBeVisible();
    await dialog.getByRole('button', { name: '保留新副本' }).click();
    await expect(dialog).toHaveCount(0);
    await expect(page.getByRole('button', { name: /公开网页测试\.html/ })).toHaveCount(2);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

test('knowledge library preserves search context across wiki links and backlinks', async ({ page }, info) => {
  test.setTimeout(50_000);
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-wiki-context-'));
  const service = start(folder);
  const query = '续接导航样本';
  const searchParams = { q: query, folder: 'library/imports', format: 'md', scope: 'all' };
  try {
    const origin = new URL(await service.url);
    const key = new URLSearchParams(origin.hash.split('?')[1]).get('session');
    const headers = { Authorization: 'Bearer ' + key };
    async function importMarkdown(name: string, content: string) {
      const response = await page.request.post(origin.origin + '/api/files', { headers, data: { name, content } });
      expect(response.ok()).toBeTruthy();
      return (await response.json()).id as string;
    }

    const target = await importMarkdown('参考入口.md', '# 参考段落\n\n定义在这里。');
    const first = await importMarkdown('双份材料.md', '# 甲版材料');
    await importMarkdown('双份材料.md', '# 乙版材料');
    const targetBody = await page.request.get(origin.origin + '/api/files/' + target, { headers });
    expect(targetBody.ok()).toBeTruthy();
    const revision = (await targetBody.json()).revision as string;
    const direct = '#/library?' + new URLSearchParams({ file: target, at: '3', revision });
    const source = await importMarkdown('导航原文.md',
      '# 导航原文\n\n' + query + ' [[参考入口]]、[[双份材料]]，再读[定义原文](' + direct + ')。');
    await page.goto(origin.href);
    await page.evaluate(params => { window.location.hash = '#/library?' + new URLSearchParams(params); }, searchParams);

    const preview = page.locator('.library-preview');
    const search = page.getByRole('searchbox', { name: '搜索知识库' });
    const getParams = () => new URLSearchParams(new URL(page.url()).hash.split('?')[1]);
    async function expectSearch() {
      await expect(search).toHaveValue(query);
      const current = getParams();
      for (const [name, value] of Object.entries(searchParams)) expect(current.get(name)).toBe(value);
    }

    await page.getByRole('button', { name: /导航原文\.md.*第 3 行/ }).click();
    await expect(preview.locator('.file-anchor')).toContainText(query);
    const sourceRevision = getParams().get('revision');
    expect(sourceRevision).toBeTruthy();
    await preview.getByRole('link', { name: '[[参考入口]]', exact: true }).click();
    await expect(preview.getByRole('heading', { name: '参考段落', exact: true })).toBeVisible();
    await expectSearch();
    expect(getParams().get('file')).toBe(target);
    expect(getParams().get('at')).toBeNull();
    expect(getParams().get('revision')).toBeNull();

    await preview.getByText('反向链接', { exact: true }).click();
    await preview.getByRole('link', { name: '导航原文.md · 第 3 行' }).click();
    await expect(preview.locator('.file-anchor')).toContainText(query);
    await expectSearch();
    expect(getParams().get('file')).toBe(source);
    expect(getParams().get('at')).toBe('3');
    expect(getParams().get('revision')).toBe(sourceRevision);

    await preview.getByRole('link', { name: '[[双份材料]]', exact: true }).click();
    await expect(preview.getByText('有多个文件匹配「双份材料」。请选择要打开的文件。')).toBeVisible();
    await expectSearch();
    await preview.getByRole('link', { name: new RegExp(first) }).click();
    await expect(preview.getByRole('heading', { name: '甲版材料' })).toBeVisible();
    await expectSearch();

    await page.goBack();
    await preview.getByRole('link', { name: '定义原文', exact: true }).click();
    await expect(preview.locator('.file-anchor')).toContainText('定义在这里。');
    await expectSearch();
    expect(getParams().get('file')).toBe(target);
    expect(getParams().get('at')).toBe('3');
    expect(getParams().get('revision')).toBe(revision);
    await page.reload();
    await expect(preview.locator('.file-anchor')).toContainText('定义在这里。');
    await expectSearch();
    await page.screenshot({ path: info.outputPath('双链与原文定位.png'), fullPage: true });
  } finally {
    await stop(service.child);
    const fixture = realpathSync(folder);
    if (dirname(fixture) !== realpathSync(tmpdir()) || !fixture.split(/[\\/]/).at(-1)?.startsWith('zhixue-wiki-context-')) {
      throw new Error('Unexpected wiki test fixture path');
    }
    rmSync(fixture, { recursive: true, force: true });
  }
});

test('wiki links complete in both note editors and return to the exact source line', async ({ page }) => {
  test.setTimeout(65_000);
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-wiki-'));
  const service = start(folder);
  try {
    const address = await service.url;
    const lesson = await seedNoteLesson(page, address);
    const url = new URL(address);
    const session = new URLSearchParams(url.hash.split('?')[1]).get('session');
    const headers = { Authorization: 'Bearer ' + session };
    const noteId = '371fe2b8-e637-487d-b75b-a812e0567641';
    const note = 'note-' + noteId;
    const created = await page.request.post(new URL('/api/notes', url).href, {
      headers, data: { id: noteId, title: '索引札记', lessonIds: [] },
    });
    expect(created.ok()).toBeTruthy();

    async function importText(name: string, content: string) {
      const response = await page.request.post(new URL('/api/files', url).href, { headers, data: { name, content } });
      expect(response.ok()).toBeTruthy();
      return (await response.json()).id as string;
    }

    const card = await importText('查询随身卡.txt', '回到 [[' + note + '|索引札记]]。');
    const first = await importText('并存样例.md', '# 第一份样例');
    const second = await importText('并存样例.md', '# 第二份样例');
    const literal = '[[' + card + '|查询随身卡]]';
    writeFileSync(join(folder, 'library', 'courses', lesson + '.md'),
      '# 课程互链测试\n\n每次查询前看 [[' + card + '|随身卡]]。\n\n`' + literal + '` 是代码。\n');

    const search = page.getByRole('searchbox', { name: '搜索知识库' });
    await page.goto(address.replace('#/?', '#/library?file=' + note + '&edit=1&q=wiki-context&'));
    const preview = page.locator('.library-preview');
    const editor = preview.getByRole('textbox', { name: '笔记正文' });
    await expect(editor).toBeEditable();
    await editor.click();
    await page.keyboard.insertText('查询参考 [[查询随身');
    const suggestions = preview.getByRole('listbox', { name: '双链补全' });
    await expect(suggestions.getByRole('option', { name: /查询随身卡/ })).toHaveAttribute('aria-selected', 'true');
    await editor.press('Enter');
    await expect(editor.getByRole('link', { name: '[[查询随身卡.txt]]' })).toBeVisible();

    await preview.getByRole('button', { name: 'Markdown 原文' }).click();
    const stored = '查询参考 [[' + card + '|查询随身卡.txt]]';
    await expect(editor).toHaveValue(stored);
    const partial = stored + '\n\n补充 [[并存';
    const suffix = '\n\n这里还有需要保留的原文。';
    await editor.fill(partial + suffix);
    await editor.evaluate((node, offset) => {
      (node as HTMLTextAreaElement).setSelectionRange(offset, offset);
    }, partial.length);
    await editor.press('Shift');
    await expect(suggestions.getByRole('option')).toHaveCount(2);
    await editor.press('ArrowDown');
    const chosen = await suggestions.getByRole('option', { selected: true }).textContent();
    const chosenId = chosen?.includes(first) ? first : second;
    const resumeFrames = await page.evaluateHandle(() => {
      const request = window.requestAnimationFrame;
      const pending: FrameRequestCallback[] = [];
      // Hold the next paint until the user selects all text after accepting a completion.
      window.requestAnimationFrame = callback => { pending.push(callback); return 0; };
      return () => {
        window.requestAnimationFrame = request;
        for (const callback of pending.splice(0)) callback(performance.now());
      };
    });
    try {
      await editor.press('Tab');
      const completed = stored + '\n\n补充 [[' + chosenId + '|并存样例.md]]';
      await expect(editor).toHaveValue(completed + suffix);
      expect(await editor.evaluate(field => (field as HTMLTextAreaElement).selectionStart)).toBe(completed.length);
      await editor.selectText();
    } finally {
      await resumeFrames.evaluate(resume => resume());
      await resumeFrames.dispose();
    }
    expect(await editor.evaluate(field => [(field as HTMLTextAreaElement).selectionStart,
      (field as HTMLTextAreaElement).selectionEnd])).toEqual([0, (await editor.inputValue()).length]);

    const source = stored + '\n\n独立引用 [[并存样例]] 和 [[尚不存在]]。\n\n`' + literal + '`';
    await editor.fill(source);
    await expect.poll(() => readFileSync(join(folder, 'library', 'notes', noteId + '.md'), 'utf8')).toBe(source);
    await preview.getByRole('button', { name: '富文本' }).click();
    await editor.getByRole('link', { name: '[[查询随身卡.txt]]' }).click();
    await expect(preview.getByRole('heading', { name: '查询随身卡.txt' })).toBeVisible();
    await expect(search).toHaveValue('wiki-context');

    await preview.getByText('反向链接', { exact: true }).click();
    await expect(preview.locator('.wiki-backlinks a')).toHaveCount(2);
    await preview.getByRole('link', { name: '索引札记 · 第 1 行' }).click();
    await expect(preview.locator('.file-anchor')).toContainText(stored);
    await preview.getByRole('link', { name: '[[并存样例]]', exact: true }).click();
    await expect(preview.getByText('有多个文件匹配「并存样例」。请选择要打开的文件。')).toBeVisible();
    await page.setViewportSize({ width: 410, height: 840 });
    const candidate = preview.getByRole('link', { name: new RegExp(second) });
    await candidate.click();
    await expect(preview.getByRole('heading', { name: '第二份样例' })).toBeVisible();

    await page.goto(address.replace('#/?', '#/library?file=' + note + '&'));
    await preview.getByRole('link', { name: '[[尚不存在]]' }).click();
    await expect(preview.getByText('找不到链接目标')).toBeVisible();
    await page.goto(address.replace('#/?', '#/lessons/' + lesson + '?'));
    await page.getByRole('link', { name: '[[随身卡]]', exact: true }).click();
    await expect(preview.getByRole('heading', { name: '查询随身卡.txt' })).toBeVisible();
    await preview.getByRole('link', { name: '[[' + note + '|索引札记]]', exact: true }).click();
    await expect(preview.getByRole('heading', { name: '索引札记' })).toBeVisible();
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

test('note AI sends only confirmed material and applies reviewed drafts and selections', async ({ page }) => {
  test.setTimeout(85_000);
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-note-ai-'));
  const plainSelection = '富文本片段 $5 与 $7';
  const markdownSelection = plainSelection.replaceAll('$', '\\$');
  const exchanged: { instruction?: string; selection?: string; course?: { title: string; sections: unknown[] } }[] = [];
  const provider = createServer((request, response) => {
    request.setEncoding('utf8');
    let collected = '';
    request.on('data', block => { collected += block; });
    request.on('end', () => {
      const prompt = JSON.parse(collected).messages[0].content;
      const material = JSON.parse(prompt.split('\n发送范围 JSON\n')[1]);
      exchanged.push(material);
      const markdown = material.selection === '需要压缩🙂的一句' ? '简明🙂一句'
        : material.selection === markdownSelection ? '**富文本建议**'
          : material.selection ? '可以追加的建议'
            : material.course ? '## 课程草稿建议\n\n- 一条复习提示' : '## 独立草稿建议';
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({ choices: [{ message: { content: JSON.stringify({ markdown }) } }],
        usage: { prompt_tokens: 21, completion_tokens: 13 } }));
    });
  });
  provider.listen(0, '127.0.0.1');
  await once(provider, 'listening');
  const bind = provider.address();
  if (!bind || typeof bind === 'string') throw new Error('Expected a loopback port');
  const service = start(folder);
  try {
    const address = await service.url;
    const url = new URL(address);
    const token = new URLSearchParams(url.hash.split('?')[1]).get('session');
    const headers = { Authorization: 'Bearer ' + token };
    const connection = await page.request.post(new URL('/api/models/connections', url).href, { headers, data: {
      name: '本机笔记建议测试', provider: 'custom', protocol: 'chat-completions',
      endpoint: 'http://127.0.0.1:' + bind.port + '/v1', model: 'note-ai-fixture',
    } });
    expect(connection.ok()).toBeTruthy();
    const assigned = await page.request.patch(new URL('/api/models/roles', url).href, { headers, data: { main: (await connection.json()).id } });
    expect(assigned.ok()).toBeTruthy();

    const lesson = await seedNoteLesson(page, address);
    const notePath = join(folder, 'library', 'notes', lesson + '.md');
    const noteRoute = new URL('/api/lessons/' + lesson + '/note', url).href;
    const original = '# 个人观察\r\n\r\n未发送的个人前文。\r\n\r\n需要压缩🙂的一句\r\n\r\n前缀 ' + markdownSelection + ' 后缀 \\$9。';
    const current = await page.request.get(noteRoute, { headers });
    const write = await page.request.put(noteRoute, { headers, data: { baseRevision: (await current.json()).revision, content: original } });
    expect(write.ok()).toBeTruthy();
    const lessonUrl = address.replace('#/?', '#/lessons/' + lesson + '?');
    await page.goto(lessonUrl);
    await page.getByRole('button', { name: '笔记', exact: true }).click();
    const pane = page.locator('.reading-side .note-panel');
    const field = pane.getByRole('textbox', { name: '本节笔记' });
    const dialog = page.getByRole('dialog');
    await pane.getByRole('button', { name: 'AI 草稿' }).click();
    await dialog.getByRole('textbox', { name: '草稿要求' }).fill('提取一个值得复习的点');
    await expect(dialog.getByRole('checkbox', { name: '使用本节当前讲稿' })).toBeChecked();
    await dialog.getByRole('button', { name: '查看发送范围' }).click();
    await expect(dialog.getByText(/将发送的讲稿/)).toBeVisible();
    expect(exchanged).toHaveLength(0);
    await dialog.getByRole('button', { name: '发送给主模型' }).click();
    await expect(dialog.getByRole('heading', { name: '课程草稿建议' })).toBeVisible();
    expect(readFileSync(notePath, 'utf8')).toBe(original);
    expect(exchanged).toHaveLength(1);
    expect(exchanged[0].instruction).toBe('提取一个值得复习的点');
    expect(exchanged[0].course?.sections.length).toBeGreaterThan(0);
    expect(JSON.stringify(exchanged[0])).not.toContain('未发送的个人前文。');
    await dialog.getByRole('button', { name: '确认追加到笔记' }).click();
    const withDraft = original + '\n\n## 课程草稿建议\n\n- 一条复习提示\n';
    await expect.poll(() => readFileSync(notePath, 'utf8')).toBe(withDraft);

    async function selectSource(text: string) {
      await field.focus();
      await field.evaluate((node, target) => {
        const input = node as HTMLTextAreaElement;
        const start = input.value.indexOf(target);
        if (start < 0) throw new Error('Selection fixture is missing');
        input.setSelectionRange(start, start + target.length);
      }, text);
      await field.press('Shift');
      await pane.getByRole('button', { name: 'AI 选段处理' }).click();
    }

    const sourceMode = pane.getByRole('button', { name: 'Markdown 原文' });
    await sourceMode.click();
    await selectSource('需要压缩🙂的一句');
    await expect(dialog.getByRole('combobox', { name: '处理方式' }).locator('option')).toHaveText(['解释', '扩写', '精简', '整理', '补例']);
    await dialog.getByRole('combobox', { name: '处理方式' }).selectOption('shorten');
    await dialog.getByRole('button', { name: '查看发送范围' }).click();
    expect(exchanged).toHaveLength(1);
    await dialog.getByRole('button', { name: '发送给主模型' }).click();
    await expect(dialog.getByLabel('建议预览').getByText('简明🙂一句', { exact: true })).toBeVisible();
    expect(exchanged[1]).toEqual({ selection: '需要压缩🙂的一句' });
    expect(readFileSync(notePath, 'utf8')).toBe(withDraft);
    await dialog.getByRole('button', { name: '确认替换选段' }).click();
    const shortened = withDraft.replace('需要压缩🙂的一句', '简明🙂一句');
    await expect.poll(() => readFileSync(notePath, 'utf8')).toBe(shortened);

    await pane.getByRole('button', { name: '富文本', exact: true }).click();
    const paragraph = field.locator('p').filter({ hasText: plainSelection });
    await paragraph.click();
    await paragraph.evaluate((node, selection) => {
      const text = node.firstChild!;
      const start = text.textContent!.indexOf(selection);
      if (start < 0) throw new Error('Expected an ordinary text selection');
      window.getSelection()?.setBaseAndExtent(text, start, text, start + selection.length);
      document.dispatchEvent(new Event('selectionchange'));
    }, plainSelection);
    await pane.getByRole('button', { name: 'AI 选段处理' }).click();
    await dialog.getByRole('combobox', { name: '处理方式' }).selectOption('expand');
    await dialog.getByRole('button', { name: '查看发送范围' }).click();
    await dialog.getByRole('button', { name: '发送给主模型' }).click();
    await expect(dialog.getByText('富文本建议', { exact: true })).toBeVisible();
    expect(exchanged[2]).toEqual({ selection: markdownSelection });
    await dialog.getByRole('button', { name: '确认替换选段' }).click();
    await expect.poll(() => readFileSync(notePath, 'utf8')).toContain('前缀 **富文本建议** 后缀 \\$9。');

    await sourceMode.click();
    await selectSource('简明🙂一句');
    await dialog.getByRole('button', { name: '查看发送范围' }).click();
    await dialog.getByRole('button', { name: '发送给主模型' }).click();
    await expect(dialog.getByLabel('建议预览').getByText('可以追加的建议', { exact: true })).toBeVisible();
    const external = readFileSync(notePath, 'utf8') + '\n用户在外部编辑器的补充。';
    writeFileSync(notePath, external, 'utf8');
    await dialog.getByRole('button', { name: '确认替换选段' }).click();
    await expect(dialog.getByRole('alert')).toContainText('本机笔记已在别处修改');
    expect(readFileSync(notePath, 'utf8')).toBe(external);
    await dialog.getByRole('button', { name: '返回笔记' }).click();
    await page.reload();
    await page.getByRole('button', { name: '笔记', exact: true }).click();
    await page.setViewportSize({ width: 410, height: 840 });
    await pane.getByRole('button', { name: '查看最近 AI 建议' }).click();
    await expect(dialog.getByLabel('建议预览').getByText('可以追加的建议', { exact: true })).toBeVisible();
    await dialog.getByRole('button', { name: '确认追加到笔记' }).click();
    await expect.poll(() => readFileSync(notePath, 'utf8')).toBe(external + '\n\n可以追加的建议\n');

    const independent = '727ba3e0-201a-40a8-a750-2a8feceb8be8';
    const created = await page.request.post(new URL('/api/notes', url).href, { headers,
      data: { id: independent, title: '独立试验笔记', lessonIds: [] } });
    expect(created.ok()).toBeTruthy();
    await page.goto(address.replace('#/?', '#/library?file=note-' + independent + '&edit=1&'));
    await page.locator('.library-preview').getByRole('button', { name: 'AI 草稿' }).click();
    await dialog.getByRole('textbox', { name: '草稿要求' }).fill('整理本周练习');
    await dialog.getByRole('button', { name: '查看发送范围' }).click();
    await dialog.getByRole('button', { name: '发送给主模型' }).click();
    await expect(dialog.getByRole('heading', { name: '独立草稿建议' })).toBeVisible();
    expect(exchanged.slice(3)).toEqual([{ selection: '简明🙂一句' }, { instruction: '整理本周练习' }]);
    await dialog.getByRole('button', { name: '确认追加到笔记' }).click();
    await expect.poll(() => readFileSync(join(folder, 'library', 'notes', independent + '.md'), 'utf8')).toBe('## 独立草稿建议\n');
  } finally {
    await stop(service.child);
    await new Promise<void>(resolveClose => provider.close(() => resolveClose()));
    rmSync(folder, { recursive: true, force: true });
  }
});

test('note checks wait for saved revisions, mark the source, and stay in the notes panel', async ({ page }) => {
  test.setTimeout(85_000);
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-note-checks-'));
  const calls: { note: string; verifiedSections: unknown[] }[] = [];
  const quote = '创建索引会删除已有的数据行。';
  const reason = '索引用于定位数据，创建索引本身不会删除数据行。';
  const provider = createServer((request, response) => {
    let collected = '';
    request.setEncoding('utf8');
    request.on('data', block => { collected += block; });
    request.on('end', () => {
      const prompt = JSON.parse(collected).messages[0].content;
      const material = JSON.parse(prompt.split('\n检查材料 JSON\n')[1]);
      calls.push(material);
      const findings = material.note.includes(quote) ? [{ quote, reason }] : [];
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({ choices: [{ message: { content: JSON.stringify({ findings }) } }],
        usage: { prompt_tokens: 28, completion_tokens: 11 } }));
    });
  });
  provider.listen(0, '127.0.0.1');
  await once(provider, 'listening');
  const bind = provider.address();
  if (!bind || typeof bind === 'string') throw new Error('Expected a loopback port');
  const service = start(folder);
  try {
    const address = await service.url;
    const url = new URL(address);
    const session = new URLSearchParams(url.hash.split('?')[1]).get('session');
    const headers = { Authorization: 'Bearer ' + session };
    const connected = await page.request.post(new URL('/api/models/connections', url).href, { headers, data: {
      name: '笔记检查本机复核', provider: 'custom', protocol: 'chat-completions',
      endpoint: 'http://127.0.0.1:' + bind.port + '/v1', model: 'note-check-fixture',
    } });
    expect(connected.ok()).toBeTruthy();
    const assigned = await page.request.patch(new URL('/api/models/roles', url).href, { headers,
      data: { review: (await connected.json()).id } });
    expect(assigned.ok()).toBeTruthy();

    const lesson = await seedNoteLesson(page, address);
    const route = new URL('/api/lessons/' + lesson + '/note', url).href;
    const original = '# 个人记录\r\n\r\n🙂学习遇到一个疑点。\r\n\r\n' + quote + '\r\n';
    const current = await page.request.get(route, { headers });
    const saved = await page.request.put(route, { headers, data: { baseRevision: (await current.json()).revision, content: original } });
    expect(saved.ok()).toBeTruthy();
    const file = join(folder, 'library', 'notes', lesson + '.md');
    await page.goto(address.replace('#/?', '#/lessons/' + lesson + '?'));
    await page.getByRole('button', { name: '笔记', exact: true }).click();
    const pane = page.locator('.reading-side .note-panel');
    const panel = pane.getByRole('region', { name: '笔记检查' });
    await panel.getByRole('button', { name: '开启后台检查' }).click();
    const consent = page.getByRole('dialog', { name: '笔记后台检查' });
    await consent.getByRole('button', { name: '查看检查范围' }).click();
    await expect(consent.getByText('笔记检查本机复核 · note-check-fixture')).toBeVisible();
    await expect(consent.getByText('本次笔记范围')).toBeVisible();
    expect(calls).toHaveLength(0);
    await consent.getByRole('button', { name: '确认开启后台检查' }).click();
    await expect(consent).toHaveCount(0);

    await expect(panel.getByText(reason)).toBeVisible({ timeout: 12_000 });
    await expect(page.getByRole('dialog')).toHaveCount(0);
    expect(calls).toEqual([{ note: original, verifiedSections: [] }]);
    expect(readFileSync(file, 'utf8')).toBe(original);
    const editor = pane.getByRole('textbox', { name: '本节笔记' });
    await expect(editor.locator('.note-check-mark')).toHaveText(quote);
    await page.setViewportSize({ width: 410, height: 840 });
    await panel.getByRole('button', { name: '定位第 5 行' }).click();
    await expect(pane.getByRole('button', { name: 'Markdown 原文' })).toHaveAttribute('aria-pressed', 'true');
    await expect(pane.locator('.note-source-backdrop mark')).toHaveText(quote);
    expect(await editor.evaluate(node => {
      const textarea = node as HTMLTextAreaElement;
      return textarea.value.slice(textarea.selectionStart, textarea.selectionEnd);
    })).toBe(quote);

    await editor.fill('第一版仍在改。');
    await expect(panel.getByRole('status')).toContainText('当前编辑待保存');
    await expect(pane.locator('.note-source-backdrop')).toHaveCount(0);
    const corrected = '创建索引可加快部分查询。\n\n保留的个人例子。';
    await editor.fill(corrected);
    expect(calls).toHaveLength(1);
    await expect.poll(() => calls.map(call => call.note), { timeout: 13_000 }).toEqual([original, corrected]);
    await expect(panel.getByText('本次未发现可以定位的疑点。')).toBeVisible();
    expect(readFileSync(file, 'utf8')).toBe(corrected);
    await panel.getByRole('button', { name: '停止后台检查' }).click();
    await expect(panel.getByRole('button', { name: '开启后台检查' })).toBeVisible();
    const state = await page.request.get(new URL('/api/notes/' + lesson + '/checks', url).href, { headers });
    expect((await state.json()).state).toBe('off');
  } finally {
    await stop(service.child);
    await new Promise<void>(resolveClose => provider.close(() => resolveClose()));
    rmSync(folder, { recursive: true, force: true });
  }
});

test('knowledge graph moves real nodes, edits relationships and restores saved views', async ({ page }) => {
  test.setTimeout(70000);
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-graph-browser-'));
  let service = start(folder);
  const graphPath = '/api/library/graph';
  const consoleErrors: string[] = [];
  page.on('pageerror', error => consoleErrors.push(error.message));

  async function request(path: string, method = 'GET', payload?: object) {
    const address = new URL(await service.url);
    const token = new URLSearchParams(address.hash.split('?')[1]).get('session');
    const response = await fetch(address.origin + path, {
      method, headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
      body: payload === undefined ? undefined : JSON.stringify(payload),
    });
    expect(response.ok, path + ' returned ' + response.status).toBeTruthy();
    return response.json();
  }

  async function saveView(interaction: () => Promise<void>) {
    const saved = page.waitForResponse(response => new URL(response.url()).pathname === graphPath + '/view', { timeout: 6500 });
    await interaction();
    expect((await saved).ok()).toBeTruthy();
  }

  async function dragAt(x: number, y: number, dx: number, dy: number) {
    const saved = page.waitForResponse(response => new URL(response.url()).pathname === graphPath + '/edit', { timeout: 6500 });
    await page.mouse.move(x, y);
    await page.mouse.down();
    await page.mouse.move(x + dx, y + dy, { steps: 10 });
    await page.mouse.up();
    const response = await saved.catch(error => { throw new Error(String(error) + '; page errors: ' + consoleErrors.join(', ')); });
    expect(response.ok(), await response.text()).toBeTruthy();
    return response.json();
  }

  const inspector = page.getByRole('complementary', { name: '图谱节点详情' });
  async function focus(title: string) {
    await page.getByRole('searchbox', { name: '搜索图谱节点' }).fill(title);
    const result = page.getByLabel('图谱搜索结果').getByRole('button', { name: new RegExp(title) });
    await saveView(() => result.click());
    await expect(inspector.getByRole('heading', { name: title, exact: true })).toBeVisible();
  }

  async function openGraph() {
    await page.getByRole('link', { name: '知识库', exact: true }).click();
    await page.getByRole('navigation', { name: '知识库视图' }).getByRole('link', { name: '图谱', exact: true }).click();
    await expect(page.getByLabel('知识图谱画布')).toBeVisible();
  }

  try {
    const reference = await request('/api/files', 'POST', { name: '节拍参考.txt', content: '用稳定的速度数拍。' });
    const practice = await request('/api/files', 'POST', { name: '练习清单.txt', content: '从四分音符开始。' });
    const noteId = '61000000-0000-4000-8000-000000000001';
    const note = 'note-' + noteId;
    const endpoint = '/api/notes/' + noteId;
    await request('/api/notes', 'POST', { id: noteId, title: '节奏划分笔记', lessonIds: [] });
    const initialNote = await request(endpoint);
    const original = '每小节的拍号可以查看 [[' + reference.id + '|本地参考]]。';
    await request(endpoint, 'PUT', { baseRevision: initialNote.revision, content: original });
    const before = await request(graphPath);
    const position = (data: typeof before, id: string) => data.nodes.find((node: { id: string }) => node.id === id).position;

    await page.setViewportSize({ width: 1260, height: 900 });
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.goto(await service.url);
    await openGraph();
    await expect(page.getByRole('status').filter({ hasText: '3 个节点 · 1 条连线' })).toBeVisible();
    await focus('节奏划分笔记');
    await expect(inspector.getByRole('button', { name: /^节拍参考\.txt 指向/ })).toBeVisible();
    const canvas = page.getByLabel('知识图谱画布');
    const bounds = await canvas.boundingBox();
    expect(bounds).toBeTruthy();
    const center = { x: bounds!.x + bounds!.width / 2, y: bounds!.y + bounds!.height / 2 };
    await page.mouse.move(center.x, center.y);
    await expect(page.locator('.graph-hover')).toContainText('节奏划分笔记');
    const moved = await dragAt(center.x, center.y, 66, 43);
    expect(position(moved, note).x - position(before, note).x).toBeGreaterThan(40);
    expect(position(moved, reference.id)).toEqual(position(before, reference.id));

    await inspector.getByRole('button', { name: '添加连线' }).click();
    const linking = page.getByRole('dialog', { name: '添加连线' });
    await linking.getByLabel('选择关联节点').fill('练习清单');
    await linking.getByRole('button', { name: /练习清单.txt/ }).click();
    await linking.getByLabel('连线方向').selectOption('both');
    await linking.getByRole('button', { name: '保存连线' }).click();
    await expect(inspector.getByRole('button', { name: /练习清单.txt.*双向/ })).toBeVisible();
    await inspector.getByRole('button', { name: '编辑连线 练习清单.txt' }).click();
    await page.getByRole('dialog', { name: '编辑连线' }).getByRole('button', { name: '删除连线' }).click();
    await expect(inspector.getByRole('button', { name: /^练习清单\.txt/ })).toHaveCount(0);
    await page.getByRole('button', { name: '撤销图谱操作' }).click();
    await expect(inspector.getByRole('button', { name: /练习清单.txt.*双向/ })).toBeVisible();

    await page.getByRole('button', { name: '分组', exact: true }).click();
    const grouping = page.getByRole('dialog', { name: '图谱分组' });
    await grouping.getByRole('button', { name: '新建分组' }).click();
    await grouping.getByLabel('分组名称').fill('节奏记忆');
    await grouping.getByLabel('向分组添加节点').fill('节拍参考');
    await grouping.getByRole('button', { name: /节拍参考.txt/ }).click();
    await grouping.getByRole('button', { name: '保存分组' }).click();
    await saveView(() => grouping.getByRole('button', { name: '收起 节奏记忆' }).click());
    await grouping.getByRole('button', { name: '关闭', exact: true }).click();
    await expect(inspector.getByRole('heading', { name: '节奏记忆' })).toBeVisible();

    const grouped = await request(graphPath);
    const left = position(grouped, note), right = position(grouped, reference.id);
    const camera = grouped.view.center, zoom = grouped.view.zoom;
    const groupLocation = {
      x: center.x + ((left.x + right.x) / 2 - camera.x) * zoom,
      y: center.y + ((left.y + right.y) / 2 - camera.y) * zoom,
    };
    const movedGroup = await dragAt(groupLocation.x, groupLocation.y, -52, 35);
    expect(position(movedGroup, note).x - left.x).toBeLessThan(-35);
    expect(position(movedGroup, note).x - left.x).toBeCloseTo(position(movedGroup, reference.id).x - right.x);
    await saveView(() => inspector.getByRole('button', { name: '展开分组' }).click());

    await focus('节奏划分笔记');
    await saveView(() => page.getByRole('button', { name: '当前关联' }).click());
    await expect(page.getByRole('status').filter({ hasText: '3 个节点 · 2 条连线' })).toBeVisible();
    await page.getByRole('button', { name: '收藏视角', exact: true }).click();
    const favorite = page.getByRole('dialog', { name: '收藏视角' });
    await favorite.getByLabel('视角名称').fill('上下游参考');
    await favorite.getByRole('button', { name: '保存当前视角' }).click();
    await expect(favorite.getByRole('button', { name: '上下游参考', exact: true })).toBeVisible();
    await favorite.getByRole('button', { name: '关闭', exact: true }).click();

    await inspector.getByRole('button', { name: '隐藏此节点' }).click();
    const hiding = page.getByRole('button', { name: '隐藏节点', exact: true });
    await hiding.click();
    const hidden = page.getByRole('dialog', { name: '隐藏节点' });
    await hidden.getByRole('button', { name: '全选' }).click();
    await hidden.getByRole('button', { name: /恢复所选节点/ }).click();
    await expect(hidden.getByText('没有隐藏节点')).toBeVisible();
    await hidden.getByRole('button', { name: '关闭', exact: true }).click();
    expect((await request(endpoint)).content).toBe(original);
    expect(await request('/api/files/trash')).toEqual([]);

    await page.getByRole('button', { name: '收藏视角', exact: true }).click();
    await saveView(() => favorite.getByRole('button', { name: '上下游参考', exact: true }).click());
    await expect.poll(async () => {
      const { focus, local } = (await request(graphPath)).view;
      return { focus, local };
    }).toEqual({ focus: note, local: true });
    await stop(service.child);
    service = start(folder);
    await page.setViewportSize({ width: 410, height: 850 });
    await page.goto(await service.url);
    await openGraph();
    await expect(page.getByRole('button', { name: '当前关联' })).toHaveAttribute('aria-pressed', 'true');
    await expect(inspector.getByRole('heading', { name: '节奏划分笔记' })).toBeVisible();
    expect(position(await request(graphPath), note)).toEqual(position(movedGroup, note));
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(410);
    await canvas.scrollIntoViewIfNeeded();
    const mobile = await canvas.boundingBox();
    expect(mobile).toBeTruthy();
    await page.mouse.dblclick(mobile!.x + mobile!.width / 2, mobile!.y + mobile!.height / 2);
    await expect(page.getByRole('button', { name: '返回预览' })).toBeVisible();
    expect((await request(endpoint)).content).toBe(original);
    expect(consoleErrors).toEqual([]);
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});


test('knowledge graph exports PNG and SVG and previews JSON changes before applying', async ({ page }) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-graph-transfer-'));
  const service = start(folder);
  const original = '只在原始资料中保留这句话。';

  async function fetchGraph(endpoint = '') {
    const address = new URL(await service.url);
    const token = new URLSearchParams(address.hash.split('?')[1]).get('session');
    const response = await fetch(address.origin + '/api/library/graph' + endpoint, { headers: { Authorization: 'Bearer ' + token } });
    expect(response.ok).toBeTruthy();
    return response.json();
  }

  try {
    await page.setViewportSize({ width: 1120, height: 850 });
    await page.goto(await service.url);
    await page.getByRole('link', { name: '知识库', exact: true }).click();
    await page.getByLabel('导入资料').setInputFiles({
      name: '原件与关系.txt', mimeType: 'text/plain', buffer: Buffer.from(original),
    });
    await expect(page.getByRole('heading', { name: '原件与关系.txt' })).toBeVisible();
    await page.getByRole('link', { name: '图谱', exact: true }).click();
    await expect(page.getByLabel('知识图谱画布')).toBeVisible();
    const before = await fetchGraph();
    const [node] = before.nodes;
    await page.getByRole('button', { name: '导入导出' }).click();
    const transfer = page.getByRole('dialog', { name: '图谱导入与导出' });
    async function download(label: string, extension: string) {
      const promise = page.waitForEvent('download');
      await transfer.getByRole('button', { name: label, exact: true }).click();
      const file = await promise;
      expect(file.suggestedFilename()).toBe('智学-知识图谱.' + extension);
      return readFileSync(await file.path());
    }

    const png = await download('导出 PNG', 'png');
    expect(png.subarray(0, 8).toString('hex')).toBe('89504e470d0a1a0a');
    expect(png.readUInt32BE(16)).toBeGreaterThan(100);
    const svg = (await download('导出 SVG', 'svg')).toString('utf8');
    expect(svg).toContain('原件与关系.txt');
    expect(svg).toMatch(/<(circle|rect)\b/);
    expect(await page.evaluate(source => new DOMParser().parseFromString(source, 'image/svg+xml').querySelector('parsererror') === null, svg)).toBe(true);
    const json = (await download('导出 JSON', 'json')).toString('utf8');
    expect(json).not.toContain(original);
    const archive = JSON.parse(json);
    expect(archive.nodes).toEqual([{ id: node.id, title: node.title, kind: node.kind }]);
    const incoming = { x: node.position.x + 131, y: node.position.y - 47 };
    archive.document.positions[node.id] = incoming;
    await transfer.getByLabel('选择图谱 JSON').setInputFiles({
      name: '导出的知识图谱.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(archive)),
    });
    await transfer.getByRole('button', { name: '预览导入' }).click();
    await expect(transfer.getByRole('status')).toHaveText('匹配 1 个节点 · 跳过 0 个未匹配节点');
    await expect(transfer.getByText('节点位置 · 1 项')).toBeVisible();
    expect((await fetchGraph()).nodes[0].position).toEqual(node.position);
    await transfer.getByRole('button', { name: '确认导入' }).click();
    await expect(transfer).toBeHidden();
    expect((await fetchGraph()).nodes[0].position).toEqual(incoming);
    await page.getByRole('button', { name: '图谱历史' }).click();
    await expect(page.getByRole('dialog', { name: '图谱历史' }).getByText('合并导入')).toBeVisible();
    expect(readFileSync(join(folder, node.path), 'utf8')).toBe(original);
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});


test('graph AI shows dashed candidates and applies only explicitly reviewed links and groups', async ({ page }) => {
  test.setTimeout(70000);
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-graph-ai-browser-'));
  let service = start(folder);
  const received: string[] = [];
  const errors: string[] = [];
  const result = {
    links: [
      { source: 'N1', target: 'N2', both: true, reason: '笔记和资料可相互对照。' },
      { source: 'N2', target: 'N3', both: false, reason: '读完资料可以尝试练习。' },
    ],
    groups: [{ name: '三角形基础', members: ['N1', 'N2'], reason: '两项材料同属三角形基础。' }],
  };
  let release: () => void = () => {};
  const held = new Promise<void>(resolve => { release = resolve; });
  const provider = createServer((request, response) => {
    let body = '';
    request.setEncoding('utf8');
    request.on('data', chunk => { body += chunk; });
    request.on('end', () => {
      const input = JSON.parse(body);
      received.push(input.messages[0].content);
      void held.then(() => {
        response.writeHead(200, { 'Content-Type': 'application/json' });
        response.end(JSON.stringify({
          choices: [{ message: { content: JSON.stringify(result) } }],
          usage: { prompt_tokens: 63, completion_tokens: 38 },
        }));
      });
    });
  });
  provider.listen(0, '127.0.0.1');
  await once(provider, 'listening');
  const address = provider.address();
  if (!address || typeof address === 'string') throw new Error('No mock model address');
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {
    const instrumented = window as Window & { graphCandidateStrokes: number };
    instrumented.graphCandidateStrokes = 0;
    const original = CanvasRenderingContext2D.prototype.setLineDash;
    CanvasRenderingContext2D.prototype.setLineDash = function(segments) {
      if (segments.length && this.canvas.classList.contains('graph-canvas')) instrumented.graphCandidateStrokes++;
      return original.call(this, segments);
    };
  });

  async function request(path: string, method = 'GET', data?: object) {
    const local = new URL(await service.url);
    const token = new URLSearchParams(local.hash.split('?')[1]).get('session');
    const reply = await fetch(local.origin + '/api' + path, {
      method, headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
      body: data === undefined ? undefined : JSON.stringify(data),
    });
    expect(reply.ok, path).toBeTruthy();
    return reply.json();
  }

  async function openGraph(width: number) {
    await page.setViewportSize({ width, height: 880 });
    await page.goto(await service.url);
    await page.getByRole('link', { name: '知识库', exact: true }).click();
    await page.getByRole('link', { name: '图谱', exact: true }).click();
    await expect(page.getByLabel('知识图谱画布')).toBeVisible();
  }

  const graphPath = '/library/graph';
  const toggle = page.getByRole('button', { name: /^图谱待审核/ });
  const panel = page.getByRole('complementary', { name: '图谱待审核' });
  try {
    const model = await request('/models/connections', 'POST', {
      name: '图谱隔离模拟模型', provider: 'custom', protocol: 'chat-completions',
      model: 'synthetic-graph', endpoint: 'http://127.0.0.1:' + address.port + '/v1',
    });
    await request('/models/roles', 'PATCH', { main: model.id });
    const files = [
      ['三角形笔记.md', '复习三角形边与角的关系。'],
      ['余弦资料.md', '资料提供一组可对照的推导。'],
      ['独立练习.md', '先写出各角之间的关系。'],
    ];
    const imported = [];
    for (const [name, content] of files) imported.push(await request('/files', 'POST', { name, content }));
    const privateText = '这份私人文件完全未选用。';
    const privateFile = await request('/files', 'POST', { name: '私人附注.md', content: privateText });
    const initial = await request(graphPath);

    await page.emulateMedia({ reducedMotion: 'reduce' });
    await openGraph(1260);
    await toggle.click();
    await panel.getByRole('button', { name: '发起推荐' }).click();
    const disclosure = page.getByRole('dialog', { name: '发起图谱推荐' });
    for (const [title] of files) {
      await disclosure.getByRole('searchbox', { name: '搜索并选择推荐节点' }).fill(title);
      await disclosure.getByRole('button', { name: new RegExp('^' + title) }).click();
    }
    await disclosure.getByRole('checkbox', { name: '发送选中节点的可用简介' }).check();
    await disclosure.getByRole('button', { name: '预览发送范围' }).click();
    await expect(disclosure.getByText('本次最多 1 次模型调用')).toBeVisible();
    await expect(disclosure.getByText('图谱隔离模拟模型 · synthetic-graph')).toBeVisible();
    for (const [, content] of files) await expect(disclosure.getByText(content, { exact: true })).toBeVisible();
    await expect(disclosure.getByText(privateText)).toHaveCount(0);
    expect(received).toHaveLength(0);
    await disclosure.getByRole('button', { name: '确认发送并排队' }).click();
    await expect(disclosure).toBeHidden();
    await panel.getByRole('button', { name: '返回节点详情' }).click();
    expect(await page.getByRole('dialog').count()).toBe(0);
    await expect.poll(() => received.length).toBe(1);
    const sent = received[0];
    const preflight = JSON.parse(sent.split('\n图谱发送范围 JSON\n')[1]);
    expect(preflight.nodes.map((node: { title: string, excerpt: string }) => [node.title, node.excerpt])).toEqual(files);
    for (const file of [...imported, privateFile]) expect(sent).not.toContain(file.id);
    expect(sent).not.toContain(privateText);
    release();

    await expect(toggle).toHaveText(/图谱待审核\s*3/);
    await expect(page.getByRole('status').filter({ hasText: /0 条连线.*2 条候选连线/ })).toBeVisible();
    await expect.poll(() => page.evaluate(() => (window as Window & { graphCandidateStrokes: number }).graphCandidateStrokes)).toBeGreaterThan(0);
    expect(await page.getByRole('dialog').count()).toBe(0);
    expect((await request(graphPath + '/archive')).edges).toEqual([]);
    expect((await request(graphPath)).groups).toEqual([]);

    await stop(service.child);
    service = start(folder);
    await openGraph(410);
    await toggle.click();
    await expect(panel.getByText('三角形基础', { exact: true })).toBeVisible();
    await panel.getByRole('checkbox', { name: /^选择连线 余弦资料/ }).check();
    await panel.getByRole('button', { name: '拒绝所选' }).click();
    await expect(panel.getByText('已拒绝1 项。', { exact: true })).toBeVisible();
    await expect(toggle).toHaveText(/图谱待审核\s*2/);
    await panel.getByRole('button', { name: '全选本页' }).click();
    await panel.getByRole('button', { name: '确认所选' }).click();
    await expect(panel.getByText('已采用2 项。', { exact: true })).toBeVisible();
    await expect(panel.getByText('暂无待审核候选')).toBeVisible();
    const final = await request(graphPath);
    expect(final.edges).toHaveLength(1);
    expect(final.edges[0]).toMatchObject({ both: true, manual: true });
    expect(new Set([final.edges[0].source, final.edges[0].target])).toEqual(new Set(imported.slice(0, 2).map(file => file.id)));
    expect(final.groups).toHaveLength(1);
    expect(final.groups[0].name).toBe('三角形基础');
    expect(final.nodes.map((node: { position: unknown }) => node.position)).toEqual(
      initial.nodes.map((node: { position: unknown }) => node.position),
    );
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(410);
    expect(received).toHaveLength(1);
    expect(errors).toEqual([]);
  } finally {
    release();
    await stop(service.child);
    provider.closeAllConnections();
    await new Promise<void>(resolve => provider.close(() => resolve()));
    rmSync(folder, { recursive: true, force: true });
  }
});

test('free chat asks the configured model only after review and keeps follow-ups in the same thread', async ({ page }, info) => {
  test.setTimeout(75000);
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-chat-model-'));
  const prompts: string[] = [];
  const model = createServer(async (request, response) => {
    const chunks: Buffer[] = [];
    for await (const chunk of request) chunks.push(chunk);
    const payload = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    prompts.push(payload.messages[0].content);
    response.setHeader('Content-Type', 'application/json');
    response.end(JSON.stringify({ choices: [{ message: { content: prompts.length === 1
      ? '索引根据键计算桶的位置，再在桶中查找记录。'
      : '多个键落到一个桶时，仍需比较实际键。' } }],
      usage: { prompt_tokens: 60, completion_tokens: 20 } }));
  });
  let service: ReturnType<typeof start> | undefined;
  try {
    model.listen(0, '127.0.0.1');
    await once(model, 'listening');
    const address = model.address();
    if (!address || typeof address === 'string') throw new Error('Missing model address');
    const endpoint = 'http://127.0.0.1:' + address.port + '/v1';
    service = start(folder);
    const launched = new URL(await service.url);
    const bearer = new URLSearchParams(launched.hash.split('?')[1]).get('session');
    const headers = { Authorization: 'Bearer ' + bearer };
    const response = await page.request.post(launched.origin + '/api/models/connections', {
      headers, data: { name: '本机自由聊天测试', provider: 'custom', protocol: 'chat-completions', endpoint, model: 'chat-fixture' },
    });
    expect(response.ok(), await response.text()).toBeTruthy();
    const selected = await page.request.patch(launched.origin + '/api/models/roles', {
      headers, data: { main: (await response.json()).id },
    });
    expect(selected.ok(), await selected.text()).toBeTruthy();

    await page.setViewportSize({ width: 430, height: 920 });
    await page.goto(launched.href);
    await page.getByRole('navigation', { name: '主要导航' }).getByRole('link', { name: '聊天', exact: true }).click();
    const style = page.getByRole('combobox', { name: '教学风格', exact: true });
    await style.selectOption('guided');
    await expect(style).toHaveValue('guided');
    await page.getByRole('link', { name: '智学首页' }).click();
    const question = '哈希索引怎样用键找到记录？';
    await page.getByLabel('学习问题或目标').fill(question);
    await page.getByLabel('上传资料').setInputFiles({
      name: '本机附件.txt', mimeType: 'text/plain', buffer: Buffer.from('仅留在附件的校验句'),
    });
    await expect(page.getByRole('button', { name: '移除附件 本机附件.txt' })).toBeVisible();
    const open = page.getByRole('button', { name: '用主模型聊天', exact: true });
    await expect(open).toBeEnabled({ timeout: 4500 });
    await open.click();
    const consent = page.getByRole('dialog', { name: '确认聊天发送范围' });
    await expect(consent.getByText(endpoint + '/chat/completions', { exact: true })).toBeVisible();
    await expect(consent.getByText(question, { exact: true })).toBeVisible();
    await expect(consent.getByText('启发式引导', { exact: true })).toBeVisible();
    expect(prompts).toHaveLength(0);
    await consent.getByRole('button', { name: '关闭', exact: true }).click();
    await expect(page.getByLabel('学习问题或目标')).toHaveValue(question);
    expect(prompts).toHaveLength(0);
    await open.click();
    await consent.getByRole('button', { name: '确认发送', exact: true }).click();
    await expect(page.getByText('索引根据键计算桶的位置，再在桶中查找记录。', { exact: true })).toBeVisible({ timeout: 15000 });
    await expect(page.getByRole('link', { name: '本机附件.txt', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: '学习目标', exact: true })).toHaveCount(0);
    const chatAddress = page.url();

    const followup = '发生哈希冲突时怎么确认键？';
    await page.getByLabel('对话消息').fill(followup);
    await page.getByRole('button', { name: '发送消息' }).click();
    await expect(consent.getByText(question, { exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(430);
    await page.screenshot({ path: info.outputPath('聊天发送范围.png'), fullPage: true });
    await consent.getByRole('checkbox', { name: '附带本对话最近三轮问答' }).uncheck();
    await expect(consent.getByText(question, { exact: true })).toHaveCount(0);
    await consent.getByRole('button', { name: '确认发送', exact: true }).click();
    await expect(page.getByText('多个键落到一个桶时，仍需比较实际键。', { exact: true })).toBeVisible({ timeout: 15000 });
    expect(page.url()).toBe(chatAddress);
    expect(prompts).toHaveLength(2);
    expect(prompts[0]).toContain(question);
    expect(prompts[0]).toContain('教学风格：启发式引导');
    expect(prompts[1]).toContain(followup);
    expect(prompts.some(prompt => prompt.includes('仅留在附件的校验句') || prompt.includes('本机附件.txt'))).toBe(false);
    expect(prompts[1]).not.toContain(question);
    await page.reload();
    await expect(page.getByText('多个键落到一个桶时，仍需比较实际键。', { exact: true })).toBeVisible();
    await page.screenshot({ path: info.outputPath('自由聊天追问.png'), fullPage: true });
    await page.getByRole('button', { name: '整理为学习目标', exact: true }).click();
    await expect(page.getByRole('heading', { name: '学习目标', exact: true })).toBeVisible();
    await page.getByLabel('预期成果', { exact: true }).fill('说明哈希冲突的处理过程');
    await expect(page.getByRole('button', { name: '用主模型生成路线', exact: true })).toBeEnabled();
    await page.getByRole('button', { name: '用主模型整理目标', exact: true }).click();
    const authoring = page.getByRole('dialog', { name: '确认目标整理范围', exact: true });
    await authoring.getByRole('button', { name: '查看整理范围', exact: true }).click();
    await expect(authoring.getByText('多个键落到一个桶时，仍需比较实际键。', { exact: true })).toBeVisible();
    await expect(authoring.getByText('索引根据键计算桶的位置，再在桶中查找记录。', { exact: true })).toBeVisible();
    await authoring.getByRole('button', { name: '取消', exact: true }).click();
    expect(prompts).toHaveLength(2);
  } finally {
    if (service) await stop(service.child);
    model.closeAllConnections();
    model.close();
    rmSync(folder, { recursive: true, force: true });
  }
});

test('teaching style follows the workspace between chat and the course assistant', async ({ page }, info) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-teaching-style-'));
  const service = start(folder);
  try {
    await page.setViewportSize({ width: 430, height: 920 });
    await page.goto(await service.url);
    const main = page.getByRole('navigation', { name: '主要导航' });
    await main.getByRole('link', { name: '聊天', exact: true }).click();
    const style = page.getByRole('combobox', { name: '教学风格', exact: true });
    await expect(style).toHaveValue('direct', { timeout: 4000 });
    await style.selectOption('guided');
    await expect(style).toHaveValue('guided');
    await page.reload();
    await expect(style).toHaveValue('guided');
    await page.screenshot({ path: info.outputPath('聊天教学风格.png'), fullPage: true });

    await page.getByRole('link', { name: '智学首页' }).click();
    await page.getByRole('button', { name: '体验 SQLite 样例' }).click();
    await page.getByRole('button', { name: '提交并生成路线' }).click();
    await expect(page.getByText('3 / 3 节已就绪', { exact: true })).toBeVisible();
    await page.getByRole('link', { name: '开始学习', exact: true }).click();
    await page.getByRole('button', { name: '课内助手', exact: true }).click();
    const assistant = page.getByRole('complementary', { name: '课内助手', exact: true });
    const courseStyle = assistant.getByRole('combobox', { name: '教学风格', exact: true });
    await expect(courseStyle).toHaveValue('guided');
    const message = assistant.getByLabel('对话消息', { exact: true });
    await message.fill('我该用什么例子来练习表和列？');
    await assistant.getByRole('button', { name: '发送消息', exact: true }).click();
    const guided = assistant.getByText(/你能先举一个与这项目标有关的例子/);
    await expect(guided).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(430);
    await page.screenshot({ path: info.outputPath('课内引导示例.png'), fullPage: true });

    await courseStyle.selectOption('direct');
    await expect(courseStyle).toHaveValue('direct');
    await message.fill('从哪个目标开始学习？');
    await assistant.getByRole('button', { name: '发送消息', exact: true }).click();
    await expect(assistant.getByText(/可以对照这项目标/)).toBeVisible();
    await assistant.getByRole('link', { name: '在聊天中打开' }).click();
    await expect(style).toHaveValue('direct');
    await expect(page.getByText('我该用什么例子来练习表和列？', { exact: true })).toBeVisible();
    await expect(page.getByText('从哪个目标开始学习？', { exact: true })).toBeVisible();
  } finally {
    await stop(service.child);
    rmSync(folder, { recursive: true, force: true });
  }
});

test('goal authoring discloses selected evidence and submits a route only after the user fills the gap', async ({ page }, info) => {
  test.setTimeout(60000);
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-goal-authoring-'));
  const material = '练习文件的列包含日期、金额和类别。';
  const clarification = '你希望脚本最后输出哪一种结果？';
  const result = '能独立编写脚本并导出按月份汇总的金额表';
  const prompts: string[] = [];
  const model = createServer(async (request, response) => {
    const chunks: Buffer[] = [];
    for await (const chunk of request) chunks.push(chunk);
    const prompt: string = JSON.parse(Buffer.concat(chunks).toString('utf8')).messages[0].content;
    prompts.push(prompt);
    const authoring = prompt.includes('\n目标卡输入 JSON\n');
    const scope = authoring ? JSON.parse(prompt.split('\n目标卡输入 JSON\n')[1]) : null;
    const source = scope?.sources.find((row: { kind: string }) => row.kind === 'file')?.key;
    const content = authoring ? {
      fields: [
        { key: 'topic', value: 'Python 表格数据整理', origin: 'inference', sources: ['E1'] },
        { key: 'practiceData', label: '练习数据', value: '含日期和金额的支出表', origin: 'suggestion', sources: source ? [source] : [] },
      ], question: clarification,
    } : { stages: [{ name: '表格数据小练习', lessons: [{
      key: 'aggregate', title: '完成表格汇总', outcome: '核对月份和汇总金额', prerequisites: [],
    }] }] };
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({ choices: [{ message: { content: JSON.stringify(content) } }],
      usage: { prompt_tokens: 95, completion_tokens: 80 } }));
  });
  let service: ReturnType<typeof start> | undefined;
  try {
    model.listen(0, '127.0.0.1');
    await once(model, 'listening');
    const address = model.address();
    if (!address || typeof address === 'string') throw new Error('Missing model address');
    const endpoint = `http://127.0.0.1:${address.port}/v1`;
    service = start(folder);
    const launched = new URL(await service.url);
    const bearer = new URLSearchParams(launched.hash.split('?')[1]).get('session');
    const headers = { Authorization: 'Bearer ' + bearer };
    const saved = await page.request.post(launched.origin + '/api/models/connections', {
      headers, data: { name: '本机目标卡测试', provider: 'custom', protocol: 'chat-completions', endpoint, model: 'goal-fixture' },
    });
    expect(saved.ok(), await saved.text()).toBeTruthy();
    const selected = await page.request.patch(launched.origin + '/api/models/roles', {
      headers, data: { main: (await saved.json()).id },
    });
    expect(selected.ok(), await selected.text()).toBeTruthy();

    await page.setViewportSize({ width: 430, height: 920 });
    await page.goto(launched.href);
    await page.getByLabel('学习问题或目标').fill('我想学习用 Python 整理表格。');
    await page.getByRole('button', { name: '开始探索' }).click();
    await page.getByLabel('添加资料').setInputFiles({
      name: '表格练习说明.txt', mimeType: 'text/plain', buffer: Buffer.from(material),
    });
    await expect(page.getByRole('link', { name: '表格练习说明.txt', exact: true })).toBeVisible();
    await page.getByRole('button', { name: '整理为学习目标', exact: true }).click();
    await expect(page.getByRole('heading', { name: '学习目标', exact: true })).toBeVisible();
    await page.getByLabel('已有基础', { exact: true }).fill('已会用表格公式');
    await page.getByRole('button', { name: '用主模型整理目标', exact: true }).click({ timeout: 6000 });
    const scope = page.getByRole('dialog', { name: '确认目标整理范围', exact: true });
    await scope.getByRole('button', { name: '查看整理范围', exact: true }).click();
    await expect(scope.getByText(`${endpoint}/chat/completions`, { exact: true })).toBeVisible();
    await expect(scope.getByText(material, { exact: true })).toHaveCount(0);
    await scope.getByRole('checkbox', { name: '附带资料片段', exact: true }).check();
    await expect(scope.getByRole('button', { name: '确认整理目标', exact: true })).toBeDisabled();
    await scope.getByRole('button', { name: '查看整理范围', exact: true }).click();
    await expect(scope.getByText(material, { exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(430);
    await scope.screenshot({ path: info.outputPath('目标整理发送范围.png') });
    expect(prompts).toHaveLength(0);
    await scope.getByRole('button', { name: '确认整理目标', exact: true }).click();
    const adopt = page.getByRole('button', { name: '采用目标草稿', exact: true });
    await expect(adopt).toBeVisible({ timeout: 15000 });
    const authoring = page.getByRole('region', { name: '模型目标整理' });
    await expect(authoring.getByText(clarification, { exact: true })).toBeVisible();
    await page.evaluate(() => {
      for (const key of Object.keys(sessionStorage)) {
        if (key.startsWith('zhixue-goal-draft-v1:')) sessionStorage.removeItem(key);
      }
    });
    await page.reload();
    await expect(adopt).toBeVisible();
    await expect(page.getByLabel('已有基础', { exact: true })).toBeEmpty();
    expect(prompts).toHaveLength(1);
    expect(prompts[0]).toContain(material);
    await adopt.click();
    await expect(page.getByLabel('预期成果', { exact: true })).toBeEmpty();
    await expect(page.getByLabel('练习数据', { exact: true })).toHaveValue('含日期和金额的支出表');
    await expect(page.getByLabel('已有基础', { exact: true })).toHaveValue('已会用表格公式');
    const goalForm = page.locator('form.goal-form');
    await expect(goalForm.getByText(clarification, { exact: true })).toBeVisible();
    const publish = page.getByRole('button', { name: '用主模型生成路线', exact: true });
    await expect(publish).toBeDisabled();
    expect(new URL(page.url()).hash).toMatch(/^#\/chat\//);
    await page.setViewportSize({ width: 430, height: 1580 });
    await goalForm.screenshot({ path: info.outputPath('模型目标卡.png') });

    const budget = `${launched.origin}/api/generation/settings`;
    const limited = await page.request.put(budget, { headers, data: { dailyLimit: 1, concurrency: 1 } });
    expect(limited.ok(), await limited.text()).toBeTruthy();
    await page.getByRole('button', { name: '用主模型整理目标', exact: true }).click();
    await scope.getByRole('checkbox', { name: '附带资料片段', exact: true }).check();
    await scope.getByRole('button', { name: '查看整理范围', exact: true }).click();
    await scope.getByRole('button', { name: '确认整理目标', exact: true }).click();
    await expect(page.getByText('今日调用已到上限，可在额度恢复后继续。', { exact: true })).toBeVisible();
    await expect(page.getByLabel('学习主题', { exact: true })).toBeDisabled();
    await page.getByText('学习依据（2）', { exact: true }).click();
    await page.getByRole('button', { name: '移除依据 表格练习说明.txt', exact: true }).click();
    const restoreEvidence = page.getByRole('button', { name: '恢复依据 表格练习说明.txt', exact: true });
    await expect(restoreEvidence).toBeVisible();
    await expect(page.getByRole('button', { name: '取消整理', exact: true })).toHaveCount(0);
    await expect(page.getByLabel('学习主题', { exact: true })).toBeEnabled();
    const increased = await page.request.put(budget, { headers, data: { dailyLimit: 2, concurrency: 1 } });
    expect(increased.ok(), await increased.text()).toBeTruthy();
    await restoreEvidence.click();
    await expect(restoreEvidence).toHaveCount(0);
    expect(prompts).toHaveLength(1);

    await page.getByLabel('预期成果', { exact: true }).fill(result);
    await expect(page.getByText(clarification, { exact: true })).toHaveCount(0);
    await page.getByLabel('学习主题', { exact: true }).press('Enter');
    await expect(publish).toBeEnabled();
    expect(new URL(page.url()).hash).toMatch(/^#\/chat\//);
    await publish.click();
    const review = page.getByRole('dialog', { name: '确认模型发送范围', exact: true });
    await review.getByLabel('本任务调用次数上限').fill('1');
    await review.getByRole('button', { name: '查看发送范围', exact: true }).click();
    await expect(review.getByText(result, { exact: true })).toBeVisible();
    expect(prompts).toHaveLength(1);
    await review.getByRole('button', { name: '确认并生成路线', exact: true }).click();
    await expect(page.getByRole('button', { name: '完成表格汇总', exact: true })).toBeVisible({ timeout: 15000 });
    expect(prompts).toHaveLength(2);
    expect(prompts[1]).toContain('制定学习路线');
    expect(prompts[1]).toContain(result);
  } finally {
    if (service) await stop(service.child);
    model.closeAllConnections();
    model.close();
    rmSync(folder, { recursive: true, force: true });
  }
});

test('background task drawer controls model course work from another page', async ({ page }, info) => {
  test.setTimeout(65000);
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-task-drawer-'));
  const wire: string[] = [];
  const evidence = '逐项核对两张表的日期。';
  let finishDraft = () => {};
  let finishReview = () => {};
  const draftReady = new Promise<void>(resolve => { finishDraft = resolve; });
  const reviewReady = new Promise<void>(resolve => { finishReview = resolve; });
  const provider = createServer(async (request, response) => {
    const chunks: Buffer[] = [];
    for await (const chunk of request) chunks.push(chunk);
    const input = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    wire.push(input.model);
    let result: object;
    if (input.model === 'drawer-reviewer') {
      await reviewReady;
      result = { sections: [{
        index: 0, verdict: 'supported', reason: '资料说明了核对方法。',
        citations: [{ source: 'S1', quote: evidence }], dependsOn: [],
      }] };
    } else if (input.messages[0].content.includes('制定学习路线')) {
      result = { stages: [{ name: '练习阶段', lessons: [{ key: 'practice', title: '对照练习表', outcome: '解释一次对照过程' }] }] };
    } else {
      await draftReady;
      result = { sections: [{ title: '核对练习表', text: evidence, checks: ['复述对照步骤'] }] };
    }
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({
      choices: [{ message: { content: JSON.stringify(result) } }],
      usage: { prompt_tokens: 8, completion_tokens: 5 },
    }));
  });
  let service: ReturnType<typeof start> | undefined;
  try {
    provider.listen(0, '127.0.0.1');
    await once(provider, 'listening');
    const address = provider.address();
    if (!address || typeof address === 'string') throw new Error('Missing model address');
    service = start(folder);
    const url = new URL(await service.url);
    const session = new URLSearchParams(url.hash.split('?')[1]).get('session');
    const headers = { Authorization: 'Bearer ' + session };
    const request = async (path: string, method = 'GET', data?: unknown) => {
      const response = await page.request.fetch(url.origin + '/api' + path, { method, headers, data });
      expect(response.ok(), await response.text()).toBeTruthy();
      return response.json();
    };
    const connect = (name: string, model: string) => request('/models/connections', 'POST', {
      name, model, provider: 'custom', protocol: 'chat-completions', endpoint: `http://127.0.0.1:${address.port}/v1`,
    });
    const main = await connect('任务抽屉本机主模型', 'drawer-fixture');
    const review = await connect('任务抽屉本机复核', 'drawer-reviewer');
    await request('/models/roles', 'PATCH', { main: main.id, review: review.id });
    const topic = '表格对照练习';
    const thread = await request('/conversations', 'POST', {
      id: crypto.randomUUID(), message: { id: crypto.randomUUID(), text: `我想学习${topic}。` },
    });
    const goalPath = `/conversations/${thread.id}/goal`;
    const goal = await request(goalPath, 'POST');
    const input = { goalId: goal.id, revision: goal.revision, values: { outcome: '独立核对两张表' } };
    const approved = await request(`${goalPath}/model-preview`, 'POST', { ...input, maxCalls: 1 });
    const submitted = await request(`${goalPath}/submit`, 'POST', { ...input, modelConsent: approved.consent });
    const project = `/projects/${submitted.projectId}`;
    const generation = `${project}/generation`;
    await expect.poll(async () => (await request(generation)).reason, { timeout: 15000 }).toBe('task_budget');
    expect(wire).toHaveLength(1);

    await page.setViewportSize({ width: 1150, height: 880 });
    await page.goto(url.href);
    await page.getByRole('navigation', { name: '主要导航' }).getByRole('link', { name: '知识库', exact: true }).click();
    const location = page.url();
    const opener = page.getByRole('button', { name: '后台任务', exact: true });
    await opener.click();
    const drawer = page.getByRole('complementary', { name: '后台任务列表', exact: true });
    const actions = drawer.getByRole('button', { name: `管理任务 ${topic}`, exact: true });
    const closeDrawer = drawer.getByRole('button', { name: '关闭任务列表', exact: true });
    const stopTask = drawer.getByRole('button', { name: '停止生成任务', exact: true });
    const limitDetails = drawer.getByText('修改任务调用额度', { exact: true });
    const newLimit = drawer.getByLabel('新的调用次数上限');
    const applyLimit = drawer.getByRole('button', { name: '应用额度并继续', exact: true });
    await expect(drawer.getByText('已达到该任务的调用次数上限。', { exact: true })).toBeVisible();
    await actions.click({ timeout: 6000 });
    await expect(drawer.getByText('路线 已生成目录', { exact: true })).toBeVisible();
    await stopTask.click();
    await expect.poll(async () => (await request(generation)).state).toBe('stopped');
    const continueTask = drawer.getByRole('button', { name: '继续生成任务', exact: true });
    await expect(continueTask).toBeVisible();
    const alternate = await connect('任务抽屉临时主模型', 'drawer-alternate');
    await request('/models/roles', 'PATCH', { main: alternate.id });
    expect((await request(generation)).authorizationCurrent).toBe(false);
    await expect(continueTask).toHaveCount(0, { timeout: 6500 });
    await expect(drawer.getByRole('button', { name: '重新确认发送范围', exact: true })).toBeVisible();
    await request('/models/roles', 'PATCH', { main: main.id });
    await expect(continueTask).toBeVisible({ timeout: 6500 });
    await closeDrawer.click();
    await expect(opener).toBeFocused();
    expect(page.url()).toBe(location);
    expect(wire).toHaveLength(1);

    await page.setViewportSize({ width: 430, height: 900 });
    await opener.click();
    await actions.click();
    await limitDetails.click();
    await newLimit.fill('2');
    await applyLimit.click();
    await expect(drawer.getByText('正在生成草稿 1 节', { exact: true })).toBeVisible();
    await closeDrawer.click();
    finishDraft();
    await expect.poll(async () => (await request(generation)).state, { timeout: 15000 }).toBe('awaiting_review');
    expect(wire).toHaveLength(2);
    await opener.click();
    await actions.click();
    await expect(drawer.getByText('等待核验', { exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(430);

    const remove = drawer.getByRole('button', { name: '删除生成任务', exact: true });
    await remove.click();
    const confirmation = page.getByRole('dialog', { name: '删除生成任务', exact: true });
    await expect(confirmation.getByText(/1 份课程草稿/)).toBeVisible();
    await confirmation.getByRole('button', { name: '取消', exact: true }).click();
    const waiting = await request(generation);
    const draft = waiting.steps.find((step: { kind: string }) => step.kind === 'draft');
    expect(draft.state).toBe('awaiting_review');
    const file = await request('/files', 'POST', { name: '对照资料.txt', content: `对照表练习\n${evidence}` });
    const source = await request(`/files/${file.id}?at=2`);
    const reviewScope = { sources: [{ fileId: file.id, revision: source.revision, at: 2 }] };
    const preview = await request(`${project}/course-review/preview`, 'POST', reviewScope);
    await request(`${project}/course-review/authorize`, 'POST', { ...reviewScope, confirmation: preview.confirmation });
    await expect.poll(async () => (await request(generation)).reason).toBe('task_budget');
    await limitDetails.click();
    await newLimit.fill('3');
    await applyLimit.click();
    await expect(drawer.getByText('正在独立复核 1 节', { exact: true })).toBeVisible();
    await drawer.screenshot({ path: info.outputPath('后台任务抽屉.png') });

    await stopTask.click();
    await expect.poll(async () => (await request(generation)).state).toBe('stopped');
    await expect(drawer.getByText('已停止', { exact: true })).toBeVisible();
    await expect(drawer.getByText('正在独立复核 1 节', { exact: true })).toBeVisible();
    finishReview();
    await expect(drawer.getByText('复核完成 1 节', { exact: true })).toBeVisible();
    const finding = await request(`/lessons/${draft.lessonId}/verification`);
    expect(finding.sections[0].state).toBe('verified');
    expect(finding.published).toEqual([]);
    expect((await request(`/lessons/${draft.lessonId}`)).body).toBeNull();

    await remove.click();
    await confirmation.screenshot({ path: info.outputPath('任务删除预览.png') });
    await confirmation.getByRole('button', { name: '确认删除任务', exact: true }).click();
    await expect(drawer.getByText('没有待处理任务', { exact: true })).toBeVisible();
    const saved = await request(project);
    expect(saved.task.status).toBe('deleted');
    expect(saved.lessons.map((lesson: { title: string }) => lesson.title)).toEqual(['对照练习表']);
    expect(page.url()).toBe(location);
    expect(wire).toEqual(['drawer-fixture', 'drawer-fixture', 'drawer-reviewer']);
    expect((await request(generation)).usage).toMatchObject({ calls: 3, outputTokens: 15, reportedCalls: 3 });
  } finally {
    finishDraft();
    finishReview();
    if (service) await stop(service.child);
    provider.closeAllConnections();
    provider.close();
    rmSync(folder, { recursive: true, force: true });
  }
});

test('model usage ledger keeps estimated and unknown connection tests visible', async ({ page }, info) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-model-ledger-'));
  const traffic: string[] = [];
  const provider = createServer(async (request, response) => {
    const chunks: Buffer[] = [];
    for await (const chunk of request) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    traffic.push(body.messages[0].content);
    const usage = traffic.length === 1
      ? { prompt_tokens: 1000, completion_tokens: 500, total_tokens: 1500 }
      : { total_tokens: 75 };
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({ choices: [{ message: { content: 'OK' } }], usage }));
  });
  let service: ReturnType<typeof start> | undefined;
  try {
    provider.listen(0, '127.0.0.1');
    await once(provider, 'listening');
    const bind = provider.address();
    if (!bind || typeof bind === 'string') throw new Error('Missing local model port');
    service = start(folder);
    const url = new URL(await service.url);
    const session = new URLSearchParams(url.hash.split('?')[1]).get('session');
    const response = await page.request.post(url.origin + '/api/models/connections', {
      headers: { Authorization: 'Bearer ' + session },
      data: {
        name: '本机线性计价', provider: 'custom', protocol: 'chat-completions',
        endpoint: `http://127.0.0.1:${bind.port}/v1`, model: 'pricing-fixture',
      },
    });
    expect(response.ok(), await response.text()).toBeTruthy();

    await page.goto(url.href);
    await page.getByRole('link', { name: '管理中心', exact: true }).click();
    await page.getByRole('link', { name: 'API与模型', exact: true }).click();
    const connection = page.getByRole('article', { name: '本机线性计价' });
    await connection.getByRole('button', { name: '配置单价', exact: true }).click({ timeout: 6000 });
    const pricing = page.getByRole('dialog', { name: '配置模型单价' });
    await pricing.getByLabel('币种', { exact: true }).fill('CNY');
    await pricing.getByLabel('每百万输入 Token 单价', { exact: true }).fill('2');
    await pricing.getByLabel('每百万输出 Token 单价', { exact: true }).fill('4');
    await pricing.getByRole('button', { name: '保存单价', exact: true }).click();
    await expect(pricing).toHaveCount(0);
    const probe = page.getByRole('dialog', { name: '测试服务连接' });
    for (let attempt = 0; attempt < 2; attempt++) {
      await connection.getByRole('button', { name: '测试连接', exact: true }).click();
      await probe.getByRole('button', { name: '开始测试', exact: true }).click();
      await expect(probe).toHaveCount(0);
    }
    expect(traffic).toEqual(['Reply with OK.', 'Reply with OK.']);

    await page.getByRole('link', { name: '额度恢复', exact: true }).click();
    const ledger = page.getByRole('region', { name: '模型用量账本', exact: true });
    const records = ledger.getByRole('list', { name: '模型调用记录', exact: true });
    await expect(records.getByText('估算 0.004 CNY', { exact: true })).toBeVisible();
    await expect(records.getByText('费用未知', { exact: true })).toBeVisible();
    await expect(ledger.getByText('共 2 次调用', { exact: true })).toBeVisible();
    await page.setViewportSize({ width: 430, height: 920 });
    await expect(records.getByText('估算 0.004 CNY', { exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(430);
    await ledger.screenshot({ path: info.outputPath('模型用量账本.png') });

    const model = await response.json();
    const third = await page.request.post(url.origin + `/api/models/connections/${model.id}/test`, {
      headers: { Authorization: 'Bearer ' + session }, data: { revision: model.revision },
    });
    expect(third.ok(), await third.text()).toBeTruthy();
    await expect(ledger.getByText('共 3 次调用', { exact: true })).toBeVisible({ timeout: 8500 });
    await ledger.getByLabel('计费日期', { exact: true }).fill('2001-01-01');
    await expect(ledger.getByText('该日期没有模型调用记录', { exact: true })).toBeVisible();
  } finally {
    if (service) await stop(service.child);
    provider.closeAllConnections();
    provider.close();
    rmSync(folder, { recursive: true, force: true });
  }
});

test('money limits keep model traffic paused until a saved task is explicitly continued', async ({ page }, info) => {
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-money-budget-'));
  const traffic: number[] = [];
  const provider = createServer(async (request, response) => {
    const chunks: Buffer[] = [];
    for await (const chunk of request) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    traffic.push(body.max_tokens);
    const route = body.messages[0].content.includes('制定学习路线');
    const result = route
      ? { stages: [{ name: '练习', lessons: [{ key: 'steps', title: '费用额度验证', outcome: '产出可定位的步骤' }] }] }
      : { sections: [{ title: '练习步骤', text: '先准备可定位的资料。', checks: ['来源待核验'] }] };
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify({ choices: [{ message: { content: JSON.stringify(result) } }],
      usage: { prompt_tokens: 30, completion_tokens: route ? 3000 : 90 } }));
  });
  let service: ReturnType<typeof start> | undefined;
  try {
    provider.listen(0, '127.0.0.1');
    await once(provider, 'listening');
    const binding = provider.address();
    if (!binding || typeof binding === 'string') throw new Error('Expected a loopback port');
    service = start(folder);
    const url = new URL(await service.url);
    const headers = { Authorization: 'Bearer ' + new URLSearchParams(url.hash.split('?')[1]).get('session') };
    async function request(path: string, method = 'GET', data?: object) {
      const reply = await page.request.fetch(url.origin + '/api' + path, { headers, method, data });
      expect(reply.ok(), await reply.text()).toBeTruthy();
      return reply.json();
    }
    const connection = await request('/models/connections', 'POST', { name: '额度本机测试',
      provider: 'custom', protocol: 'chat-completions', endpoint: `http://127.0.0.1:${binding.port}/v1`, model: 'money-budget-main' });
    await request(`/models/connections/${connection.id}/pricing`, 'PUT', {
      revision: connection.revision, previousPricing: null,
      pricing: { currency: 'CNY', inputPerMillion: '0', outputPerMillion: '100' },
    });
    await request('/models/roles', 'PATCH', { main: connection.id });
    await page.goto(url.href);
    await page.getByRole('link', { name: '管理中心', exact: true }).click();
    await page.getByRole('link', { name: 'API与模型', exact: true }).click();
    await page.getByLabel('每日金额上限', { exact: true }).fill('0.75', { timeout: 5000 });
    await page.getByLabel('每日金额上限币种', { exact: true }).fill('CNY');
    await page.getByLabel('默认单任务金额上限', { exact: true }).fill('0.65');
    await page.getByLabel('默认单任务金额上限币种', { exact: true }).fill('CNY');
    await page.getByRole('button', { name: '保存生成额度', exact: true }).click();
    await expect(page.getByText('生成额度已保存', { exact: true })).toBeVisible();
    const settings = await request('/generation/settings');
    expect(settings.dailyCostLimit).toEqual({ amount: '0.75', currency: 'CNY' });
    expect(settings.defaultTaskCostLimit).toEqual({ amount: '0.65', currency: 'CNY' });

    const thread = await request('/conversations', 'POST', { id: '00000000-0000-4000-8000-000000000098',
      message: { id: '00000000-0000-4000-8000-000000000099', text: '我想练习活字印刷' } });
    const goal = `/conversations/${thread.id}/goal`;
    const draft = await request(goal, 'POST');
    const input = { goalId: draft.id, revision: draft.revision, values: { outcome: '完成一次试印' } };
    const preview = await request(goal + '/model-preview', 'POST', { ...input, maxCost: { amount: '0.3', currency: 'CNY' } });
    const { projectId } = await request(goal + '/submit', 'POST', { ...input, modelConsent: preview.consent });
    const generation = `/projects/${projectId}/generation`;
    await expect.poll(async () => (await request(generation)).reason).toBe('task_cost');
    expect(traffic).toEqual([]);

    await page.getByRole('link', { name: '额度恢复', exact: true }).click();
    const panel = page.getByRole('region', { name: '额度恢复', exact: true });
    const proceed = panel.getByRole('button', { name: /继续.*路线、课程生成与独立核验/ });
    await expect(proceed).toBeDisabled();
    await expect(panel.getByText(/超出该任务的可用额度/)).toBeVisible();
    await page.setViewportSize({ width: 430, height: 920 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(430);
    await panel.screenshot({ path: info.outputPath('模型金额额度.png') });
    await panel.getByRole('button', { name: '修改任务金额上限', exact: true }).click();
    const editor = page.getByRole('dialog', { name: '修改任务金额上限', exact: true });
    await editor.getByLabel('任务金额上限', { exact: true }).fill('1');
    await editor.getByRole('button', { name: '保存任务金额上限', exact: true }).click();
    await expect(proceed).toBeEnabled();
    expect(traffic).toEqual([]);
    await proceed.click();
    await expect.poll(async () => (await request(generation)).state).toBe('awaiting_review');
    expect(traffic).toEqual([6144, 4096]);
    expect((await request(generation)).usage).toMatchObject({ calls: 2 });
    const ledger = page.getByRole('region', { name: '模型用量账本', exact: true });
    await expect(ledger.getByText('估算 0.309 CNY', { exact: true })).toBeVisible();
  } finally {
    if (service) await stop(service.child);
    provider.closeAllConnections();
    provider.close();
    rmSync(folder, { recursive: true, force: true });
  }
});
