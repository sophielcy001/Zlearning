import { spawn, type ChildProcess } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { once } from 'node:events';
import { mkdtempSync, readFileSync, readdirSync, realpathSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { expect, test } from '@playwright/test';

const python = process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python';

function launch(reports: string, entry = ['-m', 'acceptance']) {
  const child = spawn(resolve(python), [...entry, '--stdio', '--reports', reports], {
    stdio: ['pipe', 'pipe', 'inherit'], windowsHide: true,
  });
  const address = new Promise<string>((resolveAddress, reject) => {
    child.once('error', reject);
    child.once('exit', (code) => reject(new Error('Acceptance service exited (' + code + ')')));
    createInterface({ input: child.stdout }).once('line', resolveAddress);
  });
  return { child, address };
}

async function stop(child: ChildProcess) {
  if (child.exitCode !== null || child.signalCode !== null) return;
  const exited = once(child, 'close');
  child.stdin?.end();
  const [code] = await exited;
  expect(code, 'Acceptance service should exit cleanly').toBe(0);
}

test('independent acceptance opens in Chrome after the first automatic port is blocked', async ({ page }) => {
  const temp = realpathSync(tmpdir());
  const reports = realpathSync(mkdtempSync(join(temp, 'zhixue-acceptance-port-')));
  expect(dirname(reports)).toBe(temp);
  const service = launch(reports, ['-c', `
import socket
from acceptance.__main__ import main

class FirstRestrictedPort(socket.socket):
    fixture_pending = True

    def bind(self, address):
        if address != ("127.0.0.1", 0) or not FirstRestrictedPort.fixture_pending:
            return super().bind(address)
        FirstRestrictedPort.fixture_pending = False
        for port in (6000, 6566, 10080):
            try:
                return super().bind(("127.0.0.1", port))
            except OSError:
                continue
        raise OSError("No restricted test port was available")

socket.socket = FirstRestrictedPort
raise SystemExit(main())
`]);
  try {
    const reply = await page.goto(await service.address);
    expect(reply?.status()).toBe(200);
    await expect(page.getByRole('heading', { name: '智学独立验收' })).toBeVisible();
  } finally {
    try {
      await stop(service.child);
    } finally {
      rmSync(reports, { recursive: true, force: true });
    }
  }
});

test('runs the real learning service with simulated models and replays evidence after restart', async ({ page }, info) => {
  test.setTimeout(210000);
  const reports = mkdtempSync(join(tmpdir(), 'zhixue-acceptance-e2e-'));
  const errors: string[] = [];
  const writes: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('request', (request) => {
    if (request.method() !== 'GET') writes.push(new URL(request.url()).pathname);
  });
  let service = launch(reports);
  try {
    await page.goto(await service.address);
    await expect(page.getByRole('heading', { name: '智学独立验收' })).toBeVisible();
    const unverified = page.getByRole('button', { name: '真实模型样本联调 未验证' });
    await expect(unverified).toBeVisible();
    await expect(page.getByRole('button', { name: '模型金额额度 未验证' })).toBeVisible();
    await expect(page.getByRole('button', { name: '来源变更与课程修订 未验证' })).toBeVisible();
    await expect(page.getByRole('button', { name: '目标与路线调整 未验证' })).toBeVisible();
    await expect(page.getByRole('button', { name: '知识库检索定位 未验证' })).toBeVisible();
    await expect(page.getByRole('button', { name: '单页识别授权与范围 未验证' })).toBeVisible();
    await page.getByRole('button', { name: '运行核心验收' }).click();
    const status = page.locator('#run-status');
    await expect(status).toHaveAttribute('data-state', /^(passed|failed)$/, { timeout: 150000 });
    const runFile = readdirSync(reports).find(file => file.endsWith('.json'));
    const run = runFile ? JSON.parse(readFileSync(join(reports, runFile), 'utf8')) : null;
    const failure = run?.records.find((record: { status: string }) => record.status === 'failed');
    expect(run?.state, failure?.reason ?? run?.reason).toBe('passed');
    const history = page.getByRole('complementary', { name: '运行历史' });
    await expect(history.getByRole('button')).toHaveCount(1);
    const finding = page.getByRole('button', { name: '争议与依据不足 通过' });
    if (!(await finding.isVisible())) await page.getByText('展开分支（3）', { exact: true }).click();
    await finding.click();
    await expect(page.locator('#step-output')).toContainText('dependency_blocked');
    await expect(page.locator('#step-elapsed')).toHaveText(/^\d+ ms$/);
    const timepoint = page.getByLabel('按实际执行顺序回看');
    await timepoint.selectOption('pause');
    await expect(page.locator('#step-output')).toContainText('stopped');
    await page.getByRole('button', { name: '下一节点' }).click();
    await expect(page.locator('#step-output')).toContainText('本次复核请求数');
    await timepoint.selectOption('library-search');
    await expect(page.locator('#step-output')).toContainText('中文复习计划');
    await expect(page.locator('#step-output')).toContainText('第 3 行');
    await timepoint.selectOption('library-semantic-first');
    await expect(page.locator('#step-output')).toContainText('发布前索引状态');
    await expect(page.locator('#step-output')).toContainText('disabled');
    await expect(page.locator('#step-output')).toContainText('preparing');
    await expect(page.locator('#step-output')).toContainText('原件 SHA-256');
    await timepoint.selectOption('library-reindex');
    await expect(page.locator('#step-output')).toContainText('索引重建后原件逐字一致');
    await expect(page.locator('#step-output')).toContainText('第 2 行');
    await timepoint.selectOption('library-anchor');
    await expect(page.locator('#step-output')).toContainText('HTTP 409');
    await expect(page.locator('#step-output')).toContainText('HTTP 404');
    await timepoint.selectOption('library-wiki');
    await expect(page.locator('#step-output')).toContainText('同名候选数量');
    await expect(page.locator('#step-output')).toContainText('双链改名资料.md');
    await expect(page.locator('#step-output')).toContainText('第 3 行');
    await timepoint.selectOption('library-original-return');
    await expect(page.locator('#step-output')).toContainText('原件离开时的状态');
    await expect(page.locator('#step-output')).toContainText('文件 SHA-256');
    await expect(page.locator('#step-output')).toContainText('第 2 行');
    await timepoint.selectOption('ocr');
    await expect(page.locator('#step-output')).toContainText('只有 PDF 第 2 页 PNG');
    await expect(page.locator('#step-output')).toContainText('本机识别文字');
    await timepoint.selectOption('ocr-conflict');
    await expect(page.locator('#step-output')).toContainText('HTTP 409');
    await expect(page.locator('#step-output')).toContainText('原件逐字一致');
    await timepoint.selectOption('schedule-delay');
    await expect(page.locator('#step-output')).toContainText('评分延迟');
    await expect(page.locator('#step-output')).toContainText('当前待办数量');
    await timepoint.selectOption('library-trash');
    await expect(page.locator('#step-output')).toContainText('完整与相对旧路径状态');
    await expect(page.locator('#step-output')).toContainText('trashed');
    await timepoint.selectOption('backup-trash');
    await expect(page.locator('#step-output')).toContainText('整库恢复后按普通确认找回');
    await expect(page.locator('#step-output')).toContainText('加密回退快照可预览');
    await timepoint.selectOption('backup-trash-review');
    await expect(page.locator('#step-output')).toContainText('改路径的旧确认');
    await expect(page.locator('#step-output')).toContainText('重启后原件与双链读回一致');
    await timepoint.selectOption('schedule-report');
    await expect(page.locator('#step-output')).toContainText('评分前状态');
    await expect(page.locator('#step-output')).toContainText('ungraded');
    await expect(page.locator('#step-output')).toContainText('评分日正式复习数');
    await timepoint.selectOption('budget-task', { timeout: 5000 });
    await expect(page.locator('#step-output')).toContainText('task_cost');
    await timepoint.selectOption('budget-resume');
    await expect(page.locator('#step-output')).toContainText('0.309');
    await timepoint.selectOption('budget-daily');
    await expect(page.locator('#step-output')).toContainText('daily_cost');
    await timepoint.selectOption('budget-rollover');
    await expect(page.locator('#step-output')).toContainText('次日自动继续');
    await expect(page.locator('#step-output')).toContainText('额度足够的单任务仍待确认');
    await expect(page.locator('#step-output')).toContainText('再次前台检查新增提醒');
    await expect(page.getByRole('button', { name: '模型金额额度 通过' })).toBeVisible();
    await page.getByRole('button', { name: '每日额度拦截 通过' }).click();
    await expect(page.locator('#step-output')).toContainText('daily_cost');
    await expect(page.getByRole('button', { name: '来源变更与课程修订 通过' })).toBeVisible();
    await timepoint.selectOption('revision-preview');
    await expect(page.locator('#step-output')).toContainText('HTTP 409');
    await expect(page.locator('#step-output')).toContainText('新增模型请求数');
    await timepoint.selectOption('revision-keep');
    await expect(page.locator('#step-output')).toContainText('旧版仍有效');
    await timepoint.selectOption('revision-accept');
    await expect(page.locator('#step-output')).toContainText('复习记录保留');
    await expect(page.locator('#step-output')).toContainText('已发布引用');
    await timepoint.selectOption('revision-anchor');
    await expect(page.locator('#step-output')).toContainText('原笔记逐字一致');
    await page.locator('#step-title').scrollIntoViewIfNeeded();
    await expect(page.locator('#step-title')).toBeInViewport();
    await page.screenshot({ path: info.outputPath('摘录重新关联.png') });
    await expect(page.getByRole('button', { name: '目标与路线调整 通过' })).toBeVisible();
    await timepoint.selectOption('route-goal-preview');
    await expect(page.locator('#step-output')).toContainText('HTTP 409');
    await expect(page.locator('#step-output')).toContainText('预览后仍是原版本');
    await timepoint.selectOption('route-goal-confirm');
    await expect(page.locator('#step-output')).toContainText('目标版本');
    await expect(page.locator('#step-output')).toContainText('原复习排期保留');
    await timepoint.selectOption('route-edit-preview');
    await expect(page.locator('#step-output')).toContainText('待巩固');
    await expect(page.locator('#step-output')).toContainText('选做');
    await timepoint.selectOption('route-practice-conflict');
    await expect(page.locator('#step-output')).toContainText('同名文件原样保留');
    await expect(page.locator('#step-output')).toContainText('实战文件写入失败');
    await page.locator('#step-title').scrollIntoViewIfNeeded();
    await page.screenshot({ path: info.outputPath('路线实战冲突.png') });
    await timepoint.selectOption('route-practice-retry');
    await expect(page.locator('#step-output')).toContainText('知识库登记');
    await expect(page.locator('#step-output')).toContainText('Markdown SHA-256');
    const routeSteps = run.records.filter((record: { step: string }) => record.step.startsWith('route-'));
    expect(routeSteps.map((record: { step: string }) => record.step)).toEqual([
      'route-updates', 'route-goal-preview', 'route-goal-confirm', 'route-edit-preview',
      'route-practice-conflict', 'route-practice-retry', 'route-edit-restart',
      'route-assistant-known', 'route-assistant-earlier', 'route-assistant-goal',
      'route-group-preview', 'route-group-confirm', 'route-group-restart',
    ]);
    const routeNode = (name: string) => routeSteps.find((record: { step: string }) => record.step === name).output;
    expect(routeNode('route-assistant-known')).toMatchObject({ 标记: '已会', 当前路线版本: 4, 原复习排期保留: true });
    expect(routeNode('route-assistant-earlier')).toMatchObject({ 原位置: 4, 新位置: 3, 当前路线版本: 5 });
    expect(routeNode('route-assistant-goal')).toMatchObject({ 目标版本: 3, 当前路线版本: 6, 原目标日期保留: true });
    expect(routeNode('route-group-preview')).toMatchObject({ 确认前路线不变: true, 不同内容的确认: 'HTTP 409' });
    expect(routeNode('route-group-preview').旧测评范围).toHaveLength(2);
    expect(routeNode('route-group-preview').新测评范围[0].课程).toHaveLength(2);
    expect(routeNode('route-group-confirm')).toMatchObject({
      历史测评尝试数: 1, 当前路线版本: 8, 分支调整沿用阶段测评: true,
      必学课分支: '基础核对', 未学选做实战: '核对新阶段的查询结果',
    });
    await timepoint.selectOption('route-group-preview');
    await expect(page.locator('#step-output')).toContainText('新测评范围');
    await expect(page.locator('#step-output')).toContainText('HTTP 409');
    await page.locator('#step-title').scrollIntoViewIfNeeded();
    await page.screenshot({ path: info.outputPath('阶段范围回看.png') });
    expect(JSON.stringify(routeSteps)).not.toMatch(/"(?:confirmation|token|session|authorization)"\s*:/i);
    await expect(status).toHaveText('通过 · 61 节点');
    const fieldValue = (label: string) => page.locator('#step-output').getByText(label, { exact: true })
      .locator('xpath=following-sibling::dd[1]');
    await timepoint.selectOption('restart-unsent');
    await expect(fieldValue('旧调用记录')).toHaveText('not_sent');
    await expect(fieldValue('计入调用')).toHaveText('2');
    await timepoint.selectOption('restart-sent');
    await expect(fieldValue('等待人工时费用未知的调用')).toHaveText('1');
    await expect(fieldValue('计入调用')).toHaveText('3');
    await unverified.click();
    await expect(page.locator('#step-status')).toHaveText('未验证');
    await expect(page.getByRole('heading', { name: '实际运行' })).toBeHidden();

    const artifact = Promise.all([page.waitForEvent('download'), page.getByRole('button', { name: '导出报告' }).click()]);
    const [download] = await artifact;
    const path = await download.path();
    const report = readFileSync(path, 'utf8');
    expect(report).toContain('真实模型样本联调');
    expect(report).toContain('SQLite 完整性');
    expect(report).toContain('归档大小');
    expect(report).toContain('恢复后保留的逐日估算');
    expect(report).toContain('重启后定位与历史');
    expect(report).toContain('重启后读回路线');
    expect(report).toContain('同名文件原样保留');
    expect(report).toContain('重启后读回阶段与测评');
    expect(report).toContain('分支调整沿用阶段测评');
    expect(report).toContain('知识库检索定位');
    expect(report).toContain('语义索引首次启用');
    expect(report).toContain('提交前后关键词原文行一致');
    expect(report).toContain('索引重建后原件逐字一致');
    expect(report).toContain('原件归位与再次保存');
    expect(report).toContain('重启后原件归位读回');
    expect(report).toContain('延迟评分与单轮待办');
    expect(report).toContain('重启恢复未发送预留');
    expect(report).toContain('重启核对已发送请求');
    expect(report).toContain('单页识别授权与范围');
    expect(report).toContain('校对冲突与索引');
    for (const privateLine of ['本机摘录私有尾行 0051', '这一行模拟未授权的笔记内容。', '本机路线验收私有笔记']) {
      expect(report).not.toContain(privateLine);
      expect(JSON.stringify(run)).not.toContain(privateLine);
    }
    writeFileSync(info.outputPath('智学独立验收报告.md'), report);
    const token = new URLSearchParams(new URL(await service.address).hash.slice(1)).get('session')!;
    expect(report).not.toContain(token);
    expect(JSON.stringify(run)).not.toContain(token);
    const [identity] = readdirSync(reports).filter((file) => file.endsWith('.json'));
    const saved = readFileSync(join(reports, identity));
    writeFileSync(info.outputPath(identity), saved);
    await expect(history.getByRole('button').first()).toHaveAttribute('data-identity', identity.slice(0, -5));

    await stop(service.child);
    service = launch(reports);
    await page.setViewportSize({ width: 410, height: 850 });
    await page.goto(await service.address);
    await expect(history.getByRole('button')).toHaveCount(1);
    await expect(page.locator('#run-status')).toHaveText('通过 · 61 节点');
    await timepoint.selectOption('library-semantic-first');
    await expect(page.locator('#step-output')).toContainText('提交前后关键词原文行一致');
    await timepoint.selectOption('restart-sent');
    await expect(fieldValue('需要人工继续')).toHaveText('是');
    await timepoint.selectOption('library-anchor');
    await expect(page.locator('#step-output')).toContainText('HTTP 404');
    await timepoint.selectOption('schedule-delay');
    await expect(page.locator('#step-output')).toContainText('实际提交日');
    await timepoint.selectOption('restart');
    await expect(page.locator('#step-output')).toContainText('正式复习报告读回一致');
    await expect(page.locator('#step-output')).toContainText('双链与反向原文读回一致');
    await page.locator('#step-title').scrollIntoViewIfNeeded();
    await timepoint.selectOption('library-original-restart');
    await expect(page.locator('#step-output')).toContainText('重启后原文与双链一致');
    await expect(page.locator('#step-output')).toContainText('SQLite 完整性');
    await page.locator('#step-title').scrollIntoViewIfNeeded();
    await page.screenshot({ path: info.outputPath('原件归位重启回看.png') });
    await timepoint.selectOption('backup-trash-review');
    await expect(page.locator('#step-output')).toContainText('重启后原件与双链读回一致');
    await page.locator('#step-title').scrollIntoViewIfNeeded();
    await page.screenshot({ path: info.outputPath('换机回收证据回看.png') });
    await timepoint.selectOption('restart');
    await page.screenshot({ path: info.outputPath('知识库与复习重启回看.png') });
    await timepoint.selectOption('ocr-conflict');
    await expect(page.locator('#step-output')).toContainText('撤销后请求');
    await history.getByRole('button').click();
    await page.getByRole('button', { name: '加密备份与恢复 通过' }).click();
    await expect(page.locator('#step-output')).toContainText('已固定回退点');
    await expect(page.locator('#step-title')).toBeInViewport();
    await page.getByLabel('按实际执行顺序回看').selectOption('budget-restore');
    await expect(page.locator('#step-output')).toContainText('restore_pending');
    await expect(page.locator('#step-output')).toContainText('0.309 CNY');
    await page.getByLabel('按实际执行顺序回看').selectOption('revision-restore');
    await expect(page.locator('#step-output')).toContainText('恢复前临时位置');
    await expect(page.locator('#step-output')).toContainText('恢复后原链接');
    await page.locator('#step-title').evaluate((title) => title.scrollIntoView({ block: 'start' }));
    await expect(page.locator('#step-title')).toBeInViewport();
    await page.screenshot({ path: info.outputPath('课程换版恢复回看.png') });
    await timepoint.selectOption('route-edit-restart');
    await expect(page.locator('#step-output')).toContainText('原课程文件一致');
    await expect(page.locator('#step-output')).toContainText('原笔记修订一致');
    await expect(page.locator('#step-output')).toContainText('原复习排期保留');
    await page.locator('#step-title').evaluate((title) => title.scrollIntoView({ block: 'start' }));
    await expect(page.locator('#step-title')).toBeInViewport();
    await page.screenshot({ path: info.outputPath('目标与路线重启回看.png') });
    await timepoint.selectOption('route-group-restart');
    await expect(page.locator('#step-output')).toContainText('旧测评尝试保留');
    await expect(page.locator('#step-output')).toContainText('原复习排期保留');
    await expect(page.locator('#step-output')).toContainText('SQLite 完整性');
    await page.locator('#step-title').evaluate(title => title.scrollIntoView({ block: 'start' }));
    await expect(page.locator('#step-title')).toBeInViewport();
    await page.screenshot({ path: info.outputPath('阶段重组重启回看.png') });
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(410);
    expect(readFileSync(join(reports, identity))).toEqual(saved);
    expect(writes).toEqual(['/api/runs']);
    expect(errors).toEqual([]);
  } finally {
    await stop(service.child);
    rmSync(reports, { recursive: true, force: true });
  }
});

const fixtureEntry = 'import sys\nsys.path.insert(0, "tests")\n'
  + 'from test_acceptance_live import model_fixture\n'
  + 'with model_fixture("sqlite") as (address, _, markers):\n'
  + '    print(address, flush=True)\n    sys.stdin.buffer.read()\n';

function launchModelFixture() {
  const child = spawn(resolve(python), ['-c', fixtureEntry], {
    stdio: ['pipe', 'pipe', 'inherit'], windowsHide: true,
  });
  const address = new Promise<string>((resolveAddress, reject) => {
    child.once('error', reject);
    child.once('exit', (code) => reject(new Error('Model fixture exited (' + code + ')')));
    createInterface({ input: child.stdout }).once('line', resolveAddress);
  });
  return { child, address };
}

test('requires consent for real mode and saves independent manual findings without rerunning models', async ({ page }, info) => {
  test.setTimeout(90000);
  const reports = mkdtempSync(join(tmpdir(), 'zhixue-live-e2e-'));
  const service = launch(reports);
  const provider = launchModelFixture();
  const writes: string[] = [];
  const pageErrors: string[] = [];
  const key = 'e2e-acceptance-key-keep-in-memory';
  page.on('pageerror', (error) => pageErrors.push(error.message));
  page.on('request', (request) => {
    if (request.method() !== 'GET') writes.push(request.method() + ' ' + new URL(request.url()).pathname);
  });
  try {
    await page.setViewportSize({ width: 410, height: 850 });
    await page.goto(await service.address);
    await page.getByRole('button', { name: '联调真实模型', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '联调真实模型' });
    await expect(dialog.getByText(/最多 12 次模型请求/)).toBeVisible();
    const sample = dialog.getByLabel('验收样本');
    await sample.selectOption('si');
    await dialog.getByText('查看发送范围和公开资料', { exact: true }).click();
    await expect(dialog.getByText(/^SI 前缀 centi/)).toBeVisible();
    await sample.selectOption('sqlite');

    const mainModel = dialog.getByRole('group', { name: '主模型', exact: true });
    const reviewModel = dialog.getByRole('group', { name: '独立复核模型', exact: true });
    for (const [name, model] of [['main-test', mainModel], ['review-test', reviewModel]] as const) {
      await model.getByLabel('服务商').selectOption('custom');
      await model.getByLabel('API 地址').fill(await provider.address);
      await model.getByLabel('模型名称').fill(name);
      await model.getByLabel('API 密钥').fill(key + '-' + name);
    }
    const confirm = dialog.getByRole('button', { name: '确认发送并开始联调' });
    const consent = dialog.getByRole('checkbox', { name: /我同意/ });
    await expect(confirm).toBeDisabled();
    await consent.check();
    await expect(confirm).toBeEnabled();
    await mainModel.getByLabel('模型名称').fill('main-test-edit');
    await expect(consent).not.toBeChecked();
    expect(writes).toEqual([]);
    await mainModel.getByLabel('模型名称').fill('main-test');
    await dialog.getByText('配置本次金额上限（可选）', { exact: true }).click();
    const limit = dialog.getByLabel('本次金额上限');
    await limit.fill('5');
    for (const role of ['主模型单价', '独立复核模型单价']) {
      const prices = dialog.getByRole('group', { name: role, exact: true });
      await prices.getByLabel('每百万输入 token').fill('2');
      await prices.getByLabel('每百万输出 token').fill('4');
    }
    await consent.check();
    await limit.fill('10');
    await expect(consent).not.toBeChecked();
    await expect(confirm).toBeDisabled();
    expect(writes).toEqual([]);
    await consent.check();
    await confirm.click();
    await expect(dialog).toBeHidden();
    await expect(page.locator('#run-status')).toHaveText('通过 · 11 节点', { timeout: 55000 });
    await expect(page.getByRole('button', { name: '来源变更与课程修订 未验证' })).toBeVisible();
    await expect(page.getByRole('button', { name: '目标与路线调整 未验证' })).toBeVisible();
    await expect(page.getByRole('button', { name: '知识库检索定位 未验证' })).toBeVisible();
    await expect(page.locator('#run-description')).toContainText('模型接口联调');
    await expect(page.locator('#run-description')).toContainText('含回环连接');

    const timeline = page.getByLabel('按实际执行顺序回看');
    await timeline.selectOption('real-usage');
    await expect(page.locator('#step-output')).toContainText('费用未知的调用');
    await expect(page.locator('#step-output')).toContainText('"模型分工": "主模型"');
    await expect(page.locator('#step-output')).toContainText('"模型分工": "独立复核模型"');
    await expect(page.locator('#step-output')).toContainText('账本开始时间（UTC）');
    await expect(page.locator('#step-output')).toContainText('结算记录时间（UTC）');
    await page.getByRole('button', { name: '查看人工抽查' }).click();
    await expect(page.locator('#step-output')).toContainText('ORDER BY');
    const quality = page.getByRole('region', { name: '人工抽查' });
    await expect(quality.getByText('人工抽查未完成', { exact: true })).toBeVisible();
    await quality.getByLabel('事实与例题').selectOption('confirmed');
    await quality.getByLabel('引文与原始来源').selectOption('confirmed');
    await quality.getByLabel('适用条件与争议片段').selectOption('insufficient');
    await quality.getByRole('button', { name: '保存抽查结论' }).click();
    await expect(quality.getByRole('status')).toContainText('已记录');

    const download = page.waitForEvent('download');
    await page.getByRole('button', { name: '导出报告' }).click();
    const report = readFileSync(await (await download).path(), 'utf8');
    expect(report).toContain('模型接口联调');
    expect(report).toContain('接入范围 含回环连接');
    expect(report).toContain('"金额控制"');
    expect(report).toContain('"amount": "10"');
    expect(report).toContain('"estimated": "0.00081"');
    expect(report).toContain('"费用未知的调用": 0');
    expect(report).toContain('"发送前取消或拦截": 0');
    expect(report).toContain('"尚未发送的预留": 0');
    expect(report).toContain('"模型分工": "独立复核模型"');
    expect(report).toContain('"账本开始时间（UTC）"');
    expect(report).toContain('适用条件与争议片段 · 证据不足');
    expect(report).not.toContain(key);
    writeFileSync(info.outputPath('loopback-sample-report.md'), report, 'utf8');
    const billingDownload = page.waitForEvent('download');
    await page.getByRole('button', { name: '导出账单核对表' }).click();
    const ledger = await billingDownload;
    expect(ledger.suggestedFilename()).toMatch(/zhixue-acceptance-.+-billing\.csv/);
    const csv = readFileSync(await ledger.path(), 'utf8');
    expect(csv.startsWith('\uFEFF运行 ID,样本,接入范围')).toBe(true);
    expect(csv).toContain('账单条目,账单币种,账单金额,核对结论');
    expect(csv).toContain('独立复核模型');
    expect(csv).toContain('含回环连接');
    expect(csv).not.toContain(key);
    writeFileSync(info.outputPath('loopback-sample-billing.csv'), csv, 'utf8');
    await page.reload();
    await expect(page.locator('#run-description')).toContainText('模型接口联调');
    await expect(page.locator('#run-description')).toContainText('含回环连接');
    await expect(page.getByRole('button', { name: '导出账单核对表' })).toBeVisible();
    await timeline.selectOption('real-save');
    await expect(quality.getByLabel('适用条件与争议片段')).toHaveValue('insufficient');
    await page.getByRole('button', { name: '联调真实模型', exact: true }).click();
    await expect(mainModel.getByLabel('API 密钥')).toHaveValue('');
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(410);
    expect(writes).toEqual(['POST /api/real-runs', expect.stringMatching(/^PUT \/api\/runs\/[^/]+\/quality$/)]);
    expect(pageErrors).toEqual([]);
  } finally {
    await stop(service.child);
    if (provider.child.exitCode === null && provider.child.signalCode === null) {
      const exited = once(provider.child, 'exit');
      provider.child.stdin?.end();
      await exited;
    }
    rmSync(reports, { recursive: true, force: true });
  }
});

test('an interrupted live sample remains reviewable and exportable after the acceptance service restarts', async ({ page }, info) => {
  const temp = realpathSync(tmpdir());
  const reports = realpathSync(mkdtempSync(join(temp, 'zhixue-interrupted-e2e-')));
  expect(dirname(reports)).toBe(temp);
  const identity = randomUUID();
  const startedAt = new Date().toISOString();
  const publication = { step: 'real-publish', status: 'passed', input: {}, output: { 课程版本: 1, 正文: 'SELECT 教学样本已发布。' },
    reason: null, startedAt, finishedAt: startedAt, elapsedMs: 8 };
  const unfinished = { step: 'real-answer', status: 'running', input: {}, output: {},
    reason: null, startedAt, finishedAt: null, elapsedMs: null };
  const archived = { id: identity, mode: 'real', sample: 'sqlite', state: 'running', reason: null,
    startedAt, finishedAt: null, records: [publication, unfinished] };
  writeFileSync(join(reports, identity + '.json'), JSON.stringify(archived));
  let service = launch(reports);
  const changes: string[] = [];
  page.on('request', request => {
    if (request.method() !== 'GET') changes.push(request.method() + ' ' + new URL(request.url()).pathname);
  });
  try {
    await page.goto(await service.address);
    const run = page.locator('#run-status');
    await expect(run).toHaveText('已中断 · 1 节点');
    await page.getByRole('button', { name: '查看人工抽查' }).click();
    const quality = page.getByRole('region', { name: '人工抽查' });
    await quality.getByLabel('事实与例题').selectOption('confirmed');
    await quality.getByLabel('引文与原始来源').selectOption('insufficient');
    await quality.getByLabel('适用条件与争议片段').selectOption('disputed');
    await quality.getByRole('button', { name: '保存抽查结论' }).click();
    await expect(quality.getByRole('status')).toContainText('已记录');

    await stop(service.child);
    service = launch(reports);
    await page.goto(await service.address);
    await expect(run).toHaveText('已中断 · 1 节点');
    const history = page.getByRole('complementary', { name: '运行历史' });
    await expect(history.getByRole('button')).toHaveCount(1);
    await expect(history.getByRole('button').first()).toHaveAttribute('data-identity', identity);
    await page.getByRole('button', { name: '查看人工抽查' }).click();
    await expect(quality.getByRole('status')).toContainText('课程第 1 版');
    await expect(quality.getByLabel('适用条件与争议片段')).toHaveValue('disputed');
    await expect(page.locator('#step-output')).toContainText('SELECT 教学样本已发布。');

    const pendingDownload = page.waitForEvent('download');
    await page.getByRole('button', { name: '导出报告' }).click();
    const report = readFileSync(await (await pendingDownload).path(), 'utf8');
    expect(report).toContain('结论 已中断');
    expect(report).toContain('适用条件与争议片段 · 有偏差');
    expect(report).toContain('SELECT 教学样本已发布。');
    expect(changes).toEqual(['PUT /api/runs/' + identity + '/quality']);
    writeFileSync(info.outputPath('中断样本人工抽查报告.md'), report);
    writeFileSync(info.outputPath('中断样本存档.json'), readFileSync(join(reports, identity + '.json')));
  } finally {
    await stop(service.child);
    rmSync(reports, { recursive: true, force: true });
  }
});

test('history switches keep exports and manual review bound to the loaded sample', async ({ page }) => {
  const temp = realpathSync(tmpdir());
  const reports = realpathSync(mkdtempSync(join(temp, 'zhixue-acceptance-selection-')));
  expect(dirname(reports)).toBe(temp);
  const sqlite = randomUUID();
  const si = randomUUID();
  function record(id: string, sample: 'sqlite' | 'si', startedAt: string, version: number, body: string) {
    const common = { status: 'passed', startedAt, finishedAt: startedAt, reason: null, elapsedMs: 8, input: {} };
    const run = { id, mode: 'real', sample, state: 'passed', startedAt, finishedAt: startedAt, reason: null,
      connectionScope: '含回环连接', records: [
        { ...common, step: 'real-publish', output: { 课程版本: version, 正文: body } },
        { ...common, step: 'real-usage', output: { 计入调用: 1, 调用记录: [{ 阶段: 'route', 模型分工: '主模型',
          状态: 'succeeded', 输入用量: 20, 输出用量: 10 }] } },
      ],
    };
    writeFileSync(join(reports, id + '.json'), JSON.stringify(run));
  }
  record(sqlite, 'sqlite', '2026-11-19T16:02:00Z', 7, 'SQLite 的独立样本正文');
  record(si, 'si', '2026-11-19T16:01:00Z', 3, '单位换算的独立样本正文');
  const firstArchive = readFileSync(join(reports, sqlite + '.json'));
  const service = launch(reports);
  let releaseRead: () => void = () => {};
  const writes: string[] = [];
  page.on('request', request => {
    if (request.method() !== 'GET') writes.push(request.method() + ' ' + new URL(request.url()).pathname);
  });
  try {
    const pending = new Promise<void>(resolve => { releaseRead = resolve; });
    const target = '**/api/runs/' + si;
    await page.route(target, async route => {
      await pending;
      await route.fulfill({ status: 409, json: { detail: '这次运行的记录无法读取。' } });
    }, { times: 1 });
    await page.goto(await service.address);
    const history = page.getByRole('complementary', { name: '运行历史' });
    const first = history.getByRole('button', { name: /SQLite 筛选与排序/ });
    const second = history.getByRole('button', { name: /米与厘米的换算/ });
    await expect(first).toHaveAttribute('aria-pressed', 'true');
    const report = page.getByRole('button', { name: '导出报告' });
    const billing = page.getByRole('button', { name: '导出账单核对表' });
    const review = page.getByRole('button', { name: '查看人工抽查' });
    const quality = page.getByRole('region', { name: '人工抽查' });
    await review.click();
    await expect(quality).toBeVisible();
    await expect(report).toBeEnabled();
    await expect(billing).toBeVisible();

    await second.click();
    await expect(second).toHaveAttribute('aria-pressed', 'true');
    await expect(report).toBeDisabled({ timeout: 1500 });
    await expect(billing).toBeHidden();
    await expect(quality).toBeHidden();
    await expect(page.locator('#evidence')).toBeHidden();
    releaseRead();
    await expect(page.getByRole('alert')).toContainText('请求暂时无法完成');
    await expect(report).toBeDisabled();
    await expect(review).toBeHidden();

    await second.click();
    await expect(page.locator('#run-description')).toContainText('米与厘米的换算');
    await expect(page.getByRole('alert')).toBeHidden();
    await review.click();
    await expect(page.locator('#step-output')).toContainText('单位换算的独立样本正文');
    await quality.getByLabel('事实与例题').selectOption('confirmed');
    await quality.getByLabel('引文与原始来源').selectOption('insufficient');
    await quality.getByLabel('适用条件与争议片段').selectOption('disputed');
    await quality.getByRole('button', { name: '保存抽查结论' }).click();
    await expect(quality.getByRole('status')).toContainText('课程第 3 版');
    for (const [button, suffix, marker] of [[report, '.md', '单位换算的独立样本正文'],
      [billing, '-billing.csv', '米与厘米的换算']] as const) {
      const download = page.waitForEvent('download');
      await button.click();
      const file = await download;
      expect(file.suggestedFilename()).toBe('zhixue-acceptance-' + si + suffix);
      const content = readFileSync(await file.path(), 'utf8');
      expect(content).toContain(si);
      expect(content).toContain(marker);
      expect(content).not.toContain(sqlite);
    }
    expect(writes).toEqual(['PUT /api/runs/' + si + '/quality']);
    expect(readFileSync(join(reports, sqlite + '.json'))).toEqual(firstArchive);
  } finally {
    releaseRead();
    try { await stop(service.child); }
    finally { rmSync(reports, { recursive: true, force: true }); }
  }
});
