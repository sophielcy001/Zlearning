const { app, BrowserWindow, dialog, Menu, nativeImage, shell, Tray, ipcMain } = require('electron');
const { spawn } = require('node:child_process');
const { join, resolve } = require('node:path');
const { createInterface } = require('node:readline');
const { openOfflineStudy } = require('./offline-store.cjs');

const root = resolve(__dirname, '..');
const windows = process.platform === 'win32';
const icon = app.isPackaged
  ? join(root, 'icons', windows ? 'icon.ico' : 'icon.png')
  : windows ? join(root, 'src-tauri', 'icons', 'icon.ico') : join(root, 'dist', 'zhixue.png');
const executable = app.isPackaged
  ? join(process.resourcesPath, windows ? 'zhixue-service.exe' : 'zhixue-service')
  : join(root, '.venv', windows ? 'Scripts/python.exe' : 'bin/python');
const localData = (windows && process.env.LOCALAPPDATA) || app.getPath('appData');
const data = resolve(process.env.ZHIXUE_DEMO_DATA || (app.isPackaged
  ? join(localData, 'Zhixue', 'Electron') : join(root, 'data', 'electron')));
app.setPath('userData', join(data, 'profile'));
let window;
let tray;
let service;
let address;
let quitPhase = 'idle';
let timerOnExit;
let offlineStudy;

function startService() {
  return new Promise((resolveUrl, reject) => {
    const entry = app.isPackaged ? [] : ['-m', 'backend.launch'];
    service = spawn(executable, [...entry, '--port', '0', '--stdio', '--data', data], {
      cwd: app.isPackaged ? process.resourcesPath : root,
      windowsHide: true, stdio: ['pipe', 'pipe', 'inherit'],
    });
    service.stdin.on('error', () => {});
    const reader = createInterface({ input: service.stdout });
    const timeout = setTimeout(() => reject(new Error('等待本地服务超时。')), 18_000);
    service.once('error', error => { clearTimeout(timeout); reject(error); });
    service.once('exit', code => { clearTimeout(timeout); reject(new Error(`本地服务退出（${code}）。`)); });
    reader.once('line', line => {
      clearTimeout(timeout);
      reader.close();
      try {
        const url = new URL(line);
        if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port || !url.hash.startsWith('#/?session=')) {
          throw new Error('本地服务没有返回会话地址。');
        }
        resolveUrl(url);
      } catch { reject(new Error('无法读取本地服务会话。')); }
    });
  });
}

function showWindow() {
  if (!window || window.isDestroyed()) return;
  if (window.isMinimized()) window.restore();
  window.show();
  window.focus();
}

function offlineStoreFor(event) {
  const contents = window?.webContents;
  const frame = event.senderFrame;
  const source = URL.parse(frame?.url ?? '');
  if (event.sender !== contents || !frame || frame !== contents.mainFrame ||
      source?.origin !== address?.origin || source?.pathname !== '/mobile/' ||
      service?.exitCode !== null || service.signalCode !== null) {
    throw new Error('本机离线数据仅供智学手机离线页面使用。');
  }
  offlineStudy ??= openOfflineStudy(join(app.getPath('userData'), 'offline-study.sqlite3'));
  return offlineStudy;
}

async function setup() {
  const url = await startService();
  address = url;
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    { label: '应用', submenu: [{ label: '完全退出', accelerator: 'Ctrl+Shift+Q', click: () => app.quit() }] },
  ]));
  const trayIcon = process.platform === 'darwin' ? nativeImage.createFromPath(icon).resize({ width: 18, height: 18 }) : icon;
  tray = new Tray(trayIcon);
  tray.setToolTip('智学 · Electron');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: '打开智学', click: showWindow },
    { type: 'separator' },
    { label: '完全退出', click: () => app.quit() },
  ]));
  tray.on('click', showWindow);
  await createWindow(url);
}

