import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdirSync, mkdtempSync, realpathSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { devices, expect, test, type Page } from '@playwright/test';

test.use({ ...devices['Pixel 7'], channel: 'chrome' });

const button = (page: Page, name: string) => page.getByRole('button', { name, exact: true });
const answer = (page: Page, question: number, choice: number) => page.locator('fieldset.question').nth(question).getByRole('radio').nth(choice);
const save = (page: Page) => button(page, '保存草稿');
const submit = (page: Page) => button(page, '提交作答');
const openReview = async (page: Page) => {
  await button(page, '复习').click();
  await button(page, '打开题组 区分行与列').click();
};

const offlineReview = test.extend<{ peer: Page }>({
  peer: async ({ page, context }, use) => {
    const dir = mkdtempSync(join(tmpdir(), 'zhixue-two-review-'));
    mkdirSync(join(dir, 'library'));
    const bundled = process.env.ZHIXUE_TEST_SERVICE;
    const python = process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python';
    const child = spawn(bundled || resolve(python), [...(bundled ? [] : ['-m', 'backend.launch']), '--port', '0', '--stdio', '--data', dir],
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
        const reply = await context.request.fetch(new URL('/api' + path, service).href, { method, data, headers });
        expect(reply.ok(), await reply.text()).toBe(true);
        return reply.json() as Promise<T>;
      };
      const project = await api<{ id: string; lessons: { id: string }[] }>('/projects', 'POST', {
        id: '80000000-0000-4000-8000-000000000054', topic: 'SQLite', outcome: '区分表的行和列', foundation: '电子表格',
      });
      await expect.poll(async () => (await api<{ task: { status: string } }>('/projects/' + project.id)).task.status,
        { timeout: 15000 }).toBe('ready');
      await api('/lessons/' + project.lessons[0].id + '/learned', 'PUT', { learned: true });
      await api('/demo/next-day', 'POST');
      const mobile = new URL('/mobile/#/?session=' + session, service).href;
      await page.goto(mobile);
      await button(page, '复习').click();
      await button(page, '下载题组 区分行与列').click();
      await button(page, '打开题组 区分行与列').click();
      const peer = await context.newPage();
      await peer.goto(mobile);
      await openReview(peer);
      await context.setOffline(true);
      await use(peer);
    } finally {
      child.stdin.end();
      const killer = setTimeout(() => child.kill(), 5000);
      try { await done; } finally { clearTimeout(killer); }
      const normalized = realpathSync(dir);
      if (dirname(normalized) !== realpathSync(tmpdir()) || !basename(normalized).startsWith('zhixue-two-review-')) {
        throw new Error('Unexpected review fixture directory.');
      }
      rmSync(normalized, { recursive: true, force: true, maxRetries: 6, retryDelay: 100 });
    }
  },
});

offlineReview('independent answers merge across offline review editors and survive reload', async ({ page, peer }) => {
  await answer(page, 0, 0).check();
  await answer(peer, 1, 1).check();
  await save(page).click();
  await expect(save(page)).toBeEnabled();
  await save(peer).click();
  await expect(answer(peer, 0, 0), 'a stale save must keep the answer saved by the first tab').toBeChecked();
  await expect(answer(peer, 1, 1)).toBeChecked();
  await peer.reload();
  await openReview(peer);
  await expect(answer(peer, 0, 0)).toBeChecked();
  await expect(answer(peer, 1, 1)).toBeChecked();
});

offlineReview('a stale submission with different answers to the same question requires an explicit choice', async ({ page, peer }, info) => {
  await answer(page, 0, 0).check();
  await answer(peer, 0, 1).check();
  await answer(peer, 1, 1).check();
  await save(page).click();
  await expect(save(page)).toBeEnabled();
  await submit(peer).click();
  const savedAnswer = peer.getByRole('group', { name: '第 1 题答案核对' });
  await expect(savedAnswer).toBeVisible();
  await expect(answer(peer, 0, 1)).toBeChecked();
  await expect(answer(peer, 1, 1)).toBeChecked();
  await expect(peer.getByText('作答已保存到本机', { exact: true })).toHaveCount(0);
  await button(peer, '返回复习').click();
  await expect(savedAnswer).toBeVisible();

  await button(peer, '采用本机第 1 题答案').click();
  await answer(page, 0, 2).check();
  await save(page).click();
  await expect(save(page)).toBeEnabled();
  await button(peer, '保存核对结果').click();
  await expect(savedAnswer).toContainText(await answer(page, 0, 2).evaluate(radio => radio.parentElement!.textContent!.trim()));
  await expect(answer(peer, 0, 0)).toBeChecked();
  await peer.screenshot({ path: info.outputPath('离线复习答案核对.png'), fullPage: true });
  await button(peer, '采用本机第 1 题答案').click();
  await button(peer, '保存核对结果').click();
  await expect(savedAnswer).toHaveCount(0);
  await expect(answer(peer, 0, 2)).toBeChecked();
  await expect(answer(peer, 1, 1)).toBeChecked();
  await submit(peer).click();
  await expect(peer.getByText('作答已保存到本机', { exact: true })).toBeVisible();
});

offlineReview('an unseen answer obtained during a submit is saved as a draft for review first', async ({ page, peer }) => {
  await answer(page, 0, 0).check();
  await answer(peer, 1, 1).check();
  await save(page).click();
  await expect(save(page)).toBeEnabled();
  await submit(peer).click();
  await expect(peer.getByText('已合并另一页保存的答案，请核对后再提交。', { exact: true })).toBeVisible();
  await expect(answer(peer, 0, 0)).toBeChecked();
  await expect(answer(peer, 1, 1)).toBeChecked();
  await expect(peer.getByText('作答已保存到本机', { exact: true })).toHaveCount(0);
  await submit(peer).click();
  await expect(peer.getByText('作答已保存到本机', { exact: true })).toBeVisible();
});

offlineReview('edits typed while a merge is committing are kept for comparison', async ({ page, peer }) => {
  await answer(page, 0, 0).check();
  await save(page).click();
  await expect(save(page)).toBeEnabled();
  await answer(peer, 1, 1).check();
  type HeldDraft = Window & { releaseReviewSave?: () => void };
  await peer.evaluate(() => {
    const target = IDBTransaction.prototype;
    const handler = Object.getOwnPropertyDescriptor(target, 'oncomplete')!;
    Object.defineProperty(target, 'oncomplete', {
      ...handler,
      set(this: IDBTransaction, finish: ((event: Event) => void) | null) {
        if (!finish || this.mode !== 'readwrite' || !this.objectStoreNames.contains('items')) {
          handler.set!.call(this, finish);
          return;
        }
        Object.defineProperty(target, 'oncomplete', handler);
        handler.set!.call(this, (event: Event) => {
          (window as HeldDraft).releaseReviewSave = () => finish.call(this, event);
        });
      },
    });
  });
  await save(peer).click();
  await peer.waitForFunction(() => typeof (window as HeldDraft).releaseReviewSave === 'function');
  await answer(peer, 0, 1).check();
  await peer.evaluate(() => (window as HeldDraft).releaseReviewSave!());
  await expect(peer.getByRole('group', { name: '第 1 题答案核对' })).toBeVisible();
  await expect(answer(peer, 0, 1)).toBeChecked();
  await expect(answer(peer, 1, 1)).toBeChecked();
  await button(peer, '保存核对结果').click();
  await expect(save(peer)).toBeVisible();
  await peer.reload();
  await openReview(peer);
  await expect(answer(peer, 0, 1)).toBeChecked();
  await expect(answer(peer, 1, 1)).toBeChecked();
});
