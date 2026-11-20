import { execFile, spawn } from 'node:child_process';
import { once } from 'node:events';
import { randomUUID } from 'node:crypto';
import { existsSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { promisify } from 'node:util';
import { _electron as electron, expect, test, type Page } from '@playwright/test';

const packaged = process.env.ZHIXUE_TEST_ELECTRON;
if (packaged) test.describe.configure({ timeout: 90000 });

function electronOptions(data: string, extraArgs: string[] = []) {
  return {
    ...(packaged ? { executablePath: packaged } : {}),
    args: packaged ? extraArgs : ['desktop/electron.cjs', ...extraArgs],
    env: { ...process.env, ZHIXUE_DEMO_DATA: data },
    timeout: packaged ? 60000 : 30000,
  };
}

async function completeFirstUse(page: Page) {
  await page.getByRole('button', { name: '使用此位置', exact: true }).click();
  await page.getByRole('button', { name: '稍后配置模型', exact: true }).click({ timeout: 12000 });
  await expect(page.getByRole('heading', { name: '今天，想弄懂什么？', exact: true })).toBeVisible();
}

test('Electron hides and reopens the same window, then releases its service on quit', async () => {
  const data = mkdtempSync(join(tmpdir(), 'zhixue-electron-'));
  const app = await electron.launch(electronOptions(data));
  const processHandle = app.process();
  try {
    expect(await app.evaluate(({ app }) => app.isPackaged)).toBe(Boolean(packaged));
    const page = await app.firstWindow();
    await completeFirstUse(page);
    const health = new URL('/healthz', page.url());
    const windowId = await app.evaluate(({ BrowserWindow }) => {
      const window = BrowserWindow.getAllWindows()[0];
      window.close();
      return window.id;
    });
    expect(await app.evaluate(({ BrowserWindow }, id) => BrowserWindow.fromId(id)?.isVisible(), windowId)).toBe(false);
    expect((await fetch(health)).ok).toBe(true);

    if (packaged && process.platform === 'darwin') {
      const bundle = join(dirname(resolve(packaged)), '..', '..');
      await promisify(execFile)('/usr/bin/open', [bundle]);
    } else {
      await app.evaluate(({ app }) => app.emit(process.platform === 'darwin' ? 'activate' : 'second-instance'));
    }
    await expect.poll(() => app.evaluate(({ BrowserWindow }, id) =>
      BrowserWindow.fromId(id)?.isVisible(), windowId)).toBe(true);
    expect((await fetch(health)).ok).toBe(true);
    await app.close();
    await expect.poll(async () => fetch(health).then(() => false, () => true)).toBe(true);
  } finally {
    if (processHandle.exitCode === null && processHandle.signalCode === null) await app.close();
    rmSync(data, { recursive: true, force: true });
  }
});

async function watchWindowsExit(pid: number, executable: string) {
  const expected = executable.replaceAll("'", "''");
  const command = `
$ErrorActionPreference = 'Stop'
$target = [Diagnostics.Process]::GetProcessById(${pid})
try {
  if ($target.MainModule.FileName -ne '${expected}') { throw 'Electron process path changed' }
  $null = $target.Handle
  [Console]::WriteLine('attached')
  if (-not $target.WaitForExit(30000)) { throw 'Electron main process did not exit' }
  [Console]::WriteLine([BitConverter]::ToUInt32([BitConverter]::GetBytes([int]$target.ExitCode), 0))
} finally { $target.Dispose() }
`;
  const encoded = Buffer.from(command, 'utf16le').toString('base64');
  const watcher = spawn('powershell.exe', ['-NoProfile', '-NonInteractive', '-EncodedCommand', encoded], {
    stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true,
  });
  let output = '';
  let error = '';
  watcher.stdout.on('data', bytes => { output += String(bytes); });
  watcher.stderr.on('data', bytes => { error += String(bytes); });
  const done = once(watcher, 'close').catch((failure: Error) => {
    error += failure.message;
    return [null];
  });
  await expect.poll(() => output.startsWith('attached'), {
    timeout: 8000, message: 'Open the Electron main process before requesting exit',
  }).toBe(true);
  return async () => {
    const [code] = await done;
    expect(code, error || 'Windows process observer exit').toBe(0);
    const lines = output.trim().split(/\r?\n/);
    expect(lines).toHaveLength(2);
    expect(lines[1]).toMatch(/^\d+$/);
    return Number(lines[1]);
  };
}

test('Electron offers the default timer save and preserves it after restart', async ({}, info) => {
  const data = mkdtempSync(join(tmpdir(), 'zhixue-electron-timer-'));
  const options = electronOptions(data);
  let app = await electron.launch(options);
  let processHandle = app.process();
  try {
    expect(await app.evaluate(({ app }) => app.isPackaged)).toBe(Boolean(packaged));
    const page = await app.firstWindow();
    await completeFirstUse(page);
    await page.getByRole('button', { name: '体验 SQLite 样例' }).click();
    await page.getByRole('button', { name: '提交并生成路线' }).click();
    await page.getByRole('link', { name: '开始学习' }).click();
    await page.getByRole('button', { name: '开始计时' }).click();
    await expect(page.getByText('计时中', { exact: false })).toBeVisible();
    const health = new URL('/healthz', page.url());
    async function timerAt(target: Page) {
      const session = await target.evaluate(() => sessionStorage.getItem('zhixue-session'));
      const result = await target.request.get(new URL('/api/timer', target.url()).href,
        { headers: { Authorization: 'Bearer ' + session } });
      expect(result.status()).toBe(200);
      return result.json();
    }
    const active = (await timerAt(page)).active;
    expect(active?.id).toBeTruthy();
    const main = await app.evaluate(() => ({ pid: process.pid, executable: process.execPath }));
    const launcherPid = processHandle.pid;
    const readMainExit = process.platform === 'win32' ? await watchWindowsExit(main.pid, main.executable) : undefined;
    await app.evaluate(({ app, dialog }) => {
      const original = dialog.showMessageBox;
      // Use the native dialog's configured default without injecting desktop input.
      dialog.showMessageBox = (async (...args: unknown[]) => {
        dialog.showMessageBox = original;
        const options = args.at(-1) as { defaultId?: number; cancelId?: number; buttons?: string[] };
        const useDefault = options.defaultId === 0 && options.cancelId === 2 && options.buttons?.[0] === '结束计时并退出';
        return { response: useDefault ? options.defaultId : 2, checkboxChecked: false };
      }) as typeof original;
      app.quit();
    });
    const mainExit = await readMainExit?.();
    await expect.poll(() => processHandle.exitCode !== null || processHandle.signalCode !== null,
      { timeout: 18000 }).toBe(true);
    const launcherExit = processHandle.exitCode;
    const exitRecord = {
      mainPid: main.pid, launcherPid, mainExit: mainExit ?? launcherExit, launcherExit,
      launcherSignal: processHandle.signalCode, localServiceStopped: false,
      sameTimerAfterRestart: null as boolean | null,
    };
    const recordExit = () => writeFileSync(info.outputPath('默认计时退出进程.json'),
      JSON.stringify(exitRecord, null, 2) + '\n');
    recordExit();
    await expect.poll(async () => fetch(health).then(() => false, () => true)).toBe(true);
    exitRecord.localServiceStopped = true;
    recordExit();

    app = await electron.launch(options);
    processHandle = app.process();
    const reopened = await app.firstWindow();
    await reopened.getByRole('link', { name: '管理中心' }).click();
    await expect(reopened.getByText('最近保存的原始计时', { exact: true })).toBeVisible();
    await expect(reopened.getByText(/理解表、行和列 · 00:/)).toBeVisible();
    await expect(reopened.getByRole('link', { name: '返回计时课程' })).toHaveCount(0);
    const reopenedTimer = await timerAt(reopened);
    exitRecord.sameTimerAfterRestart = reopenedTimer.lastSaved?.id === active.id;
    recordExit();
    expect(reopenedTimer.active).toBeNull();
    expect(exitRecord.sameTimerAfterRestart, 'Restarted app must read the exact timer selected at exit').toBe(true);
    expect(mainExit ?? launcherExit, 'Electron main process exit').toBe(0);
    expect(launcherExit, 'Playwright launcher exit').toBe(0);
  } finally {
    if (processHandle.exitCode === null && processHandle.signalCode === null) {
      await app.evaluate(({ dialog }) => {
        dialog.showMessageBox = (async () => ({ response: 0, checkboxChecked: false })) as typeof dialog.showMessageBox;
      });
      await app.close();
    }
    rmSync(data, { recursive: true, force: true });
  }
});

test('Electron preserves the service and timer when an unsaved note vetoes quitting', async ({}, info) => {
  test.setTimeout(90000);
  const temp = realpathSync(tmpdir());
  const data = realpathSync(mkdtempSync(join(temp, 'zhixue-electron-veto-')));
  expect(dirname(data)).toBe(temp);
  const app = await electron.launch(electronOptions(data));
  const processHandle = app.process();
  try {
    expect(await app.evaluate(({ app }) => app.isPackaged)).toBe(Boolean(packaged));
    const page = await app.firstWindow();
    await completeFirstUse(page);
    const origin = new URL(page.url()).origin;
    const health = new URL('/healthz', origin);
    const headers = { Authorization: 'Bearer ' + await page.evaluate(() => sessionStorage.getItem('zhixue-session')) };
    async function api(path: string, method = 'GET', data?: object) {
      const response = await page.request.fetch(origin + '/api' + path, { headers, method, data });
      expect(response.ok(), path + ' HTTP ' + response.status()).toBe(true);
      return response.json();
    }
    const project = await api('/projects', 'POST', { id: randomUUID(), topic: 'SQLite', outcome: '核对取消完全退出' });
    await expect.poll(async () => (await api('/projects/' + project.id)).task.status, { timeout: 12000 }).toBe('ready');
    const lesson = project.lessons[0];
    await api('/lessons/' + lesson.id + '/note', 'PUT', { content: '退出复验的原始笔记。' });
    const initial = (await api('/timer', 'POST', { action: 'start', lessonId: lesson.id })).active;
    const draft = '取消完全退出后保留的修改。';
    await page.getByRole('link', { name: '手机离线' }).click();
    await page.getByRole('button', { name: '下载课程 ' + lesson.title }).click();
    const open = page.getByRole('button', { name: '打开课程 ' + lesson.title });
    await open.click();
    const editor = page.getByRole('textbox', { name: '本节笔记' });
    await editor.click();
    await editor.fill(draft);
    expect(await page.evaluate(() => navigator.userActivation.hasBeenActive)).toBe(true);
    await expect(page.getByText('笔记有未保存的修改', { exact: true })).toBeVisible();

    const quit = () => app.evaluate(({ app }) => { setImmediate(() => app.quit()); });
    const vetoSignal = join(data, 'exit-veto-probe');
    await app.evaluate(({ BrowserWindow, dialog }, signal) => {
      const state = { timerPrompts: 0, error: '' };
      Object.assign(globalThis, { exitProbe: state });
      BrowserWindow.getAllWindows()[0].webContents.once('will-prevent-unload', () => {
        process.getBuiltinModule('node:fs')!.writeFileSync(signal, 'blocked');
      });
      dialog.showMessageBox = (async (...args: unknown[]) => {
        const options = args.at(-1) as { buttons?: string[] };
        if (options.buttons?.[0] === '结束计时并退出') state.timerPrompts++;
        return { response: 0, checkboxChecked: false };
      }) as typeof dialog.showMessageBox;
      dialog.showErrorBox = (_title, content) => { state.error = content; };
    }, vetoSignal);
    const trace = () => app.evaluate(() => (globalThis as typeof globalThis & {
      exitProbe: { timerPrompts: number; error: string };
    }).exitProbe);
    await quit();
    await expect.poll(() => existsSync(vetoSignal), { message: 'Expected the renderer to block quitting' }).toBe(true);
    expect(await fetch(health).then(response => response.ok, () => false), 'Service stopped before the renderer approved exit').toBe(true);
    expect((await api('/timer')).active).toMatchObject({ id: initial.id, status: 'running' });
    await expect(editor).toHaveValue(draft);
    await expect(page.getByText('笔记有未保存的修改', { exact: true })).toBeVisible();

    await page.getByRole('button', { name: '保存到本机', exact: true }).click();
    await expect(page.getByText('笔记已保存到本机', { exact: true })).toBeVisible();
    await app.evaluate(() => {
      const fetch = globalThis.fetch;
      globalThis.fetch = async (input, init) => {
        if (init?.method === 'POST' && new URL(String(input)).pathname === '/api/timer') {
          globalThis.fetch = fetch;
          throw new Error('Injected failure committing the timer choice');
        }
        return fetch(input, init);
      };
    });
    const reopenedWindow = app.waitForEvent('window');
    await quit();
    const restored = await reopenedWindow;
    await expect(restored.getByRole('heading', { name: '今天，想弄懂什么？', exact: true })).toBeVisible();
    await expect.poll(async () => (await trace()).error).toContain('计时状态暂时不可用');
    expect((await fetch(health)).ok).toBe(true);
    await restored.getByRole('link', { name: '手机离线' }).click();
    await restored.getByRole('button', { name: '打开课程 ' + lesson.title }).click();
    await expect(restored.getByRole('textbox', { name: '本节笔记' })).toHaveValue(draft);
    const restoredActive = await restored.request.get(origin + '/api/timer', { headers });
    expect((await restoredActive.json()).active).toMatchObject({ id: initial.id, status: 'running' });
    expect((await trace()).timerPrompts).toBe(2);

    await quit();
    await expect.poll(() => processHandle.exitCode, { timeout: 18000 }).toBe(0);
    await expect.poll(() => fetch(health).then(() => false, () => true)).toBe(true);
    writeFileSync(info.outputPath('完全退出否决与恢复.json'), JSON.stringify({
      keptServiceAfterVeto: true, keptSameActiveTimer: true, keptDraft: true,
      recoveredWindowAfterTimerFailure: true, exitedAfterSave: true,
    }, null, 2) + '\n');
  } finally {
    if (processHandle.exitCode === null && processHandle.signalCode === null) {
      await app.evaluate(({ BrowserWindow, dialog }) => {
        dialog.showMessageBox = (async () => ({ response: 0, checkboxChecked: false })) as typeof dialog.showMessageBox;
        BrowserWindow.getAllWindows().forEach(window => window.webContents.on('will-prevent-unload', event => event.preventDefault()));
      }).catch(() => {});
      await app.close().catch(() => {});
    }
    rmSync(data, { recursive: true, force: true, maxRetries: 8, retryDelay: 200 });
  }
});

test('Electron keeps an unsaved offline review and its timer on a quit request', async ({}, info) => {
  test.setTimeout(90000);
  const temp = realpathSync(tmpdir());
  const data = realpathSync(mkdtempSync(join(temp, 'zhixue-electron-review-veto-')));
  expect(dirname(data)).toBe(temp);
  const options = electronOptions(data);
  let app = await electron.launch(options);
  let child = app.process();
  try {
    expect(await app.evaluate(({ app }) => app.isPackaged)).toBe(Boolean(packaged));
    const page = await app.firstWindow();
    await completeFirstUse(page);
    const service = new URL(page.url()).origin;
    const health = new URL('/healthz', service);
    const headers = { Authorization: 'Bearer ' + await page.evaluate(() => sessionStorage.getItem('zhixue-session')) };
    async function api(path: string, method = 'GET', data?: object) {
      const response = await page.request.fetch(service + '/api' + path, { headers, method, data });
      expect(response.ok(), path + ' HTTP ' + response.status()).toBe(true);
      return response.json();
    }
    const project = await api('/projects', 'POST', { id: randomUUID(), topic: 'SQLite', outcome: '检验复习草稿退出保护' });
    await expect.poll(async () => (await api('/projects/' + project.id)).task.status, { timeout: 12000 }).toBe('ready');
    const lesson = project.lessons[0];
    await api('/lessons/' + lesson.id + '/learned', 'PUT', { learned: true });
    await api('/demo/next-day', 'POST');
    const timer = (await api('/timer', 'POST', { action: 'start', lessonId: lesson.id })).active;
    async function openReview(target: Page, download = false) {
      await target.getByRole('link', { name: '手机离线' }).click();
      await target.getByRole('button', { name: '复习', exact: true }).click();
      if (download) await target.getByRole('button', { name: '下载题组 区分行与列', exact: true }).click();
      await target.getByRole('button', { name: /^打开题组/ }).click();
      return target.getByRole('group').filter({ has: target.getByRole('radio') }).first().getByRole('radio').nth(1);
    }
    const answer = await openReview(page, true);
    await answer.check();
    expect(await page.evaluate(() => navigator.userActivation.hasBeenActive)).toBe(true);
    const vetoSignal = join(data, 'review-veto-probe');
    const dialogSignal = join(data, 'review-veto-message.txt');
    await app.evaluate(({ BrowserWindow, dialog }, { vetoSignal, dialogSignal }) => {
      const fs = process.getBuiltinModule('node:fs')!;
      BrowserWindow.getAllWindows()[0].webContents.once('will-prevent-unload', () => {
        fs.writeFileSync(vetoSignal, 'blocked');
      });
      dialog.showMessageBox = (async (...args: unknown[]) => {
        const options = args.at(-1) as { message: string; buttons?: string[] };
        if (options.buttons?.[0] === '返回编辑') fs.writeFileSync(dialogSignal, options.message);
        return { response: 0, checkboxChecked: false };
      }) as typeof dialog.showMessageBox;
    }, { vetoSignal, dialogSignal });
    const quit = () => app.evaluate(({ app }) => { setImmediate(() => app.quit()); });
    await quit();
    await expect.poll(() => existsSync(vetoSignal) || child.exitCode !== null,
      { timeout: 18000, message: 'The review editor must veto quitting before the app stops' }).toBe(true);
    expect(existsSync(vetoSignal), 'Unsaved review did not prevent complete exit').toBe(true);
    await expect.poll(() => existsSync(dialogSignal)).toBe(true);
    expect(readFileSync(dialogSignal, 'utf8')).toBe('有未保存的学习内容。');
    expect((await fetch(health)).ok).toBe(true);
    expect((await api('/timer')).active).toMatchObject({ id: timer.id, status: 'running' });
    await expect(answer).toBeChecked();

    const draft = page.getByRole('button', { name: '保存草稿', exact: true });
    await draft.click();
    await expect(draft).toBeEnabled();
    await quit();
    await expect.poll(() => child.exitCode, { timeout: 18000 }).toBe(0);
    await expect.poll(() => fetch(health).then(() => false, () => true)).toBe(true);

    app = await electron.launch(options);
    child = app.process();
    const restored = await app.firstWindow();
    await expect(await openReview(restored)).toBeChecked();
    await expect(restored.getByRole('button', { name: '提交作答', exact: true })).toBeEnabled();
    await app.close();
    await expect.poll(() => child.exitCode, { timeout: 18000 }).toBe(0);
    writeFileSync(info.outputPath('复习草稿取消退出.json'), JSON.stringify({
      blockedQuitWithUnsavedAnswer: true, keptSameTimerAndLocalService: true, reopenedUnsubmittedAnswer: true,
    }, null, 2) + '\n');
  } finally {
    if (child.exitCode === null && child.signalCode === null) {
      await app.evaluate(({ BrowserWindow, dialog }) => {
        dialog.showMessageBox = (async () => ({ response: 0, checkboxChecked: false })) as typeof dialog.showMessageBox;
        BrowserWindow.getAllWindows().forEach(window => window.webContents.on('will-prevent-unload', event => event.preventDefault()));
      }).catch(() => {});
      await app.close().catch(() => {});
    }
    rmSync(data, { recursive: true, force: true, maxRetries: 8, retryDelay: 200 });
  }
});

test('Electron quits while a phone timer remains active in the workspace', async () => {
  const data = mkdtempSync(join(tmpdir(), 'zhixue-electron-phone-timer-'));
  const options = electronOptions(data);
  let app = await electron.launch(options);
  let running = app.process();
  try {
    expect(await app.evaluate(({ app }) => app.isPackaged)).toBe(Boolean(packaged));
    const page = await app.firstWindow();
    await completeFirstUse(page);
    await page.getByRole('button', { name: '体验 SQLite 样例' }).click();
    await page.getByRole('button', { name: '提交并生成路线' }).click();
    await page.getByRole('link', { name: '开始学习' }).click();
    const result = await page.evaluate(async () => {
      const headers = { Authorization: 'Bearer ' + sessionStorage.getItem('zhixue-session'), 'Content-Type': 'application/json' };
      const catalog = await (await fetch('/api/mobile/catalog', { headers })).json();
      const workspace = catalog.workspace as string;
      const clock = await (await fetch('/api/mobile/timer?workspace=' + workspace, { headers })).json();
      const timerId = crypto.randomUUID();
      const answer = await fetch('/api/mobile/timer', { method: 'POST', headers, body: JSON.stringify({
        id: crypto.randomUUID(), workspace, timerId, kind: 'timer', lessonId: catalog.lessons[0].id,
        category: 'learning', action: 'start', capturedAt: clock.serverAt, live: true, present: true,
      }) });
      return { status: answer.status, timerId };
    });
    expect(result.status).toBe(200);
    await app.evaluate(({ app, dialog }) => {
      dialog.showMessageBox = (async () => ({ response: 2, checkboxChecked: false })) as typeof dialog.showMessageBox;
      app.quit();
    });
    await expect.poll(() => running.exitCode).toBe(0);

    app = await electron.launch(options);
    running = app.process();
    const reopened = await app.firstWindow();
    await expect(reopened.getByRole('link', { name: '返回计时课程' })).toContainText('手机');
    const saved = await reopened.evaluate(async () => {
      const response = await fetch('/api/timer', { headers: {
        Authorization: 'Bearer ' + sessionStorage.getItem('zhixue-session'),
      } });
      return (await response.json()).active;
    });
    expect(saved).toMatchObject({ id: result.timerId, device: 'phone', status: 'running' });
  } finally {
    if (running.exitCode === null && running.signalCode === null) {
      await app.evaluate(({ dialog }) => {
        dialog.showMessageBox = (async () => ({ response: 0, checkboxChecked: false })) as typeof dialog.showMessageBox;
      });
      await app.close();
    }
    rmSync(data, { recursive: true, force: true });
  }
});

test('installed Windows Electron keeps and saves timers through its native exit dialog', async () => {
  test.skip(process.platform !== 'win32' || !packaged, 'requires an installed Windows Electron candidate');
  const data = mkdtempSync(join(tmpdir(), 'zhixue-electron-native-dialog-'));
  const options = electronOptions(data);
  const runFile = promisify(execFile);
  let fixture = await electron.launch(options);
  let child = fixture.process();
  let page = await fixture.firstWindow();
  let nativePid = await fixture.evaluate(() => process.pid);
  const timers = () => page.evaluate(async () => {
    const response = await fetch('/api/timer', {
      headers: { Authorization: 'Bearer ' + sessionStorage.getItem('zhixue-session') },
    });
    if (!response.ok) throw new Error('Timer state unavailable: ' + response.status);
    return response.json();
  });
  const quit = () => fixture.evaluate(({ app }) => { setImmediate(() => app.quit()); });
  const choose = async (label: string) => {
    const { stdout } = await runFile('powershell.exe', [
      '-NoLogo', '-NoProfile', '-NonInteractive', '-File', resolve('desktop/windows-native.ps1'),
      '-Mode', 'dialog', '-RootId', String(nativePid), '-DialogTitle', '智学 · 完全退出', '-Choice', label,
    ], { timeout: 18000, windowsHide: true });
    expect(JSON.parse(stdout)).toMatchObject({ acknowledged: true });
  };
  const waitForExit = async (oldPage: Page) => {
    const health = new URL('/healthz', oldPage.url());
    await expect.poll(() => child.exitCode, { timeout: 14000 }).toBe(0);
    await expect.poll(() => fetch(health).then(() => false, () => true)).toBe(true);
  };
  const reopen = async () => {
    fixture = await electron.launch(options);
    child = fixture.process();
    page = await fixture.firstWindow();
    nativePid = await fixture.evaluate(() => process.pid);
    await expect(page.getByRole('heading', { name: '今天，想弄懂什么？', exact: true })).toBeVisible();
  };
  try {
    expect(await fixture.evaluate(({ app }) => app.isPackaged)).toBe(true);
    await completeFirstUse(page);
    await page.getByRole('button', { name: '体验 SQLite 样例' }).click();
    await page.getByRole('button', { name: '提交并生成路线' }).click();
    await page.getByRole('link', { name: '开始学习' }).click();
    await page.getByRole('button', { name: '开始计时' }).click();
    await expect.poll(async () => (await timers()).active).toMatchObject({ device: 'desktop', status: 'running' });
    const started = (await timers()).active;
    const reading = new URL(page.url()).hash;
    await quit();
    await choose('取消');
    await expect.poll(async () => (await timers()).active).toMatchObject({ id: started.id, status: 'running' });

    await quit();
    await choose('暂存并退出');
    await waitForExit(page);
    await reopen();
    const stashed = await timers();
    expect(stashed.active).toBeNull();
    expect(stashed.stashed).toEqual(expect.arrayContaining([expect.objectContaining({ id: started.id })]));

    await page.goto(new URL(reading, page.url()).href);
    await page.getByRole('button', { name: '开始计时' }).click();
    await expect.poll(async () => (await timers()).active).toMatchObject({ device: 'desktop', status: 'running' });
    const next = (await timers()).active;
    await quit();
    await choose('结束计时并退出');
    await waitForExit(page);
    await reopen();
    expect(await timers()).toMatchObject({ active: null, lastSaved: { id: next.id } });
    await quit();
    await waitForExit(page);
  } finally {
    if (child.exitCode === null && child.signalCode === null) {
      // Playwright's Windows launcher can wrap Electron, so clean up the owned native process tree.
      await runFile('taskkill.exe', ['/PID', String(nativePid), '/T', '/F'], { windowsHide: true }).catch(() => {});
      await expect.poll(() => child.exitCode !== null || child.signalCode !== null).toBe(true);
    }
    rmSync(data, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  }
});

test('Electron releases a late microphone stream when hidden and can record again locally', async () => {
  test.setTimeout(120000);
  const temp = realpathSync(tmpdir());
  const folder = realpathSync(mkdtempSync(join(temp, 'zhixue-electron-voice-')));
  expect(dirname(folder)).toBe(temp);
  const app = await electron.launch(electronOptions(folder, [
    '--use-fake-device-for-media-stream',
    '--use-file-for-fake-audio-capture=' + resolve('e2e/fixtures/voice-jfk.wav'),
  ]));
  const processHandle = app.process();
  try {
    expect(await app.evaluate(({ app }) => app.isPackaged)).toBe(Boolean(packaged));
    const page = await app.firstWindow();
    await completeFirstUse(page);
    const health = new URL('/healthz', page.url());

    const camera = await page.evaluate(async () => {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ video: true });
        stream.getTracks().forEach(track => track.stop());
        return 'allowed';
      } catch (error) {
        return error instanceof DOMException ? error.name : String(error);
      }
    });
    expect(camera).toBe('NotAllowedError');

    await page.evaluate(() => {
      const request = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
      const tracks: MediaStreamTrack[] = [];
      let release: () => void;
      const first = new Promise<void>(resolve => { release = resolve; });
      Reflect.set(window, 'testVoiceTracks', tracks);
      Reflect.set(window, 'releaseTestVoice', () => release());
      navigator.mediaDevices.getUserMedia = async constraints => {
        const hold = tracks.length === 0;
        const stream = await request(constraints);
        tracks.push(...stream.getAudioTracks());
        if (hold) await first;
        return stream;
      };
    });
    const trackStates = () => page.evaluate(() =>
      (Reflect.get(window, 'testVoiceTracks') as MediaStreamTrack[]).map(track => track.readyState));
    const hide = () => app.evaluate(({ BrowserWindow }) => { BrowserWindow.getAllWindows()[0].close(); });
    const show = () => app.evaluate(({ app }) => { app.emit(process.platform === 'darwin' ? 'activate' : 'second-instance'); });
    let transcriptions = 0;
    page.on('request', request => {
      if (new URL(request.url()).pathname === '/api/voice/transcribe') transcriptions++;
    });

    const input = page.getByLabel('学习问题或目标');
    const button = page.getByRole('button', { name: '学习问题语音输入' });
    const dialog = page.getByRole('dialog', { name: '录音转文字' });
    const start = dialog.getByRole('button', { name: '开始录音' });
    await input.fill('保留原来输入的目标');
    await button.click();
    await start.click();
    await expect(dialog.getByText('正在打开麦克风')).toBeVisible();
    await expect.poll(trackStates).toEqual(['live']);
    await hide();
    await expect.poll(() => app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].isVisible())).toBe(false);
    await page.evaluate(() => Reflect.get(window, 'releaseTestVoice')());
    await expect.poll(trackStates).toEqual(['ended']);
    await expect(dialog).toHaveCount(0);
    expect(transcriptions).toBe(0);
    console.log('PASS hiding the Electron window ends the late-arriving simulated microphone stream without transcribing');

    await show();
    await button.click();
    await start.click();
    await expect(dialog.getByText('正在录音', { exact: true })).toBeVisible();
    await app.evaluate(({ BrowserWindow }) => { BrowserWindow.getAllWindows()[0].minimize(); });
    await expect.poll(() => app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].isMinimized())).toBe(true);
    await expect(dialog).toHaveCount(0);
    await expect.poll(trackStates).toEqual(['ended', 'ended']);
    expect(transcriptions).toBe(0);
    console.log('PASS minimizing Electron while recording releases the simulated microphone');

    await show();
    await button.click();
    await dialog.getByLabel('录音语言').selectOption('en');
    await start.click();
    await expect(dialog.getByText('正在录音', { exact: true })).toBeVisible();
    await new Promise(resolve => setTimeout(resolve, 4100));
    await dialog.getByRole('button', { name: '停止并识别' }).click();
    const draft = dialog.getByRole('textbox', { name: '语音草稿' });
    await expect(draft).toHaveValue(/fellow Americans/i, { timeout: 15000 });
    await expect(input).toHaveValue('保留原来输入的目标');
    expect(transcriptions).toBe(1);
    await expect.poll(trackStates).toEqual(['ended', 'ended', 'ended']);
    await draft.fill('核对后的本机转写');
    await dialog.getByRole('button', { name: '填入文字' }).click();
    await expect(input).toHaveValue('保留原来输入的目标\n核对后的本机转写');
    console.log('PASS Electron accepts audio without camera access and applies the local transcript only on confirmation');

    await button.click();
    await start.click();
    await expect(dialog.getByText('正在录音', { exact: true })).toBeVisible();
    await page.evaluate(() => { location.hash = '/library'; });
    await expect(page.getByRole('heading', { name: '知识库', exact: true })).toBeVisible();
    await expect(dialog).toHaveCount(0);
    await expect.poll(trackStates).toEqual(['ended', 'ended', 'ended', 'ended']);
    expect(transcriptions).toBe(1);
    console.log('PASS navigating away releases the simulated microphone while leaving the local service usable');
    await app.close();
    await expect.poll(() => fetch(health).then(() => false, () => true)).toBe(true);
  } finally {
    if (processHandle.exitCode === null && processHandle.signalCode === null) await app.close();
    rmSync(folder, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  }
});

