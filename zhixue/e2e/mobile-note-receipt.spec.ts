import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { devices, expect, test } from '@playwright/test';

test.use({ ...devices['Pixel 7'], channel: 'chrome' });

test('a lost note conflict reply returns the desktop resolution on the first resend', async ({ page, context }, info) => {
  test.setTimeout(80000);
  const dir = mkdtempSync(join(tmpdir(), 'zhixue-note-resolution-'));
  mkdirSync(join(dir, 'library'));
  const bundled = process.env.ZHIXUE_TEST_SERVICE;
  const python = process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python';
  const launch = bundled ? [] : ['-m', 'backend.launch'];
  const child = spawn(bundled || resolve(python), [...launch, '--port', '0', '--stdio', '--data', dir],
    { stdio: ['pipe', 'pipe', 'inherit'], windowsHide: true });
  const done = once(child, 'close');
  try {
    const lines = createInterface({ input: child.stdout });
    const [address] = await once(lines, 'line');
    lines.close();
    const service = new URL(address as string);
    const session = new URLSearchParams(service.hash.split('?')[1]).get('session');
    const headers = { Authorization: 'Bearer ' + session };
    const api = async <T,>(path: string, method = 'GET', data?: object): Promise<T> => {
      const reply = await context.request.fetch(new URL('/api' + path, service).href, { headers, method, data });
      expect(reply.ok(), await reply.text()).toBe(true);
      return reply.json() as Promise<T>;
    };
    const project = await api<{ id: string; lessons: { id: string; title: string }[] }>('/projects', 'POST', {
      id: '80000000-0000-4000-8000-000000000052', topic: 'SQLite', outcome: '区分表的行和列', foundation: '电子表格',
    });
    const [lesson] = project.lessons;
    await expect.poll(async () => (await api<{ task: { status: string } }>('/projects/' + project.id)).task.status,
      { timeout: 15000 }).toBe('ready');
    const note = '/lessons/' + lesson.id + '/note';
    const initial = '# 学习笔记\n共同段落\n末行\n';
    const desktop = initial.replace('共同段落', '电脑编辑');
    const phone = initial.replace('共同段落', '手机编辑');
    const reconciled = initial.replace('共同段落', '电脑已核对两份改稿');
    const before = await api<{ revision: string }>(note, 'PUT', { content: initial });

    await page.goto(new URL('/mobile/#/?session=' + session, service).href);
    const open = page.getByRole('button', { name: '打开课程 ' + lesson.title, exact: true });
    await page.getByRole('button', { name: '下载课程 ' + lesson.title, exact: true }).click();
    await open.click();
    const editor = page.getByRole('textbox', { name: '本节笔记' });
    await expect(editor).toHaveValue(initial);
    await context.setOffline(true);
    await api(note, 'PUT', { content: desktop, baseRevision: before.revision });
    await editor.fill(phone);
    await page.getByRole('button', { name: '保存到本机' }).click();
    await page.getByRole('button', { name: '同步', exact: true }).click();
    await expect(page.getByText('1 项待同步', { exact: true })).toBeVisible();

    const attempts: { id: string; state: string; conflictId?: string }[] = [];
    const receipts: string[] = [];
    page.on('request', request => {
      if (request.method() === 'GET' && new URL(request.url()).pathname.startsWith('/api/mobile/changes/')) receipts.push(request.url());
    });
    await page.route('**/api/mobile/changes', async route => {
      if (route.request().method() !== 'POST') return route.continue();
      const response = await route.fetch();
      const result = await response.json() as typeof attempts[number];
      attempts.push(result);
      if (attempts.length === 1) return route.abort('failed');
      return route.fulfill({ response });
    });

    await context.setOffline(false);
    await expect.poll(() => attempts[0]?.state, { timeout: 15000 }).toBe('conflict');
    const conflict = note + '/conflicts/' + attempts[0].conflictId;
    const issue = await api<{ current: { revision: string }; draft: { revision: string }; external: { revision: string } }>(conflict);
    await api(conflict, 'POST', {
      action: 'merge', content: reconciled, currentRevision: issue.current.revision,
      draftRevision: issue.draft.revision, externalRevision: issue.external.revision,
    });
    if (attempts.length === 1) await page.getByRole('button', { name: '同步到电脑', exact: true }).click();
    await expect.poll(() => attempts[1]?.state, {
      message: 'the first resend after desktop resolution must return the saved note', timeout: 12000,
    }).toBe('saved');
    expect(attempts.map(item => item.state)).toEqual(['conflict', 'saved']);
    expect(attempts[1].id).toBe(attempts[0].id);
    await expect(page.getByText('0 项待同步', { exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: '本机待处理' })).toHaveCount(0);
    expect(receipts).toHaveLength(0);
    await context.setOffline(true);
    await page.reload();
    await page.getByRole('button', { name: '课程', exact: true }).click();
    await open.click();
    await expect(editor).toHaveValue(reconciled);

    writeFileSync(info.outputPath('笔记冲突回执丢失后首次重发.json'), JSON.stringify({
      conflictReplyDropped: true, desktopResolvedBeforeResend: true, sameOperationId: true,
      savedOnFirstResend: true, extraReceiptQueries: 0, reopenedResolvedNoteOffline: true,
    }, null, 2) + '\n');
  } finally {
    child.stdin.end();
    const killer = setTimeout(() => child.kill(), 5000);
    await done;
    clearTimeout(killer);
    rmSync(dir, { recursive: true, force: true, maxRetries: 6, retryDelay: 100 });
  }
});
