import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { devices, expect, test } from '@playwright/test';

test.use({ ...devices['Pixel 7'], channel: 'chrome' });

test('a delayed course download preserves a note already synced from another tab', async ({ page, context }, info) => {
  test.setTimeout(120000);
  const dir = mkdtempSync(join(tmpdir(), 'zhixue-course-reply-'));
  mkdirSync(join(dir, 'library'));
  const python = process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python';
  const bundled = process.env.ZHIXUE_TEST_SERVICE;
  const launch = bundled ? [] : ['-m', 'backend.launch'];
  const child = spawn(bundled || resolve(python), [...launch, '--port', '0', '--stdio', '--data', dir],
    { stdio: ['pipe', 'pipe', 'inherit'], windowsHide: true });
  const done = once(child, 'close');
  let releaseDownload!: () => void;
  let releaseRemoved: (() => void) | undefined;
  const allowDownload = new Promise<void>(resolve => { releaseDownload = resolve; });
  try {
    const lines = createInterface({ input: child.stdout });
    const [address] = await once(lines, 'line');
    lines.close();
    const service = new URL(address as string);
    const session = new URLSearchParams(service.hash.split('?')[1]).get('session');
    const headers = { Authorization: 'Bearer ' + session };
    const api = async <T,>(path: string, method = 'GET', data?: object): Promise<T> => {
      const result = await context.request.fetch(new URL('/api' + path, service).href, { headers, method, data });
      expect(result.ok(), await result.text()).toBe(true);
      return await result.json() as T;
    };
    const project = await api<{ id: string; lessons: { id: string; title: string }[] }>('/projects', 'POST', {
      id: '80000000-0000-4000-8000-000000000051', topic: 'SQLite', outcome: '区分表的行和列', foundation: '电子表格',
    });
    const [lesson] = project.lessons;
    await expect.poll(async () => (await api<{ task: { status: string } }>('/projects/' + project.id)).task.status,
      { timeout: 15000 }).toBe('ready');
    const note = '/lessons/' + lesson.id + '/note';
    const original = '# 本节笔记\n下载前内容\n';
    const edited = '# 本节笔记\n另一页已同步的内容\n';
    await api(note, 'PUT', { content: original });

    const peer = await context.newPage();
    const mobile = new URL('/mobile/#/?session=' + session, service).href;
    const download = '下载课程 ' + lesson.title;
    const open = '打开课程 ' + lesson.title;
    let captured = false;
    await page.route('**/api/mobile/lessons/' + lesson.id, async route => {
      const response = await route.fetch();
      const snapshot = await response.json();
      captured = response.ok() && snapshot.note.content === original;
      await allowDownload;
      await route.fulfill({ response });
    }, { times: 1 });
    await Promise.all([page.goto(mobile), peer.goto(mobile)]);
    await expect(peer.getByRole('button', { name: download, exact: true })).toBeEnabled();
    await page.getByRole('button', { name: download, exact: true }).click();
    await expect.poll(() => captured, { timeout: 12000 }).toBe(true);

    await peer.getByRole('button', { name: download, exact: true }).click();
    await peer.getByRole('button', { name: open, exact: true }).click();
    const editor = peer.getByRole('textbox', { name: '本节笔记' });
    await expect(editor).toHaveValue(original);
    await editor.fill(edited);
    await peer.getByRole('button', { name: '保存到本机' }).click();
    await peer.getByRole('button', { name: '同步', exact: true }).click();
    await peer.getByRole('button', { name: '同步到电脑' }).click();
    await expect(peer.getByText('0 项待同步', { exact: true })).toBeVisible({ timeout: 15000 });
    const synced = await api<{ content: string; revision: string }>(note);
    expect(synced.content).toBe(edited);

    const delayed = page.waitForResponse(response =>
      response.url().endsWith('/api/mobile/lessons/' + lesson.id) && response.ok());
    releaseDownload();
    await delayed;
    await page.getByRole('button', { name: open, exact: true }).click();
    const reader = page.getByRole('textbox', { name: '本节笔记' });
    await expect(reader, 'the delayed download must leave the synced note intact').toHaveValue(edited);
    await context.setOffline(true);
    await page.reload();
    await page.getByRole('button', { name: open, exact: true }).click();
    await expect(reader).toHaveValue(edited);

    await context.setOffline(false);
    const newest = edited + '电脑再次更新\n';
    await api(note, 'PUT', { content: newest, baseRevision: synced.revision });
    await page.getByRole('button', { name: '返回课程' }).click();
    await page.getByRole('button', { name: '移除下载 ' + lesson.title }).click();
    await page.getByRole('button', { name: download, exact: true }).click();
    await page.getByRole('button', { name: open, exact: true }).click();
    await expect(reader).toHaveValue(newest);

    await page.getByRole('button', { name: '返回课程' }).click();
    await page.getByRole('button', { name: '移除下载 ' + lesson.title }).click();
    await peer.reload();
    await expect(peer.getByRole('button', { name: download, exact: true })).toBeEnabled();
    let capturedBeforeRemoval = false;
    const allowRemoved = new Promise<void>(resolve => { releaseRemoved = resolve; });
    await page.route('**/api/mobile/lessons/' + lesson.id, async route => {
      const response = await route.fetch();
      capturedBeforeRemoval = response.ok();
      await allowRemoved;
      await route.fulfill({ response });
    }, { times: 1 });
    await page.getByRole('button', { name: download, exact: true }).click();
    await expect.poll(() => capturedBeforeRemoval).toBe(true);
    await peer.getByRole('button', { name: download, exact: true }).click();
    await expect(peer.getByRole('button', { name: open, exact: true })).toBeVisible();
    await peer.getByRole('button', { name: '移除下载 ' + lesson.title }).click();
    await expect(peer.getByRole('button', { name: download, exact: true })).toBeEnabled();
    const afterRemoval = page.waitForResponse(response =>
      response.url().endsWith('/api/mobile/lessons/' + lesson.id) && response.ok());
    releaseRemoved!();
    await afterRemoval;
    await expect(page.getByRole('button', { name: download, exact: true }),
      'the delayed download must leave the explicit removal intact').toBeEnabled();
    await expect(page.getByRole('button', { name: open, exact: true })).toHaveCount(0);
    await context.setOffline(true);
    await page.reload();
    await expect(page.getByText('此设备还没有下载内容。', { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: open, exact: true })).toHaveCount(0);

    await context.setOffline(false);
    const afterDownload = newest + '移除后重新下载取得的版本\n';
    const afterRemovalNote = await api<{ revision: string }>(note);
    await api(note, 'PUT', { content: afterDownload, baseRevision: afterRemovalNote.revision });
    await page.getByRole('button', { name: download, exact: true }).click();
    await page.getByRole('button', { name: open, exact: true }).click();
    await expect(reader).toHaveValue(afterDownload);

    writeFileSync(info.outputPath('迟到下载与同步笔记.json'), JSON.stringify({
      oldDownloadHeld: captured, otherTabSyncedBeforeRelease: true, latestNoteReadWithNetworkOff: true,
      redownloadAfterRemovalReadsLatestNote: true,
      delayedReplyDidNotRestoreRemovedLesson: true, removalPreservedAfterOfflineReload: true,
    }, null, 2) + '\n', 'utf8');
  } finally {
    releaseDownload();
    releaseRemoved?.();
    await context.setOffline(false);
    child.stdin.end();
    await done;
    rmSync(dir, { recursive: true, force: true, maxRetries: 6, retryDelay: 100 });
  }
});