async function createWindow(url) {
  window = new BrowserWindow({
    title: '智学 · Electron', icon, width: 1250, height: 880, minWidth: 720, minHeight: 540, show: false,
    backgroundColor: '#f5f7fc',
    webPreferences: {
      contextIsolation: true, sandbox: true, nodeIntegration: false,
      preload: join(__dirname, 'window-preload.cjs'),
    },
  });
  window.on('page-title-updated', event => event.preventDefault());
  const webContents = window.webContents;
  webContents.on('will-prevent-unload', cancelWindowClose);
  // Chromium can keep a capturing page visible while the native window is hidden.
  const notifyHidden = () => {
    if (!webContents.isDestroyed()) webContents.send('zhixue:window-hidden');
  };
  window.on('hide', notifyHidden);
  window.on('minimize', notifyHidden);
  const localMedia = (contents, permission, details) =>
    contents === webContents && permission === 'media' && details?.isMainFrame === true;
  webContents.session.setPermissionCheckHandler((contents, permission, origin, details) =>
    localMedia(contents, permission, details) && origin === url.origin && details.mediaType === 'audio');
  webContents.session.setPermissionRequestHandler((contents, permission, callback, details) => {
    const target = details?.requestingUrl;
    const types = details?.mediaTypes;
    const audioOnly = types?.length === 1 && types[0] === 'audio';
    callback(Boolean(localMedia(contents, permission, details) && audioOnly
      && URL.canParse(target) && new URL(target).origin === url.origin));
  });
  window.webContents.on('will-navigate', (event, location) => {
    if (new URL(location).origin !== url.origin) event.preventDefault();
  });
  window.webContents.setWindowOpenHandler(({ url: link }) => {
    if (/^https?:\/\//i.test(link)) void shell.openExternal(link).catch(() => {});
    return { action: 'deny' };
  });
  window.on('close', event => {
    if (quitPhase === 'closing') return;
    event.preventDefault();
    window.hide();
  });
  await window.loadURL(url.href);
  showWindow();
}

async function timerRequest(data) {
  const response = await fetch(new URL('/api/timer', address), {
    method: data ? 'POST' : 'GET',
    headers: {
      Authorization: `Bearer ${new URLSearchParams(address.hash.split('?')[1]).get('session')}`,
      'Content-Type': 'application/json',
    },
    signal: AbortSignal.timeout(3000),
    body: data && JSON.stringify(data),
  });
  if (!response.ok) throw new Error('计时数据暂时不可用。');
  return response.json();
}

async function quitWhenReady() {
  if (quitPhase !== 'idle') return;
  quitPhase = 'checking';
  try {
    if (address && service.exitCode === null && service.signalCode === null) {
      const { active } = await timerRequest();
      if (active && active.device !== 'phone') {
        const options = {
          type: 'question', title: '智学 · 完全退出', message: '还有一条学习计时尚未结束。',
          detail: '结束后保存有效时长。暂存后可在下次启动时核对。',
          buttons: ['结束计时并退出', '暂存并退出', '取消'], defaultId: 0, cancelId: 2, noLink: true,
        };
        const { response } = await (window?.isVisible() ? dialog.showMessageBox(window, options) : dialog.showMessageBox(options));
        if (response === 2) {
          quitPhase = 'idle';
          return;
        }
        timerOnExit = { action: response === 0 ? 'finish' : 'stash', timerId: active.id };
      }
    }
    quitPhase = 'closing';
    if (window && !window.isDestroyed()) window.close();
    else void finishQuit();
  } catch {
    timerOnExit = undefined;
    quitPhase = 'idle';
    showTimerError();
  }
}

function showTimerError() {
  dialog.showErrorBox('智学 · 计时保存失败', '计时状态暂时不可用，请稍后重试。');
}

function cancelWindowClose() {
  if (quitPhase !== 'closing') return;
  timerOnExit = undefined;
  quitPhase = 'checking';
  showWindow();
  const options = {
    type: 'info', title: '智学 · 完全退出', message: '有未保存的学习内容。',
    detail: '请返回编辑，保存笔记或复习草稿后再退出。', buttons: ['返回编辑'], defaultId: 0, noLink: true,
  };
  void dialog.showMessageBox(window, options).catch(() => {}).finally(() => { quitPhase = 'idle'; });
}

async function finishQuit() {
  quitPhase = 'finishing';
  if (timerOnExit) {
    try {
      await timerRequest(timerOnExit);
    } catch {
      timerOnExit = undefined;
      await createWindow(address).catch(() => {});
      quitPhase = 'idle';
      showTimerError();
      return;
    }
  }
  if (service?.pid && service.exitCode === null && service.signalCode === null) {
    await new Promise(resolve => {
      const timeout = setTimeout(() => service.kill(), 6500);
      service.once('exit', () => { clearTimeout(timeout); resolve(); });
      service.stdin.end();
    });
  }
  quitPhase = 'finished';
  app.quit();
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  ipcMain.handle('zhixue:offline-list', event => offlineStoreFor(event).list());
  ipcMain.handle('zhixue:offline-read', (event, ...args) => offlineStoreFor(event).read(...args));
  ipcMain.handle('zhixue:offline-write', (event, ...args) => offlineStoreFor(event).write(...args));
  app.once('will-quit', () => offlineStudy?.close());
  app.on('second-instance', showWindow);
  if (process.platform === 'darwin') app.on('activate', showWindow);
  // A window can veto closing to protect unsaved work.
  app.on('window-all-closed', () => {
    if (quitPhase === 'closing') void finishQuit();
  });
  app.on('before-quit', event => {
    if (quitPhase === 'finished') return;
    event.preventDefault();
    void quitWhenReady();
  });
  app.whenReady().then(setup).catch(error => {
    const hint = app.isPackaged ? '请检查应用安装和数据目录。' : '请检查 npm run build 和 uv sync --locked 是否已运行。';
    dialog.showErrorBox('智学启动失败', `${error.message}\n${hint}`);
    process.exitCode = 1;
    app.quit();
  });
}
