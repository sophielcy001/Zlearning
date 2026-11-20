import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { devices, expect, test } from '@playwright/test';

test.use({ ...devices['Pixel 7'], channel: 'chrome' });

test('two offline note editors can compare and merge without overwriting a newer save', async ({ page, context }, info) => {
  test.setTimeout(80000);
  const dir = mkdtempSync(join(tmpdir(), 'zhixue-local-note-conflict-'));
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
      id: '80000000-0000-4000-8000-000000000053', topic: 'SQLite', outcome: '区分表的行和列', foundation: '电子表格',
    });
    const [lesson] = project.lessons;
    await expect.poll(async () => (await api<{ task: { status: string } }>('/projects/' + project.id)).task.status,
      { timeout: 15000 }).toBe('ready');
    const note = '/lessons/' + lesson.id + '/note';
    const initial = '# 学习笔记\n共同段落\n';
    const firstSave = initial.replace('共同段落', '第一页改稿');
    const secondDraft = initial.replace('共同段落', '第二页改稿');
    const newerSave = firstSave + '第一页再次补写\n';
    const initialMerge = firstSave + '第二页改稿\n';
    const finalMerge = initialMerge + '第一页再次补写\n';
    await api(note, 'PUT', { content: initial });

    const mobile = new URL('/mobile/#/?session=' + session, service).href;
    const open = '打开课程 ' + lesson.title;
    await page.goto(mobile);
    await page.getByRole('button', { name: '下载课程 ' + lesson.title, exact: true }).click();
    await page.getByRole('button', { name: open, exact: true }).click();
    const peer = await context.newPage();
    await peer.goto(mobile);
    await peer.getByRole('button', { name: open, exact: true }).click();
    const firstEditor = page.getByRole('textbox', { name: '本节笔记' });
    const draft = peer.getByRole('textbox', { name: '本节笔记' });
    await expect(firstEditor).toHaveValue(initial);
    await expect(draft).toHaveValue(initial);
    await context.setOffline(true);

    await draft.fill(secondDraft);
    await firstEditor.fill(firstSave);
    await page.getByRole('button', { name: '保存到本机' }).click();
    await expect(page.getByText('笔记已保存到本机', { exact: true })).toBeVisible();

    await peer.getByRole('button', { name: '保存到本机' }).click();
    const comparison = peer.getByRole('textbox', { name: '本机已保存的版本' });
    await expect(comparison, 'the stale editor needs a recovery path that keeps its draft').toHaveValue(firstSave);
    await expect(comparison).toHaveJSProperty('readOnly', true);
    await expect(draft).toHaveValue(secondDraft);
    await expect.poll(() => peer.evaluate(() =>
      document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
    await peer.screenshot({ path: info.outputPath('离线笔记双页核对.png'), fullPage: true });
    await peer.getByRole('button', { name: '返回课程' }).click();
    await expect(draft).toHaveValue(secondDraft);

    await firstEditor.fill(newerSave);
    await page.getByRole('button', { name: '保存到本机' }).click();
    await expect(page.getByText('笔记已保存到本机', { exact: true })).toBeVisible();
    const saveMerge = peer.getByRole('button', { name: '保存合并结果' });
    await draft.fill(initialMerge);
    await saveMerge.click();
    await expect(comparison).toHaveValue(newerSave);
    await expect(draft).toHaveValue(initialMerge);

    await draft.fill(finalMerge);
    await saveMerge.click();
    await expect(comparison).toHaveCount(0);
    await expect(peer.getByText('笔记已保存到本机', { exact: true })).toBeVisible();
    await expect(peer.getByRole('alert')).toHaveCount(0);
    await peer.reload();
    await peer.getByRole('button', { name: open, exact: true }).click();
    await expect(draft).toHaveValue(finalMerge);
    await peer.getByRole('button', { name: '同步', exact: true }).click();
    await context.setOffline(false);
    await expect(peer.getByText('0 项待同步', { exact: true })).toBeVisible({ timeout: 15000 });
    expect((await api<{ content: string }>(note)).content).toBe(finalMerge);

    writeFileSync(info.outputPath('离线两页笔记合并.json'), JSON.stringify({
      displayedSavedText: true, keptUnsavedDraft: true, leavingDidNotOverwrite: true,
      refreshedComparisonAfterAnotherSave: true, clearedBlockedNavigationNoticeAfterSave: true,
      reopenedMergedNoteOffline: true, desktopMatchesMergedNote: true,
    }, null, 2) + '\n');
  } finally {
    child.stdin.end();
    const killer = setTimeout(() => child.kill(), 5000);
    await done;
    clearTimeout(killer);
    rmSync(dir, { recursive: true, force: true, maxRetries: 6, retryDelay: 100 });
  }
});
