import { spawn } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { once } from 'node:events';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { devices, expect, test, type Dialog, type Page } from '@playwright/test';

test.use({ ...devices['Pixel 7'], channel: 'chrome' });

test('selected phone lessons, notes, and review outbox survive offline relaunch and sync once', async ({ page, context }, info) => {
  test.setTimeout(60000);
  const dir = mkdtempSync(join(tmpdir(), 'zhixue-mobile-'));
  mkdirSync(join(dir, 'library'));
  const python = process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python';
  const child = spawn(resolve(python), ['-m', 'backend.launch', '--port', '0', '--stdio', '--data', dir],
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
      const result = await context.request.fetch(new URL('/api' + path, service).href, { headers, method, data });
      expect(result.ok(), await result.text()).toBe(true);
      return await result.json() as T;
    };
    const ready = await api<{id: string; lessons: {id: string; title: string}[]}>('/projects', 'POST', {
      id: '80000000-0000-4000-8000-000000000050', topic: 'SQLite', outcome: '区分表的行和列', foundation: '电子表格',
    });
    const first = ready.lessons[0];
    await expect.poll(async () => (await api<{task: {status: string}}>('/projects/' + ready.id)).task.status,
      { timeout: 12000 }).toBe('ready');
    const lesson = '/lessons/' + first.id;
    const note = await api<{revision: string}>(lesson + '/note', 'PUT', { content: '# 要点\n电脑旧段\n手机旧段\n' });
    await api(lesson + '/learned', 'PUT', { learned: true });
    await api('/demo/next-day', 'POST');
    await page.goto(new URL('/mobile/#/?session=' + session, service).href);
    await expect(page.getByRole('heading', { name: '智学离线' })).toBeVisible();
    await page.getByRole('button', { name: '下载课程 ' + first.title }).click();
    await expect(page.getByRole('button', { name: '打开课程 ' + first.title })).toBeVisible();
    await page.getByRole('button', { name: '复习', exact: true }).click();
    await page.getByRole('button', { name: "下载题组 区分行与列" }).click();
    await expect(page.getByRole('button', { name: /^打开题组/ })).toHaveCount(1);

    await context.setOffline(true);
    await page.reload();
    await expect(page.getByText('离线', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '打开课程 ' + first.title }).click();
    const firstDraft = '# 要点\n电脑旧段\n手机第一次保存\n';
    const draft = '# 要点\n电脑旧段\n手机独立改稿\n保存未返回时继续输入\n';
    const editor = page.getByRole('textbox', { name: '本节笔记' });
    const save = page.getByRole('button', { name: '保存到本机' });
    type HeldNoteSave = Window & { finishNoteSave?: () => void };
    const holdNextStudyWrite = (target: Page = page) => target.evaluate(() => {
      const prototype = IDBTransaction.prototype;
      const complete = Object.getOwnPropertyDescriptor(prototype, 'oncomplete')!;
      Object.defineProperty(prototype, 'oncomplete', {
        ...complete,
        set(this: IDBTransaction, handler: ((event: Event) => void) | null) {
          if (!handler || this.mode !== 'readwrite' || !this.objectStoreNames.contains('items')) {
            complete.set!.call(this, handler);
            return;
          }
          Object.defineProperty(prototype, 'oncomplete', complete);
          complete.set!.call(this, (event: Event) => {
            const browser = window as HeldNoteSave;
            browser.finishNoteSave = () => {
              delete browser.finishNoteSave;
              handler.call(this, event);
            };
          });
        },
      });
    });
    await holdNextStudyWrite();
    await editor.fill(firstDraft);
    await save.click();
    await page.waitForFunction(() => typeof (window as HeldNoteSave).finishNoteSave === 'function');
    await expect(save).toBeDisabled();
    const notePanel = page.getByRole('region', { name: '笔记', exact: true });
    const unsaved = notePanel.getByText('笔记有未保存的修改', { exact: true });
    const saving = notePanel.getByText('正在保存到本机', { exact: true });
    const locallySaved = notePanel.getByText('笔记已保存到本机', { exact: true });
    await expect(saving).toBeVisible();
    await editor.fill(draft);
    await page.evaluate(() => (window as HeldNoteSave).finishNoteSave!());
    await expect(editor).toHaveValue(draft);
    await expect(save).toBeEnabled();
    await expect(unsaved).toBeVisible();
    await expect(locallySaved).toHaveCount(0);
    await page.screenshot({ path: info.outputPath('续写尚未保存.png') });
    await save.click();
    await expect(save).toBeDisabled();
    await expect(locallySaved).toBeVisible();
    await expect(unsaved).toHaveCount(0);
    await expect(saving).toHaveCount(0);

    const openOfflineCourse = page.getByRole('button', { name: '打开课程 ' + first.title });
    const returnDraft = draft + '返回之前保存的旧内容\n';
    const newestReturnDraft = returnDraft + '等待返回时写入的新内容\n';
    await holdNextStudyWrite();
    await editor.fill(returnDraft);
    await page.getByRole('button', { name: '返回课程' }).click();
    await page.waitForFunction(() => typeof (window as HeldNoteSave).finishNoteSave === 'function');
    await editor.fill(newestReturnDraft);
    await page.evaluate(() => (window as HeldNoteSave).finishNoteSave!());
    await openOfflineCourse.click();
    await expect(editor, 'Returning to the list must save edits made while awaiting the first write').toHaveValue(newestReturnDraft);

    const beforeTab = newestReturnDraft + '切换前发起写入\n';
    await editor.fill(beforeTab);
    await page.evaluate(() => {
      const put = IDBObjectStore.prototype.put;
      IDBObjectStore.prototype.put = function (...args: Parameters<typeof put>) {
        if (this.name === 'items' && args[0]?.kind === 'lesson') {
          IDBObjectStore.prototype.put = put;
          throw new DOMException('本机可用空间不足。', 'QuotaExceededError');
        }
        return put.apply(this, args);
      };
    });
    await page.getByRole('button', { name: '复习', exact: true }).click();
    await expect(page.getByRole('alert')).toContainText('本机可用空间不足。');
    await expect(page.getByRole('button', { name: '课程', exact: true })).toHaveAttribute('aria-pressed', 'true');
    await expect(editor).toHaveValue(beforeTab);

    await holdNextStudyWrite();
    await save.click();
    await page.waitForFunction(() => typeof (window as HeldNoteSave).finishNoteSave === 'function');
    await editor.fill(draft);
    await page.getByRole('button', { name: '复习', exact: true }).click();
    await page.evaluate(() => (window as HeldNoteSave).finishNoteSave!());
    await expect(page.getByRole('button', { name: '复习', exact: true })).toHaveAttribute('aria-pressed', 'true');
    await expect(page.getByRole('alert')).toHaveCount(0);
    await page.getByRole('button', { name: '课程', exact: true }).click();
    await openOfflineCourse.click();
    await expect(editor).toHaveValue(draft);

    const reopened = await context.newPage();
    await reopened.goto(new URL('/mobile/', service).href);
    await reopened.getByRole('button', { name: '打开课程 ' + first.title }).click();
    await expect(reopened.getByRole('textbox', { name: '本节笔记' })).toHaveValue(draft);
    await reopened.getByRole('button', { name: '复习', exact: true }).click();
    const openReview = reopened.getByRole('button', { name: /^打开题组/ });
    await openReview.click();
    const choices = reopened.getByRole('group').filter({ has: reopened.getByRole('radio') });
    const firstAnswer = choices.first().getByRole('radio').first();
    const secondAnswer = choices.nth(1).getByRole('radio').nth(1);
    const draftButton = reopened.getByRole('button', { name: '保存草稿', exact: true });
    const backToReviews = reopened.getByRole('button', { name: '返回复习', exact: true });
    const submit = reopened.getByRole('button', { name: '提交作答', exact: true });
    const waitForStudyWrite = () => reopened.waitForFunction(() => typeof (window as HeldNoteSave).finishNoteSave === 'function');
    const releaseStudyWrite = () => reopened.evaluate(() => (window as HeldNoteSave).finishNoteSave!());
    async function keepReviewOnReload(reason: string) {
      let dialogType: string | undefined;
      const cancel = async (dialog: Dialog) => {
        dialogType = dialog.type();
        await dialog.dismiss();
      };
      reopened.on('dialog', cancel);
      try {
        await reopened.reload({ timeout: 5000 }).catch(error => {
          if (dialogType !== 'beforeunload') throw error;
        });
        expect(dialogType, reason).toBe('beforeunload');
      } finally {
        reopened.off('dialog', cancel);
      }
    }
    await firstAnswer.check();
    await keepReviewOnReload('Reload must warn about an unsaved review answer');
    await expect(firstAnswer).toBeChecked();
    await holdNextStudyWrite(reopened);
    await draftButton.click();
    await waitForStudyWrite();
    await keepReviewOnReload('Reload must warn while review draft storage has not acknowledged the write');
    await expect(firstAnswer).toBeChecked();
    await secondAnswer.check();
    await releaseStudyWrite();
    await expect(draftButton).toBeEnabled();
    await backToReviews.click();
    await expect(openReview).toBeVisible();
    await reopened.reload();
    await reopened.getByRole('button', { name: '复习', exact: true }).click();
    await openReview.click();
    await expect(firstAnswer).toBeChecked();
    await expect(secondAnswer, 'A later answer must survive draft acknowledgement, leaving and offline reload').toBeChecked();

    const replacement = choices.nth(1).getByRole('radio').first();
    await holdNextStudyWrite(reopened);
    await replacement.check();
    await backToReviews.click();
    await waitForStudyWrite();
    await secondAnswer.check();
    await releaseStudyWrite();
    await openReview.click();
    await expect(secondAnswer, 'Returning from a review must persist the latest answer').toBeChecked();

    await holdNextStudyWrite(reopened);
    await submit.click();
    await waitForStudyWrite();
    await expect(secondAnswer).toBeDisabled();
    await expect(replacement).toBeDisabled();
    await releaseStudyWrite();
    await expect(reopened.getByText('作答已保存到本机', { exact: true })).toBeVisible();
    await reopened.close();
    await page.reload();
    await page.getByRole('button', { name: '同步', exact: true }).click();
    await expect(page.getByText('2 项待同步', { exact: true })).toBeVisible();
    const paths = await page.evaluate(async () => {
      const owned = (await caches.keys()).filter(key => key.startsWith('zhixue-mobile-'));
      return (await Promise.all(owned.map(async name => (await (await caches.open(name)).keys()).map(req => new URL(req.url).pathname)))).flat();
    });
    expect(paths).toContain('/mobile/');
    expect(paths.every(path => !path.startsWith('/api/'))).toBe(true);

    await api(lesson + '/note', 'PUT', { content: '# 要点\n电脑独立改稿\n手机旧段\n', baseRevision: note.revision });
    const sent: string[] = [];
    let missingReceipt = false;
    await page.route('**/api/mobile/changes', async route => {
      const request = route.request();
      if (request.method() !== 'POST' || request.postDataJSON().kind !== 'review') return route.continue();
      sent.push(request.postDataJSON().id);
      if (missingReceipt) return route.continue();
      const result = await route.fetch();
      expect(result.ok(), await result.text()).toBe(true);
      missingReceipt = true;
      await route.abort('failed');
    });
    await context.setOffline(false);
    await expect.poll(() => missingReceipt).toBe(true);
    await expect(page.getByText('1 项待同步', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '同步到电脑' }).click();
    await expect(page.getByText('0 项待同步', { exact: true })).toBeVisible({ timeout: 12000 });
    const saved = await api<{content: string; revision: string}>(lesson + '/note');
    expect(saved.content).toBe('# 要点\n电脑独立改稿\n手机独立改稿\n保存未返回时继续输入\n');
    const changes = await api<{reviews: {state: string; answers: number[]}[]}>('/mobile/changes');
    expect(sent).toHaveLength(2);
    expect(new Set(sent).size).toBe(1);
    expect(changes.reviews).toHaveLength(1);
    expect(changes.reviews[0]).toMatchObject({ state: 'settled', answers: [0, 1] });
    writeFileSync(info.outputPath('复习草稿与正式作答.json'), JSON.stringify({
      laterAnswerSurvivedDraftSave: true, latestAnswerSurvivedReturn: true,
      editingLockedWhileSubmitting: true, submissionsAfterReplay: changes.reviews.length, answers: [0, 1],
      warnedBeforeReloadWithUnsavedAnswer: true, warnedBeforeReloadDuringDraftSave: true,
    }, null, 2) + '\n');
    await page.unrouteAll({ behavior: 'wait' });
    const courses = page.getByRole('button', { name: '课程', exact: true });
    const openCourse = page.getByRole('button', { name: '打开课程 ' + first.title, exact: true });
    const sync = page.getByRole('button', { name: '同步', exact: true });
    await courses.click();
    await openCourse.click();
    await expect(editor).toHaveValue(saved.content);
    await context.setOffline(true);
    const concurrent = saved.content.replace('手机独立改稿', '电脑修改了同一段');
    await api(lesson + '/note', 'PUT', { content: concurrent, baseRevision: saved.revision });
    const conflictDraft = saved.content.replace('手机独立改稿', '手机修改了同一段');
    await editor.fill(conflictDraft);
    await expect(unsaved).toBeVisible();
    await save.click();
    await expect(locallySaved).toBeVisible();
    await sync.click();
    await context.setOffline(false);
    await expect(page.getByRole('heading', { name: '本机待处理', exact: true })).toBeVisible({ timeout: 12000 });
    await courses.click();
    await openCourse.click();
    const conflict = notePanel.getByText('与电脑笔记冲突 · 待处理', { exact: true });
    await expect(conflict).toBeVisible();
    await expect(editor).toHaveValue(conflictDraft);
    await editor.fill(conflictDraft + '继续核对冲突\n');
    await expect(unsaved).toBeVisible();
    await expect(conflict).toBeVisible();
    await expect(locallySaved).toHaveCount(0);
    await page.screenshot({ path: info.outputPath('未保存及同步冲突.png') });
    await editor.fill(conflictDraft);
    await expect(unsaved).toHaveCount(0);
    await expect(conflict).toBeVisible();
    const pending = await api<{ conflicts: { id: string }[] }>(lesson + '/note');
    expect(pending.conflicts).toHaveLength(1);
    writeFileSync(info.outputPath('保存状态与同步冲突.json'), JSON.stringify({
      statusDuringFirstSave: 'saving', statusAfterFirstReceipt: 'unsaved', statusAfterSecondSave: 'saved',
      conflictWhileEditing: true, retainedConflicts: pending.conflicts.length,
      latestTextSurvivedReturn: true, latestTextSurvivedTabSwitch: true, failedWriteKeptEditor: true,
    }, null, 2) + '\n');
    writeFileSync(info.outputPath('手机离线保存后续输入.json'), JSON.stringify({
      beforeLocalSave: firstDraft, laterPhoneDraft: draft, syncedDesktopNote: saved.content,
      survivedSaveCompletion: true, survivedOfflineReopen: true, waitingChangesAfterSync: 0,
    }, null, 2) + '\n');
  } finally {
    child.stdin.end();
    const killer = setTimeout(() => child.kill(), 3500);
    await done;
    clearTimeout(killer);
    rmSync(dir, { force: true, recursive: true });
  }
});