async function verifyOfflineRestart(interrupted: boolean) {
  test.setTimeout(90000);
  const data = mkdtempSync(join(tmpdir(), 'zhixue-electron-offline-'));
  const options = electronOptions(data);
  let app = await electron.launch(options);
  let processHandle = app.process();
  try {
    const page = await app.firstWindow();
    await completeFirstUse(page);
    const desktopBridge = () => page.evaluate(() => window.zhixueDesktop!.offline!.list());
    await expect(desktopBridge()).rejects.toThrow('本机离线数据仅供智学手机离线页面使用。');
    const origin = new URL(page.url()).origin;
    const session = await page.evaluate(() => sessionStorage.getItem('zhixue-session'));
    function api<T>(target: Page, root: string, token: string | null, path: string, method = 'GET', body?: object) {
      return target.request.fetch(root + '/api' + path, {
        method, data: body, headers: { Authorization: 'Bearer ' + token },
      }).then(async response => {
        expect(response.ok(), path + ' HTTP ' + response.status()).toBe(true);
        return await response.json() as T;
      });
    }
    const created = await api<{ id: string; lessons: { id: string; title: string }[] }>(page, origin, session, '/projects',
      'POST', { id: randomUUID(), topic: 'SQLite', outcome: '区分表的行和列', foundation: '电子表格' });
    await expect.poll(async () => (await api<{ task: { status: string } }>(page, origin, session,
      '/projects/' + created.id)).task.status).toBe('ready');
    const first = created.lessons[0];
    const lesson = '/lessons/' + first.id;
    await api(page, origin, session, lesson + '/note', 'PUT', { content: '# 下载时的笔记\n' });
    await api(page, origin, session, lesson + '/learned', 'PUT', { learned: true });
    await api(page, origin, session, '/demo/next-day', 'POST');
    await page.getByRole('link', { name: '手机离线' }).click();
    await page.getByRole('button', { name: '下载课程 ' + first.title }).click();
    await expect(page.getByRole('button', { name: '打开课程 ' + first.title })).toBeVisible();
    await page.getByRole('button', { name: '复习', exact: true }).click();
    await page.getByRole('button', { name: '下载题组 区分行与列' }).click();
    await expect(page.getByRole('button', { name: /^打开题组/ })).toBeVisible();

    await page.context().setOffline(true);
    await expect(page.getByText('离线', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '课程', exact: true }).click();
    await page.getByRole('button', { name: '打开课程 ' + first.title }).click();
    const draft = '# 重启前的离线笔记\n';
    await page.getByRole('textbox', { name: '本节笔记' }).fill(draft);
    await page.getByRole('button', { name: '保存到本机' }).click();
    await expect(page.getByText('笔记已保存到本机', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '开始笔记计时' }).click();
    const timer = page.getByRole('region', { name: '本机计时' });
    await expect(timer.locator('time')).not.toHaveText('00:00:00');
    await timer.getByRole('button', { name: '结束计时' }).click();
    await page.getByRole('button', { name: '复习', exact: true }).click();
    await page.getByRole('button', { name: /^打开题组/ }).click();
    const questions = page.getByRole('group').filter({ has: page.getByRole('radio') });
    await questions.nth(0).getByRole('radio').nth(0).check();
    await questions.nth(1).getByRole('radio').nth(1).check();
    await page.getByRole('button', { name: '提交作答' }).click();
    await expect(page.getByText('作答已保存到本机', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '同步', exact: true }).click();
    await expect(page.getByText('2 项待同步', { exact: true })).toBeVisible();
    await expect(page.getByText('2 次计时操作待同步。', { exact: true })).toBeVisible();

    if (interrupted) {
      // On Windows the process started by Playwright can be a wrapper around the Electron main process.
      const mainPID = await app.evaluate(() => process.pid);
      const launchedPID = processHandle.pid;
      const health = new URL('/healthz', origin);
      expect((await fetch(health)).ok).toBe(true);
      expect(process.kill(mainPID, 'SIGKILL')).toBe(true);
      const began = performance.now();
      await expect.poll(() => fetch(health, { signal: AbortSignal.timeout(600) })
        .then(() => false, error => error.cause?.code === 'ECONNREFUSED'), { timeout: 14000 }).toBe(true);
      await expect.poll(() => processHandle.exitCode !== null || processHandle.signalCode !== null).toBe(true);
      console.log(JSON.stringify({ mainPID, launchedPID, oldLoopbackClosedMs: Math.round(performance.now() - began) }));
    } else {
      await app.close();
      await expect.poll(() => processHandle.exitCode).toBe(0);
    }
    app = await electron.launch(options);
    processHandle = app.process();
    const reopened = await app.firstWindow();
    await expect(reopened.getByRole('link', { name: '手机离线' })).toBeVisible();
    const reopenedOrigin = new URL(reopened.url()).origin;
    expect(reopenedOrigin).not.toBe(origin);
    const reopenedSession = await reopened.evaluate(() => sessionStorage.getItem('zhixue-session'));
    await reopened.route('**/api/mobile/**', route => route.abort('failed'));
    await reopened.getByRole('link', { name: '手机离线' }).click();
    await reopened.getByRole('button', { name: '同步', exact: true }).click();
    await expect(reopened.getByText('2 项待同步', { exact: true })).toBeVisible();
    await expect(reopened.getByText('2 次计时操作待同步。', { exact: true })).toBeVisible();
    await reopened.getByRole('button', { name: '课程', exact: true }).click();
    await reopened.getByRole('button', { name: '打开课程 ' + first.title }).click();
    await expect(reopened.getByRole('textbox', { name: '本节笔记' })).toHaveValue(draft);

    await reopened.unroute('**/api/mobile/**');
    await reopened.getByRole('button', { name: '同步', exact: true }).click();
    await reopened.getByRole('button', { name: '同步到电脑' }).click();
    await expect(reopened.getByText('0 项待同步', { exact: true })).toBeVisible({ timeout: 12000 });
    await expect(reopened.getByText(/次计时操作待同步/)).toHaveCount(0);
    expect((await api<{ content: string }>(reopened, reopenedOrigin, reopenedSession, lesson + '/note')).content).toBe(draft);
    const settled = await api<{ reviews: { state: string; answers: number[] }[] }>(reopened, reopenedOrigin, reopenedSession,
      '/mobile/changes');
    expect(settled.reviews).toHaveLength(1);
    expect(settled.reviews[0]).toMatchObject({ state: 'settled', answers: [0, 1] });
    console.log('PASS loopback origin changed; downloaded lesson and note, two waiting changes and both timer operations survived; one review settled');
  } finally {
    if (processHandle.exitCode === null && processHandle.signalCode === null) await app.close();
    rmSync(data, { recursive: true, force: true, maxRetries: 8, retryDelay: 200 });
  }
}

test('Electron reopens downloaded study and sends offline work once after its loopback origin changes',
  () => verifyOfflineRestart(false));

test('Windows Electron releases its local service and reopens pending study after main process termination', async () => {
  test.skip(process.platform !== 'win32', 'Covers the Windows Electron launch process and sidecar.');
  await verifyOfflineRestart(true);
});
