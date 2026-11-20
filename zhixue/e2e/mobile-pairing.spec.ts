import { execFileSync, spawn, type ChildProcessByStdio } from 'node:child_process';
import { once } from 'node:events';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { type Readable, type Writable } from 'node:stream';
import { devices, expect, test, type Page } from '@playwright/test';

const python = resolve(process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
const packagedService = process.env.ZHIXUE_TEST_SERVICE;
type HttpsDeployment = {
  phoneOrigin: string; relayOrigin: string; relayPort: string;
  bundle: string; relayPython: string; chromeSpki: string;
};
const deployment: HttpsDeployment | undefined = process.env.ZHIXUE_TEST_MOBILE_DEPLOYMENT
  ? JSON.parse(process.env.ZHIXUE_TEST_MOBILE_DEPLOYMENT) : undefined;
type Child = ChildProcessByStdio<Writable, Readable, Readable>;

async function service(args: string[], address: RegExp, stream: 'stdout' | 'stderr', graceful = false,
  runtime?: { executable: string; directory: string }) {
  const sidecar = args[0] === '-m' && args[1] === 'backend.launch' ? packagedService : undefined;
  const command = sidecar ? args.slice(2) : ['-u', ...args];
  const child: Child = spawn(sidecar ?? runtime?.executable ?? python, command, {
    cwd: runtime?.directory, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true,
  });
  const closed = once(child, 'close');
  const lines = createInterface({ input: child[stream] });
  const stop = async () => {
    if (child.exitCode !== null || child.signalCode !== null) return;
    child.stdin.end();
    if (!graceful) child.kill();
    const killer = setTimeout(() => child.kill('SIGKILL'), 14000);
    try { await closed; } finally { clearTimeout(killer); }
  };
  try {
    const url = await new Promise<URL>((resolve, reject) => {
      const startup = setTimeout(() => reject(new Error('Service failed to announce an address')), sidecar ? 35000 : 12000);
      child.once('error', reject);
      child.once('exit', code => reject(new Error('Service exited before readiness with code ' + code)));
      lines.on('line', line => {
        const match = line.match(address);
        if (!match) return;
        clearTimeout(startup);
        resolve(new URL(match[1]));
      });
      void closed.then(() => clearTimeout(startup));
    });
    return { url, stop };
  } catch (error) {
    await stop();
    throw error;
  } finally {
    lines.close();
    child.stdout.resume();
    child.stderr.resume();
  }
}

test.use({ ...devices['Pixel 7'], channel: 'chrome' });
if (deployment) {
  const phone = new URL(deployment.phoneOrigin);
  const relay = new URL(deployment.relayOrigin);
  if (phone.protocol !== 'https:' || relay.protocol !== 'https:' || phone.origin === relay.origin) {
    throw new Error('HTTPS deployment verification needs two different HTTPS origins.');
  }
  test.use({ launchOptions: { args: [
    '--host-resolver-rules=MAP ' + phone.hostname + ' 127.0.0.1',
    '--ignore-certificate-errors-spki-list=' + deployment.chromeSpki,
  ] } });
}

test('paired phone shares offline work, timers and reminders through an encrypted relay', async ({ page, context, browser }, info) => {
  test.skip(!['darwin', 'win32'].includes(process.platform), 'The desktop stores pairings in the native system vault.');
  test.setTimeout(packagedService ? 240000 : 180000);
  const folder = mkdtempSync(join(tmpdir(), 'zhixue-device-sync-'));
  mkdirSync(join(folder, 'library'));
  const desktopBrowser = await browser.newContext({ viewport: { width: 1080, height: 960 } });
  const desktop = await desktopBrowser.newPage();
  const services: Awaited<ReturnType<typeof service>>[] = [];
  const start = async (...args: Parameters<typeof service>) => {
    const child = await service(...args);
    services.push(child);
    return child;
  };
  const desktopArgs = ['-m', 'backend.launch', '--port', '0', '--stdio', '--data', folder];
  const startDesktop = () => start(desktopArgs, /^(http:\/\/127\.0\.0\.1:\d+\/#\/\?session=\S+)$/, 'stdout', true);
  let computer: Awaited<ReturnType<typeof service>> | undefined;
  const secretHeaders = () => {
    const session = new URLSearchParams(computer!.url.hash.split('?')[1]).get('session');
    return { Authorization: 'Bearer ' + session };
  };
  const api = async <T,>(path: string, method = 'GET', data?: object): Promise<T> => {
    const result = await desktopBrowser.request.fetch(new URL('/api' + path, computer!.url).href, {
      headers: secretHeaders(), method, data,
    });
    expect(result.ok(), await result.text()).toBe(true);
    return await result.json() as T;
  };
  const showPairing = async () => {
    await desktop.goto(new URL('/mobile/' + computer!.url.hash, computer!.url).href);
    await desktop.getByRole('button', { name: '同步', exact: true }).click();
  };
  try {
    const hosted = deployment ? { url: new URL(deployment.phoneOrigin) }
      : await start(['-m', 'http.server', '0', '--bind', '127.0.0.1', '--directory', 'dist'],
        /Serving HTTP on .*? \((http:\/\/127\.0\.0\.1:\d+\/)\)/, 'stdout');
    const relayRuntime = deployment && { executable: deployment.relayPython, directory: deployment.bundle };
    const startRelay = (port: string) => start(['-m', 'backend.mobile_relay', '--port', port, '--origin', hosted.url.origin],
      /Uvicorn running on (http:\/\/127\.0\.0\.1:\d+)/, 'stderr', false, relayRuntime);
    const relay = await startRelay(deployment?.relayPort ?? '0');
    const relayOrigin = deployment ? new URL(deployment.relayOrigin).origin : relay.url.origin;
    computer = await startDesktop();
    expect(new Set([computer.url.origin, relayOrigin, hosted.url.origin]).size).toBe(3);
    const denied = await desktopBrowser.request.get(new URL('/api/device-sync', computer.url).href);
    expect(denied.status()).toBe(401);
    const ready = await api<{ id: string; lessons: { id: string; title: string }[] }>('/projects', 'POST', {
      id: '80000000-0000-4000-8000-000000000058', topic: 'SQLite', outcome: '区分表的行和列', foundation: '电子表格',
    });
    await expect.poll(async () => (await api<{task: {status: string}}>('/projects/' + ready.id)).task.status,
      { timeout: 12000 }).toBe('ready');
    await api('/reminders/settings', 'PUT', { enabled: false, quiet: null });
    const first = ready.lessons[0];
    const lesson = '/lessons/' + first.id;
    const draft = '# 同步试验\n电脑原段\n手机已离线修改 📒\n';
    const original = await api<{ revision: string }>(lesson + '/note', 'PUT', { content: '# 同步试验\n电脑原段\n手机原段\n' });
    await api(lesson + '/learned', 'PUT', { learned: true });
    await api('/demo/next-day', 'POST');

    const phoneTraffic: string[] = [];
    page.on('request', request => phoneTraffic.push(request.url()));
    await showPairing();
    await desktop.getByLabel('手机网页地址').fill(hosted.url.origin);
    await desktop.getByLabel('中继地址').fill(relayOrigin);
    await desktop.getByRole('button', { name: '生成配对链接' }).click();
    const invite = desktop.getByLabel('手机配对链接');
    await expect(invite).toBeVisible();
    await test.step('rejects a stale cancel and refreshes the invitation created elsewhere', async () => {
      const prior = await api<{ revision: string; invitation: { url: string } }>('/device-sync');
      await desktop.evaluate(() => {
        const originalFetch = window.fetch;
        let held = false;
        const release = new Promise<void>(resolve => window.addEventListener('release-invite-view', () => {
          window.fetch = originalFetch;
          resolve();
        }, { once: true }));
        window.fetch = async function (...args: Parameters<typeof fetch>) {
          const response = await originalFetch.apply(this, args);
          if (!held && args[0] === '/api/device-sync' && args[1]?.method === 'GET') {
            held = true;
            document.documentElement.dataset.inviteView = 'held';
            await release;
          }
          return response;
        };
      });

      try {
        await expect(desktop.locator('html')).toHaveAttribute('data-invite-view', 'held');
        const replacement = await api<{ revision: string; invitation: { url: string } }>('/device-sync/invite', 'POST', {
          mobile: hosted.url.origin, relay: relayOrigin,
        });
        expect(replacement.invitation.url).not.toBe(prior.invitation.url);
        const conflict = desktop.waitForResponse(response =>
          new URL(response.url()).pathname === '/api/device-sync/invite' && response.request().method() === 'DELETE');
        await desktop.getByRole('button', { name: '取消邀请', exact: true }).click();
        const response = await conflict;
        expect(new URL(response.url()).searchParams.get('revision') === prior.revision).toBe(true);
        expect(response.status()).toBe(409);
        await expect(desktop.getByRole('alert')).toContainText('配对状态已变化');
        await expect(invite).toHaveValue(replacement.invitation.url);
        const finalState = await api<{ revision: string }>('/device-sync');
        expect(finalState.revision === replacement.revision).toBe(true);
        writeFileSync(info.outputPath('电脑配对过期操作.json'), JSON.stringify({
          conflictRejected: true, replacementVisible: true, newerInvitationPreserved: true,
        }, null, 2));
      } finally {
        await desktop.evaluate(() => window.dispatchEvent(new Event('release-invite-view')));
      }
    });
    const inviteUrl = await invite.inputValue();
    expect(new URL(inviteUrl).origin).toBe(hosted.url.origin);
    await page.goto(new URL('/zhixue.png', hosted.url).href);
    await page.evaluate(() => new Promise<void>((resolve, reject) => {
      const open = indexedDB.open('zhixue-mobile-study', 1);
      open.onerror = () => reject(open.error);
      open.onupgradeneeded = () => {
        open.result.createObjectStore('items', { keyPath: 'key' }).put({
          key: 'old-workspace:lesson:original', kind: 'lesson', note: null,
          snapshot: { workspace: 'old-workspace', id: 'original', title: '旧版下载课程', body: '# 旧版课程', projectTopic: '旧项目' },
        });
      };
      open.onsuccess = () => { open.result.close(); resolve(); };
    }));
    await page.goto(inviteUrl);
    expect(new URL(page.url()).hash).toBe('#/');
    await page.getByLabel('此手机的名称').fill('隔离测试手机');
    await page.getByRole('button', { name: '申请配对' }).click();
    const phoneCode = page.getByLabel('手机配对码');
    await expect(phoneCode).toHaveText(/^[0-9A-F]{8}$/);
    const code = await phoneCode.innerText();
    await expect(desktop.getByLabel('电脑配对码')).toHaveText(code, { timeout: 10000 });
    await page.getByRole('button', { name: '课程', exact: true }).click();
    await expect(page.getByRole('button', { name: '下载课程 ' + first.title })).toHaveCount(0);
    const invalid = await desktopBrowser.request.post(new URL('/api/device-sync/approve', computer.url).href, {
      headers: secretHeaders(), data: { code: code === '00000000' ? 'FFFFFFFF' : '00000000' },
    });
    expect(invalid.status()).toBe(409);
    await test.step('ignores a real pre-approval poll delivered after approval', async () => {
      await desktop.evaluate(() => {
        const fetchCurrent = window.fetch;
        let captured = false;
        const release = new Promise<void>(resolve =>
          window.addEventListener('release-old-pairing-status', () => resolve(), { once: true }));
        const resume = new Promise<void>(resolve => window.addEventListener('resume-pairing-status', () => {
          window.fetch = fetchCurrent;
          resolve();
        }, { once: true }));
        window.fetch = async function (...args: Parameters<typeof fetch>) {
          const response = await fetchCurrent.apply(this, args);
          if (args[0] === '/api/device-sync' && args[1]?.method === 'GET') {
            if (!captured) {
              captured = true;
              document.documentElement.dataset.pairingStatus = 'held';
              await release;
              setTimeout(() => { document.documentElement.dataset.pairingStatus = 'delivered'; }, 300);
            } else await resume;
          }
          return response;
        };
      });

      try {
        await expect(desktop.locator('html')).toHaveAttribute('data-pairing-status', 'held');
        await desktop.getByRole('button', { name: '确认配对码并批准' }).click();
        const paired = desktop.getByText('已配对 隔离测试手机', { exact: true });
        await expect(paired).toBeVisible({ timeout: 12000 });
        await desktop.evaluate(() => window.dispatchEvent(new Event('release-old-pairing-status')));
        await expect(desktop.locator('html')).toHaveAttribute('data-pairing-status', 'delivered');
        await expect(paired, 'a stale read must not restore the already approved invitation').toBeVisible({ timeout: 1500 });
        await expect(desktop.getByLabel('电脑配对码')).toHaveCount(0);
        writeFileSync(info.outputPath('电脑配对轮询隔离.json'),JSON.stringify({
          heldBeforeApproval: true, deliveredAfterApproval: true, approvedViewPreserved: true,
        },null,2));
      } finally {
        await desktop.evaluate(() => window.dispatchEvent(new Event('resume-pairing-status')));
      }
    });
    const course = page.getByRole('button', { name: '下载课程 ' + first.title });
    await expect(course).toBeEnabled({ timeout: 15000 });
    await test.step('registers and removes a browser subscription through the encrypted pairing', async () => {
      await context.grantPermissions(['notifications'], { origin: hosted.url.origin });
      await page.evaluate(() => {
        let subscription: object | null = null;
        const base64 = (data: Uint8Array) => btoa(String.fromCharCode(...data))
          .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
        PushManager.prototype.getSubscription = async () => subscription as PushSubscription | null;
        PushManager.prototype.subscribe = async options => {
          if (!navigator.userActivation.isActive || !options?.userVisibleOnly ||
              !(options.applicationServerKey instanceof Uint8Array) || options.applicationServerKey.length !== 65) {
            throw new Error('The browser requires a direct action and its paired application key.');
          }
          const endpoint = 'https://fcm.googleapis.com/fcm/send/zhixue-browser-fixture';
          const keys = await crypto.subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, true, ['deriveBits']);
          const encoded = new Uint8Array(await crypto.subtle.exportKey('raw', keys.publicKey));
          subscription = {
            endpoint, options: { applicationServerKey: options.applicationServerKey.buffer },
            toJSON: () => ({ endpoint, expirationTime: null, keys: {
              p256dh: base64(encoded), auth: base64(new Uint8Array(16)),
            } }),
            unsubscribe: async () => { subscription = null; return true; },
          };
          return subscription as PushSubscription;
        };
      });
      await page.getByRole('button', { name: '同步', exact: true }).click();
      await page.getByText('手机系统推送', { exact: true }).click();
      await page.getByRole('button', { name: '启用手机推送', exact: true }).click({ timeout: 14000 });
      await expect(page.getByText('已启用手机系统推送。')).toBeVisible({ timeout: 12000 });
      await page.screenshot({ path: info.outputPath('手机推送设置.png'), fullPage: true });
      await page.getByRole('button', { name: '停用手机推送', exact: true }).click();
      await expect(page.getByRole('button', { name: '停用手机推送', exact: true })).toHaveCount(0, { timeout: 12000 });
      await expect(page.getByRole('button', { name: '启用手机推送', exact: true })).toBeEnabled();

      await page.getByRole('button', { name: '启用手机推送', exact: true }).click();
      await expect(page.getByText('已启用手机系统推送。')).toBeVisible({ timeout: 12000 });
      await context.setOffline(true);
      await expect(page.getByText('离线', { exact: true })).toBeVisible();
      await page.getByRole('button', { name: '停用手机推送', exact: true }).click();
      await expect(page.getByText('等待同步停用手机推送。', { exact: true })).toBeVisible();
      await page.reload();
      await page.getByRole('button', { name: '同步', exact: true }).click();
      await page.getByText('手机系统推送', { exact: true }).click();
      await expect(page.getByText('等待同步停用手机推送。', { exact: true })).toBeVisible();
      await page.getByRole('button', { name: '课程', exact: true }).click();
      await context.setOffline(false);
      await expect(course).toBeEnabled({ timeout: 15000 });
      await expect.poll(() => page.evaluate(() => new Promise<IDBValidKey[]>((resolve, reject) => {
        const request = indexedDB.open('zhixue-mobile-study');
        request.onerror = () => reject(request.error);
        request.onsuccess = () => {
          const db = request.result;
          const tx = db.transaction('device');
          const keys = tx.objectStore('device').getAllKeys();
          tx.oncomplete = () => { db.close(); resolve(keys.result); };
          tx.onabort = () => { db.close(); reject(tx.error); };
        };
      })), { timeout: 15000 }).toEqual(['pair', 'pair-version']);
      await page.getByRole('button', { name: '同步', exact: true }).click();
      await page.getByText('手机系统推送', { exact: true }).click();
      await expect(page.getByRole('button', { name: '启用手机推送', exact: true })).toBeEnabled({ timeout: 15000 });
      await expect(page.getByRole('button', { name: '停用手机推送', exact: true })).toHaveCount(0);
      await page.getByRole('button', { name: '课程', exact: true }).click();
    });
    await test.step('rejects an old workspace notice and retries a course after a failed relay request', async () => {
      const { workspace } = await api<{ workspace: string }>('/mobile/catalog');
      const second = ready.lessons[1];
      const pairing = await page.evaluate(async () => {
        const room = await new Promise<string>((resolve, reject) => {
          const open = indexedDB.open('zhixue-mobile-study', 3);
          open.onerror = () => reject(open.error);
          open.onsuccess = () => {
            const db = open.result;
            const tx = db.transaction('device');
            const pair = tx.objectStore('device').get('pair');
            tx.oncomplete = () => { db.close(); resolve(pair.result.room); };
            tx.onabort = () => { db.close(); reject(tx.error); };
          };
        });
        const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(room));
        return Array.from(new Uint8Array(digest), value => value.toString(16).padStart(2, '0')).join('');
      });
      const matching = { v: 1, delivery: '40000000-0000-4000-8000-000000000007', workspace, lessonId: second.id, pairing };
      const deliver = (notice: typeof matching) => page.evaluate(data => {
        navigator.serviceWorker.dispatchEvent(new MessageEvent('message', { data: { type: 'zhixue-notice', notice: data } }));
      }, notice);
      await deliver({ ...matching, delivery: '40000000-0000-4000-8000-000000000008',
        workspace: '40000000-0000-4000-8000-000000000009' });
      await expect(page.getByRole('alert').filter({ hasText: '提醒对应的工作区已更换。' })).toBeVisible();
      expect(new URL(page.url()).hash).toBe('#/');
      await expect(course).toBeEnabled();
      await expect(page.getByRole('heading', { name: second.title, exact: true })).toHaveCount(0);

      const offers = (url: URL) => url.origin === relayOrigin && url.pathname.endsWith('/offer');
      await page.route(offers, route => route.abort());
      try {
        await deliver(matching);
        const failure = page.getByRole('alert').filter({ hasText: '暂未连上电脑' });
        await expect(failure.getByRole('button', { name: '重试', exact: true })).toBeVisible();
        expect(new URL(page.url()).hash).toContain(matching.delivery);
      } finally {
        await page.unroute(offers);
      }
      await page.getByRole('alert').filter({ hasText: '暂未连上电脑' })
        .getByRole('button', { name: '重试', exact: true }).click();
      await expect(page.getByRole('heading', { name: second.title, level: 2, exact: true })).toBeVisible({ timeout: 15000 });
      expect(new URL(page.url()).hash).toBe('#/');
      await expect(page.getByRole('region', { name: '本机计时' })).toHaveCount(0);
      await page.getByRole('button', { name: '返回课程', exact: true }).click();
    });
    await expect(page.getByRole('button', { name: '打开课程 旧版下载课程' })).toBeVisible();
    await course.click();
    const openCourse = page.getByRole('button', { name: '打开课程 ' + first.title });
    await expect(openCourse).toBeVisible();
    await page.getByRole('button', { name: '复习', exact: true }).click();
    await page.getByRole('button', { name: '下载题组 区分行与列' }).click();
    await expect(page.getByRole('button', { name: /^打开题组/ })).toBeVisible();
    await api(lesson + '/note', 'PUT', { content: '# 同步试验\n电脑独立修改\n手机原段\n', baseRevision: original.revision });

    await computer.stop();
    await page.getByRole('button', { name: '同步', exact: true }).click();
    await page.getByRole('button', { name: '同步到电脑', exact: true }).click();
    await expect(page.getByText(/暂未连上电脑/)).toBeVisible({ timeout: 18000 });
    await context.setOffline(true);
    await page.reload();
    await expect(page.getByText('离线', { exact: true })).toBeVisible();
    await openCourse.click();
    await page.getByRole('textbox', { name: '本节笔记' }).fill(draft);
    await page.getByRole('button', { name: '保存到本机' }).click();
    await expect(page.getByText('笔记已保存到本机', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '复习', exact: true }).click();
    await page.getByRole('button', { name: /^打开题组/ }).click();
    const radios = page.getByRole('group').filter({ has: page.getByRole('radio') });
    await radios.first().getByRole('radio').first().check();
    await radios.nth(1).getByRole('radio').nth(1).check();
    await page.getByRole('button', { name: '提交作答' }).click();
    await expect(page.getByText('作答已保存到本机', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '同步', exact: true }).click();
    await expect(page.getByText('2 项待同步', { exact: true })).toBeVisible();
    await page.screenshot({ path: info.outputPath('手机离线待同步.png'), fullPage: true });

    computer = await startDesktop();
    await showPairing();
    await expect(desktop.getByText('已配对 隔离测试手机', { exact: true })).toBeVisible();
    const receivedAddress = new URL('/api/mobile/changes', computer.url).href;
    const encrypted: Buffer[] = [];
    let discarded = false;
    await page.route(relayOrigin + '/v1/**/exchange/*', async route => {
      const packet = route.request().postDataBuffer();
      if (!packet) return route.continue();
      encrypted.push(packet);
      const response = await route.fetch();
      encrypted.push(await response.body());
      if (!discarded) {
        const history = await desktopBrowser.request.get(receivedAddress, { headers: secretHeaders() });
        if (history.ok() && (await history.json()).reviews.length === 1) {
          discarded = true;
          return route.abort('failed');
        }
      }
      await route.fulfill({ response });
    });
    await context.setOffline(false);
    await expect.poll(() => discarded, { timeout: 24000 }).toBe(true);
    await expect(page.getByText('1 项待同步', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '同步到电脑', exact: true }).click();
    await expect(page.getByText('0 项待同步', { exact: true })).toBeVisible({ timeout: 18000 });
    expect((await api<{ content: string }>(lesson + '/note')).content).toBe('# 同步试验\n电脑独立修改\n手机已离线修改 📒\n');
    const history = await api<{ reviews: { state: string; answers: number[] }[] }>('/mobile/changes');
    expect(history.reviews).toHaveLength(1);
    expect(history.reviews[0]).toMatchObject({ state: 'settled', answers: [0, 1] });
    expect(encrypted.length).toBeGreaterThanOrEqual(8);
    expect(encrypted.every(packet => !packet.includes('手机已离线修改') && !packet.includes('"answers"')
      && !packet.includes('隔离测试手机'))).toBe(true);
    expect(phoneTraffic.every(url => [hosted.url.origin, relayOrigin].includes(new URL(url).origin))).toBe(true);
    const keyExtractable = await page.evaluate(() => new Promise<boolean>((resolve, reject) => {
      const lookup = indexedDB.open('zhixue-mobile-study');
      lookup.onerror = () => reject(lookup.error);
      lookup.onsuccess = () => {
        const store = lookup.result.transaction('device').objectStore('device');
        const pair = store.get('pair');
        pair.onsuccess = () => { resolve(pair.result.key.extractable); lookup.result.close(); };
        pair.onerror = () => reject(pair.error);
      };
    }));
    expect(keyExtractable).toBe(false);
    await relay.stop();
    await page.getByRole('button', { name: '同步到电脑', exact: true }).click();
    await expect(page.getByText(/暂未连上电脑/)).toBeVisible();
    const restarted = await startRelay(relay.url.port);
    expect(restarted.url.origin).toBe(relay.url.origin);
    await expect(page.getByText('电脑已连接', { exact: true })).toBeVisible({ timeout: 17000 });
    await page.screenshot({ path: info.outputPath('手机同步完成.png'), fullPage: true });
    await page.unrouteAll({ behavior: 'wait' });

    await test.step('hands off the active timer and reconciles a phone interval recorded offline', async () => {
      type Active = { id: string; device: string; status: string };
      type State = { active: Active | null; lastSaved: Active | null };
      const status = () => api<State>('/timer');
      const deskStart = () => api<State>('/timer', 'POST', { action: 'start', lessonId: first.id });
      const original = (await deskStart()).active!;
      await page.getByRole('button', { name: '课程', exact: true }).click();
      await openCourse.click();
      await page.getByRole('button', { name: '开始学习计时', exact: true }).click();
      const banner = page.getByRole('region', { name: '本机计时', exact: true });
      await expect(banner.getByText('计时中', { exact: true })).toBeVisible();
      await expect.poll(async () => (await status()).active?.device).toBe('phone');
      expect((await status()).lastSaved?.id).toBe(original.id);
      await desktop.goto(new URL('/#/lessons/' + first.id, computer!.url).href);
      await expect(desktop.getByRole('link', { name: '返回计时课程' })).toContainText('手机');
      await page.screenshot({ path: info.outputPath('手机计时中.png'), fullPage: true });
      await expect(banner.locator('time')).not.toHaveText('00:00:00');
      await banner.getByRole('button', { name: '暂停计时' }).click();
      await expect.poll(async () => (await status()).active?.status).toBe('paused');
      await page.reload();
      await expect(banner.getByText('计时已暂停', { exact: true })).toBeVisible();
      await banner.getByRole('button', { name: '继续计时' }).click();
      await expect.poll(async () => (await status()).active?.status).toBe('running');

      await desktop.getByRole('button', { name: '结束上一计时并开始本节', exact: true }).first().click();
      await expect.poll(async () => (await status()).active?.device).toBe('desktop');
      const reclaimed = (await status()).active!;
      expect(reclaimed.id).not.toBe(original.id);
      await expect(banner).toHaveCount(0, { timeout: 12000 });

      await context.setOffline(true);
      await expect(page.getByText('离线', { exact: true })).toBeVisible();
      await openCourse.click();
      await page.getByRole('button', { name: '开始笔记计时', exact: true }).click();
      const displaySeconds = async () => (await banner.locator('time').innerText()).split(':')
        .reduce((total, unit) => total * 60 + Number(unit), 0);
      // Both timers must overlap after whole-second accounting and phone clock correction.
      await expect.poll(displaySeconds).toBeGreaterThanOrEqual(3);
      await api('/timer', 'POST', { action: 'finish', timerId: reclaimed.id });
      const elapsed = await displaySeconds();
      await expect.poll(displaySeconds).toBeGreaterThanOrEqual(elapsed + 2);
      await banner.getByRole('button', { name: '结束计时', exact: true }).click();
      await page.getByRole('button', { name: '同步', exact: true }).click();
      await expect(page.getByText('2 次计时操作待同步。', { exact: true })).toBeVisible();
      await page.screenshot({ path: info.outputPath('手机离线计时待发.png'), fullPage: true });

      const conflicts = () => api<{ records: { id: string; reason: string; seconds: number; overlaps: { timerId: string }[] }[] }>(
        '/mobile/timer/conflicts',
      );
      const exchange = relayOrigin + '/v1/**/exchange/*';
      let lostTimerReceipt: string | undefined;
      await page.route(exchange, async route => {
        if (lostTimerReceipt || !route.request().postDataBuffer()) return route.continue();
        const response = await route.fetch();
        if (response.ok()) {
          const [held] = (await conflicts()).records;
          if (held) {
            lostTimerReceipt = held.id;
            return route.abort('failed');
          }
        }
        await route.fulfill({ response });
      });
      await context.setOffline(false);
      await expect.poll(() => lostTimerReceipt, { timeout: 18000 }).toBeTruthy();
      await expect(page.getByText('1 次计时操作待同步。', { exact: true })).toBeVisible();
      await page.getByRole('button', { name: '同步到电脑', exact: true }).click();
      await expect(page.getByText(/次计时操作待同步/)).toHaveCount(0, { timeout: 14000 });
      const phoneReview = page.getByText(/个时段待核对/);
      await expect(phoneReview).toHaveText('1 个时段待核对。请打开电脑的管理中心。');
      await page.screenshot({ path: info.outputPath('手机重试后仅一项待核对.png'), fullPage: true });
      await page.unroute(exchange);
      const records = (await conflicts()).records;
      expect(records.map(item => item.id)).toEqual([lostTimerReceipt]);
      const [overlap] = records;
      expect(overlap.reason).toBe('overlap');
      expect(overlap.seconds).toBeGreaterThan(0);
      expect(overlap.overlaps.map(item => item.timerId)).toContain(reclaimed.id);
      await desktop.goto(new URL('/#/settings', computer!.url).href);
      const review = desktop.getByRole('heading', { name: '手机计时待核对' });
      await expect(review).toBeVisible();
      await desktop.getByText(/笔记 · 有重叠/).click();
      await expect(desktop.getByText(/电脑记录 ·/)).toBeVisible();
      await desktop.screenshot({ path: info.outputPath('电脑核对重叠计时.png'), fullPage: true });
      await desktop.getByRole('button', { name: '保留已计入时段' }).click();
      await expect(review).toHaveCount(0);
      await page.getByRole('button', { name: '同步到电脑', exact: true }).click();
      await expect(phoneReview).toHaveCount(0, { timeout: 14000 });
      console.log('PASS lost timer receipt replay preserved one desktop conflict and one phone review count; desktop resolution cleared both.');
      const data = await api<{ timeRecords: { category: string; seconds: number }[] }>('/study-report');
      expect(data.timeRecords.filter(entry => entry.category === 'notes')).toEqual([
        expect.objectContaining({ seconds: overlap.seconds }),
      ]);
    });

    await test.step('replays a paused offline timer in order after losing its pause receipt', async () => {
      const sync = page.getByRole('button', { name: '同步', exact: true });
      const send = page.getByRole('button', { name: '同步到电脑', exact: true });
      const banner = page.getByRole('region', { name: '本机计时', exact: true });
      const pending = page.getByText(/次计时操作待同步/);
      const displayedSeconds = async () => (await banner.locator('time').innerText()).split(':')
        .reduce((seconds, unit) => seconds * 60 + Number(unit), 0);
      await page.bringToFront();
      await expect(page.getByText('电脑已连接', { exact: true })).toBeVisible();
      await context.setOffline(true);
      await expect(page.getByText('离线', { exact: true })).toBeVisible();
      await page.getByRole('button', { name: '课程', exact: true }).click();
      await openCourse.click();
      await page.getByRole('button', { name: '开始笔记计时', exact: true }).click();
      await expect.poll(displayedSeconds).toBeGreaterThanOrEqual(2);
      await banner.getByRole('button', { name: '暂停计时' }).click();
      await expect(banner.getByText('计时已暂停', { exact: true })).toBeVisible();
      const pausedSeconds = await displayedSeconds();
      const pauseObserved = Date.now();
      await page.reload();
      await expect(banner.getByText('计时已暂停', { exact: true })).toBeVisible();
      await expect.poll(() => Date.now() - pauseObserved).toBeGreaterThanOrEqual(2200);
      expect(await displayedSeconds()).toBe(pausedSeconds);
      await banner.getByRole('button', { name: '继续计时' }).click();
      await expect.poll(displayedSeconds).toBeGreaterThanOrEqual(pausedSeconds + 2);
      await banner.getByRole('button', { name: '结束计时' }).click();
      await expect(banner).toHaveCount(0);
      await page.reload();
      await sync.click();
      await expect(pending).toHaveText('4 次计时操作待同步。');

      type Queued = { id: string; action: string; timerId: string;
        at: { wall: number; clock?: { offset: number } } };
      const queued = await page.evaluate(() => new Promise<Queued[]>((done, fail) => {
        const opened = indexedDB.open('zhixue-mobile-study', 3);
        opened.onerror = () => fail(opened.error);
        opened.onsuccess = () => {
          const database = opened.result;
          const request = database.transaction('timers').objectStore('timers').get('ledger');
          request.onsuccess = () => { database.close(); done(request.result.events); };
          request.onerror = () => { database.close(); fail(request.error); };
        };
      }));
      expect(queued.map(event => event.action)).toEqual(['start', 'pause', 'resume', 'finish']);
      expect(new Set(queued.map(event => event.id)).size).toBe(4);
      const [begin, pause, resume, finish] = queued;
      expect(new Set(queued.map(event => event.timerId))).toEqual(new Set([begin.timerId]));
      expect(resume.at.wall - pause.at.wall).toBeGreaterThanOrEqual(2200);
      const secondAt = ({ at }: Queued) => {
        expect(at.clock).toBeDefined();
        return Math.floor((at.wall + at.clock!.offset) / 1000);
      };
      const firstSeconds = secondAt(pause) - secondAt(begin);
      const secondSeconds = secondAt(finish) - secondAt(resume);
      const { directory } = await api<{ directory: string }>('/setup');
      const database = join(directory, 'zhixue.sqlite3');
      type Receipt = { id: string; action: string; seconds: number; state: string };
      const receipts = (): Receipt[] => JSON.parse(execFileSync(python, ['-c', [
        'import json, sqlite3, sys',
        'from contextlib import closing',
        'from pathlib import Path',
        'with closing(sqlite3.connect(Path(sys.argv[1]).as_uri() + "?mode=ro", uri=True, timeout=1)) as db:',
        '    rows = db.execute("""SELECT id, details, reply FROM mobile_changes',
        '        WHERE kind = ? AND json_extract(details, \'$.timerId\') = ? ORDER BY received_at, id""", ("timer", sys.argv[2]))',
        '    print(json.dumps([{"action": json.loads(details)["action"], **json.loads(reply)} for _, details, reply in rows]))',
      ].join('\n'), database, begin.timerId], { encoding: 'utf8', timeout: 8000, windowsHide: true }));
      const total = async () => {
        const report = await api<{ timeRecords: { timerId: string; category: string; seconds: number }[] }>(
          '/study-report?period=month',
        );
        return report.timeRecords.filter(record => record.timerId === begin.timerId && record.category === 'notes')
          .reduce((sum, record) => sum + record.seconds, 0);
      };
      expect(await total()).toBe(0);
      const exchange = relayOrigin + '/v1/**/exchange/*';
      let beforeRetry: Receipt[] | undefined;
      let retry = false;
      await page.route(exchange, async route => {
        if (beforeRetry) return retry ? route.continue() : route.abort('failed');
        if (!route.request().postDataBuffer()) return route.continue();
        const response = await route.fetch();
        if (response.ok()) {
          // The report includes finished timers only. Observe the committed pause in the isolated database.
          const observed = receipts();
          if (observed.some(receipt => receipt.id === pause.id)) {
            beforeRetry = observed;
            return route.abort('failed');
          }
        }
        await route.fulfill({ response });
      });
      try {
        await context.setOffline(false);
        await expect.poll(() => beforeRetry, { timeout: 18000 }).toEqual([
          { id: begin.id, action: 'start', state: 'saved', seconds: 0 },
          { id: pause.id, action: 'pause', state: 'saved', seconds: firstSeconds },
        ]);
        await expect(pending).toHaveText('3 次计时操作待同步。');
        expect(await total()).toBe(0);
        retry = true;
        await send.click();
        await expect(pending).toHaveCount(0, { timeout: 16000 });
        const finalReceipts = receipts();
        expect(finalReceipts).toEqual([
          ...beforeRetry!,
          { id: resume.id, action: 'resume', state: 'saved', seconds: 0 },
          { id: finish.id, action: 'finish', state: 'saved', seconds: secondSeconds },
        ]);
        expect(await total()).toBe(firstSeconds + secondSeconds);
        expect((await api<{ active: unknown }>('/timer')).active).toBeNull();
        await expect(page.getByText(/个时段待核对/)).toHaveCount(0);
        writeFileSync(info.outputPath('手机离线四步计时.json'), JSON.stringify({
          timer: begin.timerId, actions: queued.map(({ id, action }) => ({ id, action })),
          lostReply: pause.id, firstSeconds, secondSeconds, pausedMilliseconds: resume.at.wall - pause.at.wall,
          recordedSeconds: await total(), beforeRetry, finalReceipts,
        }, null, 2));
        console.log('PASS four offline timer operations replayed once after pause receipt loss, excluding the paused gap');
      } finally {
        await page.unroute(exchange);
      }
    });

    await test.step('delivers a daily reminder on one device and shares decisions and settings', async () => {
      const phoneCard = page.getByRole('region', { name: '每日学习提醒', exact: true });
      const pcCard = desktop.getByRole('region', { name: '每日学习提醒', exact: true });
      const preferences = page.locator('details').filter({ has: page.getByText('提醒设置', { exact: true }) });
      await page.bringToFront();
      await preferences.getByText('提醒设置', { exact: true }).click();
      const enable = preferences.getByRole('checkbox', { name: '启用学习提醒' });
      await expect(enable).not.toBeChecked();
      await enable.check();
      await preferences.getByRole('button', { name: '保存提醒设置' }).click();
      await expect(preferences.getByText('提醒设置已保存。')).toBeVisible();
      await expect(phoneCard.getByText(/项复习到期/)).toBeVisible({ timeout: 12000 });
      await page.screenshot({ path: info.outputPath('手机提醒.png'), fullPage: true });

      await desktop.goto(new URL('/#/reviews', computer!.url).href);
      const desktopChecked = desktop.waitForResponse(reply => reply.url().endsWith('/api/reminders/check'));
      await desktop.bringToFront();
      await desktop.reload();
      expect((await (await desktopChecked).json()).reminders).toEqual([]);
      await expect(pcCard).toHaveCount(0);
      const original = await api<{ reviews: { key: string; dueOn: string }[] }>('/reviews');
      const timer = await api<{ active: unknown }>('/timer');
      await page.bringToFront();
      await phoneCard.getByRole('button', { name: '查看到期复习' }).click();
      await expect(page.getByRole('button', { name: '复习', exact: true })).toHaveAttribute('aria-pressed', 'true');
      expect((await api<{ active: unknown }>('/timer')).active).toEqual(timer.active);
      await phoneCard.getByRole('button', { name: '明天', exact: true }).click();
      await expect(phoneCard).toHaveCount(0);
      expect((await api<{ reminders: unknown[] }>('/reminders/check', 'POST', { trigger: 'foreground' })).reminders).toEqual([]);

      await api('/demo/next-day', 'POST');
      await desktop.bringToFront();
      await desktop.reload();
      await expect(pcCard.getByText(/项复习到期/)).toBeVisible({ timeout: 12000 });
      expect((await api<{ reviews: { key: string; dueOn: string }[] }>('/reviews')).reviews.map(row => ({ key: row.key, dueOn: row.dueOn })))
        .toEqual(original.reviews.map(row => ({ key: row.key, dueOn: row.dueOn })));
      await expect(phoneCard).toHaveCount(0);
      await desktop.screenshot({ path: info.outputPath('电脑接续稍后提醒.png'), fullPage: true });

      await page.bringToFront();
      await page.getByRole('button', { name: '同步', exact: true }).click();
      await preferences.getByText('提醒设置', { exact: true }).click();
      await enable.uncheck();
      await preferences.getByRole('checkbox', { name: '使用免打扰时段' }).check();
      await preferences.getByLabel('开始时间', { exact: true }).fill('22:00');
      await preferences.getByLabel('结束时间', { exact: true }).fill('07:00');
      await preferences.getByRole('button', { name: '保存提醒设置' }).click();
      await expect(preferences.getByText('提醒设置已保存。')).toBeVisible();
      expect(await api('/reminders/settings')).toEqual({ enabled: false, quiet: { start: '22:00', end: '07:00' } });
      await desktop.bringToFront();
      await expect(pcCard).toHaveCount(0, { timeout: 15000 });
    });

    await test.step('keeps the old phone outbox separate after encrypted restore and re-pairing', async () => {
      const getCatalog = () => api<{ workspace: string; lessons: { id: string }[] }>('/mobile/catalog');
      const readNote = () => api<{ content: string; revision: string }>(lesson + '/note');
      const getPair = () => api<{ paired: { name: string } | null }>('/device-sync');
      const originalWorkspace = (await getCatalog()).workspace;
      const savedNote = await readNote();
      expect((await getPair()).paired?.name).toBe('隔离测试手机');
      const password = 'isolated workspace restore fixture';
      const archive = await api<{ id: string; status: string }>('/backups', 'POST', { password });
      expect(archive.status).toBe('ready');

      const oldDraft = savedNote.content + '\n恢复前仅留在手机的笔记。\n';
      const sync = page.getByRole('button', { name: '同步', exact: true });
      const courses = page.getByRole('button', { name: '课程', exact: true });
      await page.bringToFront();
      await context.setOffline(true);
      await expect(page.getByText('离线', { exact: true })).toBeVisible();
      await courses.click();
      await openCourse.click();
      await page.getByRole('textbox', { name: '本节笔记' }).fill(oldDraft);
      await page.getByRole('button', { name: '保存到本机' }).click();
      await expect(page.getByText('笔记已保存到本机', { exact: true })).toBeVisible();
      await page.getByRole('button', { name: '开始学习计时', exact: true }).click();
      await page.getByRole('region', { name: '本机计时' }).getByRole('button', { name: '结束计时', exact: true }).click();
      await sync.click();
      await expect(page.getByText('1 项待同步', { exact: true })).toBeVisible();
      await expect(page.getByText('2 次计时操作待同步。', { exact: true })).toBeVisible();

      type Outbox = { timers: { id: string; action: string; workspace: string }[];
        records: { key: string; snapshot: { workspace: string; id: string }; note?: { content: string; editedAt?: string } }[] };
      const outbox = (): Promise<Outbox> => page.evaluate(async () => {
        const opened = indexedDB.open('zhixue-mobile-study', 3);
        const db = await new Promise<IDBDatabase>((done, fail) => {
          opened.onsuccess = () => done(opened.result);
          opened.onerror = () => fail(opened.error);
        });
        try {
          return await new Promise<Outbox>((done, fail) => {
            const tx = db.transaction(['items', 'timers']);
            const records = tx.objectStore('items').getAll();
            const ledger = tx.objectStore('timers').get('ledger');
            tx.oncomplete = () => done({ timers: ledger.result.events, records: records.result });
            tx.onabort = () => fail(tx.error);
          });
        } finally { db.close(); }
      });
      const before = await outbox();
      expect(before.timers.map(event => [event.action, event.workspace])).toEqual([
        ['start', originalWorkspace], ['finish', originalWorkspace],
      ]);
      const oldLesson = before.records.find(record => record.snapshot.id === first.id &&
        record.snapshot.workspace === originalWorkspace && record.note?.content === oldDraft);
      expect(oldLesson?.note?.editedAt).toBeTruthy();

      await api(lesson + '/note', 'PUT', {
        content: '恢复后应消失的电脑修改。', baseRevision: savedNote.revision,
      });
      const base = '/backups/' + archive.id;
      const preview = await api<{ restore: { activeTimer: boolean; confirmation: string } }>(base + '/preview', 'POST', {
        password,
      });
      expect(preview.restore.activeTimer).toBeFalsy();
      expect(preview.restore.confirmation).toMatch(/^[0-9a-f]{64}$/);
      const requested = await api<{ id: string }>(base + '/restore', 'POST', {
        password, confirmation: preview.restore.confirmation,
      });
      const restoreStatus = () => api<{ id?: string; state: string; rollbackBackupId?: string }>('/backups/restore-status');
      await expect.poll(async () => (await restoreStatus()).state, { timeout: 30000 }).toBe('complete');
      expect(await restoreStatus()).toMatchObject({ id: requested.id, rollbackBackupId: expect.any(String) });
      const currentWorkspace = (await getCatalog()).workspace;
      expect(currentWorkspace).not.toBe(originalWorkspace);
      expect(await readNote()).toMatchObject(savedNote);
      expect((await getPair()).paired).toBeNull();

      await context.setOffline(false);
      await expect(page.getByText(/暂未连上电脑|配对已撤销、到期或工作区已更换/)).toBeVisible({ timeout: 15000 });
      await expect(page.getByText('1 项待同步', { exact: true })).toBeVisible();
      await expect(page.getByText('2 次计时操作待同步。', { exact: true })).toBeVisible();
      expect((await outbox()).timers).toEqual(before.timers);
      console.log('PASS actual encrypted restore changed the workspace and disconnected the previous approval');

      await showPairing();
      await desktop.getByLabel('手机网页地址').fill(hosted.url.origin);
      await desktop.getByLabel('中继地址').fill(relayOrigin);
      await desktop.getByRole('button', { name: '生成配对链接' }).click();
      await expect(invite).toBeVisible();
      const newInvite = await invite.inputValue();
      const invitation = (value: string) => JSON.parse(Buffer.from(
        new URLSearchParams(new URL(value).hash.split('?')[1]).get('pair')!, 'base64url',
      ).toString());
      expect(invitation(newInvite).room).not.toBe(invitation(inviteUrl).room);
      expect(invitation(newInvite).publicKey).not.toBe(invitation(inviteUrl).publicKey);
      await page.evaluate(() => { document.documentElement.dataset.existingPhone = 'yes'; });
      await page.goto(newInvite);
      await expect(page.getByLabel('此手机的名称')).toBeVisible({ timeout: 8000 });
      await expect(page.locator('html')).toHaveAttribute('data-existing-phone', 'yes');
      expect(new URL(page.url()).hash).toBe('#/');
      await page.getByLabel('此手机的名称').fill('恢复后复配手机');
      await page.getByRole('button', { name: '申请配对' }).click();
      await expect(phoneCode).toHaveText(/^[0-9A-F]{8}$/);
      await expect(desktop.getByLabel('电脑配对码')).toHaveText(await phoneCode.innerText(), { timeout: 10000 });
      await desktop.getByRole('button', { name: '确认配对码并批准' }).click();
      await expect(desktop.getByText('已配对 恢复后复配手机', { exact: true })).toBeVisible({ timeout: 12000 });
      await expect(page.getByText('电脑已连接', { exact: true })).toBeVisible({ timeout: 15000 });
      await sync.click();
      await expect(page.getByText('这台设备还保存了其他工作区的待同步记录。')).toBeVisible();
      await page.getByRole('button', { name: '同步到电脑', exact: true }).click();
      await expect(page.getByRole('button', { name: '同步到电脑', exact: true })).toBeEnabled();
      const after = await outbox();
      expect(after.timers).toEqual(before.timers);
      expect(after.records.find(record => record.key === oldLesson?.key)).toEqual(oldLesson);
      expect(await readNote()).toMatchObject(savedNote);
      for (const event of before.timers) {
        const receipt = new URL('/api/mobile/changes/' + event.id, computer!.url);
        receipt.search = new URLSearchParams({ workspace: currentWorkspace }).toString();
        expect((await desktopBrowser.request.get(receipt.href, { headers: secretHeaders() })).status()).toBe(404);
      }
      expect((await api<{ active: unknown }>('/timer')).active).toBeNull();
      await courses.click();
      await expect(course).toBeEnabled();
      await course.click();
      await expect(course).toHaveCount(0);
      const downloaded = await outbox();
      expect(downloaded.records.find(record => record.key === oldLesson?.key)).toEqual(oldLesson);
      expect(downloaded.records.find(record => record.snapshot.id === first.id && record.snapshot.workspace === currentWorkspace)
        ?.note?.content).toBe(savedNote.content);
      await openCourse.filter({ hasNotText: '本机笔记待同步' }).click();
      const currentDraft = savedNote.content + '\n重新配对后确认同步的笔记。\n';
      await page.getByRole('textbox', { name: '本节笔记' }).fill(currentDraft);
      await page.getByRole('button', { name: '保存到本机' }).click();
      await sync.click();
      await page.getByRole('button', { name: '同步到电脑', exact: true }).click();
      await expect.poll(async () => (await readNote()).content, { timeout: 14000 }).toBe(currentDraft);
      await expect(page.getByText('1 项待同步', { exact: true })).toBeVisible();
      expect((await outbox()).records.find(record => record.key === oldLesson?.key)).toEqual(oldLesson);
      writeFileSync(info.outputPath('恢复后手机工作区隔离.json'), JSON.stringify({
        restoreId: requested.id, originalWorkspace, currentWorkspace,
        freshInvitation: true, oldNoteRetained: true, currentNoteMatchesBackup: true, currentNoteSynced: true,
        retainedTimerActions: after.timers.map(({ id, action }) => ({ id, action })), receivedOldTimerEvents: 0,
      }, null, 2));
      console.log('PASS re-pairing restored the course while leaving the old draft and timer events unsent');
    });

    await showPairing();
    await desktop.getByRole('button', { name: '撤销手机配对' }).click();
    await expect(desktop.getByRole('button', { name: '生成配对链接' })).toBeVisible();
    await page.getByRole('button', { name: '同步到电脑', exact: true }).click();
    await expect(page.getByText(/暂未连上电脑/)).toBeVisible({ timeout: 15000 });
    await page.getByRole('button', { name: '课程', exact: true }).click();
    await expect(openCourse).toHaveCount(2);
    await test.step('removing the local pairing discards an in-flight catalog response', async () => {
      await desktop.getByLabel('手机网页地址').fill(hosted.url.origin);
      await desktop.getByLabel('中继地址').fill(relayOrigin);
      await desktop.getByRole('button', { name: '生成配对链接' }).click();
      await expect(invite).toBeVisible();
      await page.goto(await invite.inputValue());
      await page.getByRole('button', { name: '申请配对' }).click();
      await expect(phoneCode).toHaveText(/^[0-9A-F]{8}$/);
      await expect(desktop.getByLabel('电脑配对码')).toHaveText(await phoneCode.innerText());
      await desktop.getByRole('button', { name: '确认配对码并批准' }).click();
      await expect(page.getByText('电脑已连接', { exact: true })).toBeVisible({ timeout: 15000 });
      const peer = await context.newPage();
      await peer.goto(new URL('/mobile/', hosted.url).href);
      await expect(peer.getByText('电脑已连接', { exact: true })).toBeVisible({ timeout: 15000 });
      await peer.getByRole('button', { name: '同步', exact: true }).click();
      async function holdCatalog(target: Page) {
        const syncNow = target.getByRole('button', { name: '同步到电脑', exact: true });
        await expect(syncNow).toBeEnabled();
  
        // Hold the actual decrypted catalog in the phone, after the old computer has answered.
        await target.evaluate(() => {
          const original = crypto.subtle.decrypt;
          crypto.subtle.decrypt = async function (...args: Parameters<SubtleCrypto['decrypt']>) {
            const plaintext = await original.apply(this, args);
            const reply = JSON.parse(new TextDecoder().decode(plaintext));
            const manualSync = Array.from(document.querySelectorAll('button')).some(button =>
              button.disabled && button.textContent === '正在同步');
            if (reply.status === 200 && Array.isArray(reply.data?.lessons) && manualSync) {
              crypto.subtle.decrypt = original;
              await new Promise<void>(resolve => {
                window.addEventListener('release-test-catalog', () => resolve(), { once: true });
                document.documentElement.dataset.heldCatalog = 'yes';
              });
            }
            return plaintext;
          };
        });
        await expect(async () => {
          if (await target.locator('html').getAttribute('data-held-catalog') !== 'yes' && await syncNow.isEnabled({ timeout: 1000 })) {
            await syncNow.click({ timeout: 1000 });
          }
          await expect(target.locator('html')).toHaveAttribute('data-held-catalog', 'yes', { timeout: 1200 });
        }).toPass({ timeout: 15000 });
        await expect(target.getByRole('button', { name: '正在同步' })).toBeDisabled();
      }
      await holdCatalog(page);
      await holdCatalog(peer);
      const receiptPhone = await context.newPage();
      await receiptPhone.addInitScript(() => {
        const interval = window.setInterval.bind(window);
        window.setInterval = (handler, delay, ...args) =>
          interval(handler, delay === 2500 || delay === 10000 ? 600000 : delay, ...args);
        window.addEventListener('focus', event => event.stopImmediatePropagation(), { capture: true });
      });
      await receiptPhone.goto(new URL('/mobile/', hosted.url).href);
      await receiptPhone.bringToFront();
      await expect(receiptPhone.getByText('电脑已连接', { exact: true })).toBeVisible({ timeout: 15000 });
      const receiptTabs = receiptPhone.getByRole('navigation', { name: '手机学习' });
      await receiptTabs.getByRole('button', { name: '同步', exact: true }).click();
      const syncReceipt = receiptPhone.getByRole('button', { name: '同步到电脑', exact: true });
      await expect(async () => {
        await syncReceipt.click({ timeout: 1000 });
        await expect(receiptPhone.getByRole('button', { name: '正在同步' })).toBeDisabled({ timeout: 800 });
      }).toPass({ timeout: 15000 });
      await expect(syncReceipt).toBeEnabled({ timeout: 15000 });
      await receiptTabs.getByRole('button', { name: '课程', exact: true }).click();
      await receiptPhone.getByRole('button', { name: '打开课程 ' + first.title, exact: true })
        .filter({ hasNotText: '本机笔记待同步' }).click();
      const body = receiptPhone.getByRole('textbox', { name: '本节笔记' });
      const heldDraft = await body.inputValue() + '\n撤销配对时尚未收到回执。';
      await body.fill(heldDraft);
      await receiptPhone.getByRole('button', { name: '保存到本机' }).click();
      await expect(receiptPhone.getByText('笔记已保存到本机', { exact: true })).toBeVisible();
      await receiptPhone.getByRole('button', { name: '同步', exact: true }).click();
      await receiptPhone.evaluate(() => {
        const original = crypto.subtle.decrypt;
        crypto.subtle.decrypt = async function (...args: Parameters<SubtleCrypto['decrypt']>) {
          const plaintext = await original.apply(this, args);
          const response = JSON.parse(new TextDecoder().decode(plaintext));
          if (response.status === 200 && response.data?.state === 'saved' && response.data?.submittedRevision) {
            crypto.subtle.decrypt = original;
            await new Promise<void>(resolve => {
              window.addEventListener('release-test-note-receipt', () => resolve(), { once: true });
              document.documentElement.dataset.heldNoteReceipt = 'yes';
            });
          }
          return plaintext;
        };
      });
      const currentWorkspace = (await api<{ workspace: string }>('/mobile/catalog')).workspace;
      const localNote = (target = receiptPhone) => target.evaluate(key => new Promise<{ content: string; editedAt?: string; pending?: { id: string } }>((done, fail) => {
        const open = indexedDB.open('zhixue-mobile-study', 3);
        open.onerror = () => fail(open.error);
        open.onsuccess = () => {
          const db = open.result;
          const tx = db.transaction('items');
          const request = tx.objectStore('items').get(key);
          request.onsuccess = () => done(request.result.note);
          request.onerror = () => fail(request.error);
          tx.oncomplete = () => db.close();
        };
      }), currentWorkspace + ':lesson:' + first.id);
      await expect(async () => {
        if (!(await receiptPhone.locator('html').getAttribute('data-held-note-receipt'))) {
          await syncReceipt.click({ timeout: 1000 });
        }
        await expect(receiptPhone.locator('html')).toHaveAttribute('data-held-note-receipt', 'yes', { timeout: 1500 });
      }).toPass({ timeout: 15000 });
      await expect(receiptPhone.getByRole('button', { name: '正在同步' })).toBeDisabled();
      const committedNote = await api<{ content: string; revision: string }>(lesson + '/note');
      expect(committedNote.content).toBe(heldDraft);
      const waiting = await localNote();
      expect(waiting).toMatchObject({ content: heldDraft, editedAt: expect.any(String), pending: { id: expect.any(String) } });
      await page.getByRole('button', { name: '移除本机配对' }).click();
      await expect(page.getByText('等待连接', { exact: true })).toBeVisible();
      await expect(page.getByText('请在手机上打开电脑生成的配对链接。')).toBeVisible();

      await expect(peer.getByText('等待连接', { exact: true }), 'the other phone page clears its connection while its own catalog is held').toBeVisible();
      await expect(peer.getByRole('button', { name: '正在同步' })).toBeDisabled();
      await expect(peer.getByText('请在手机上打开电脑生成的配对链接。')).toBeVisible();
      await expect(receiptPhone.getByText('等待连接', { exact: true })).toBeVisible();
      await receiptPhone.evaluate(() => window.dispatchEvent(new Event('release-test-note-receipt')));
      await expect(syncReceipt).toBeEnabled();
      expect(await localNote(), 'the previous pairing receipt must leave the submitted local draft and its ID intact').toEqual(waiting);
      await receiptPhone.close();
      console.log('PASS a note receipt already produced by the computer leaves its original local outbox record after unpairing');

      for (const phonePage of [page, peer]) {
        await phonePage.evaluate(() => window.dispatchEvent(new Event('release-test-catalog')));
        await expect(phonePage.getByRole('button', { name: '同步到电脑', exact: true })).toBeEnabled();
      }
      await expect(peer.getByText('等待连接', { exact: true })).toBeVisible();
      await peer.getByRole('button', { name: '课程', exact: true }).click();
      await expect(peer.getByRole('heading', { name: '从电脑下载课程' })).toHaveCount(0);
      await expect(peer.getByRole('button', { name: '打开课程 ' + first.title, exact: true })).toHaveCount(2);
      await peer.close();
      await expect(page.getByText('等待连接', { exact: true })).toBeVisible();
      await page.getByRole('button', { name: '课程', exact: true }).click();
      await expect(page.getByRole('heading', { name: '从电脑下载课程' })).toHaveCount(0);
      await expect(openCourse).toHaveCount(2);
      writeFileSync(info.outputPath('手机移除配对与延迟回执.json'), JSON.stringify({
        heldGenuineCatalog: true, unpairedBeforeDelivery: true,
        unpairedAfterDelivery: true, remoteDownloadVisible: false, localCoursesRetained: 2,
      }, null, 2));
      writeFileSync(info.outputPath('手机双页面配对隔离.json'), JSON.stringify({
        twoPagesHeldRealCatalog: true, peerDisconnectedBeforeDelivery: true,
        peerDisconnectedAfterDelivery: true, peerLocalCoursesRetained: 2,
      }, null, 2));
      console.log('PASS a second phone page removed the old connection before its held catalog returned');
      console.log('PASS delayed catalog did not reconnect the unpaired phone or hide local courses');

      await showPairing();
      await desktop.getByRole('button', { name: '撤销手机配对' }).click();
      await desktop.getByLabel('手机网页地址').fill(hosted.url.origin);
      await desktop.getByLabel('中继地址').fill(relayOrigin);
      await desktop.getByRole('button', { name: '生成配对链接' }).click();
      await page.goto(await invite.inputValue());
      await page.getByRole('button', { name: '申请配对' }).click();
      await expect(phoneCode).toHaveText(/^[0-9A-F]{8}$/);
      await expect(desktop.getByLabel('电脑配对码')).toHaveText(await phoneCode.innerText());
      await desktop.getByRole('button', { name: '确认配对码并批准' }).click();
      await expect(page.getByText('电脑已连接', { exact: true })).toBeVisible({ timeout: 15000 });
      await expect.poll(async () => (await localNote(page)).pending ?? null, { timeout: 15000 }).toBeNull();
      expect(await localNote(page)).toMatchObject({ content: heldDraft, revision: committedNote.revision });
      expect(await api(lesson + '/note')).toMatchObject(committedNote);
      const recovered = await api<{ id: string; state: string }>('/mobile/changes/' + waiting.pending!.id +
        '?' + new URLSearchParams({ workspace: currentWorkspace }));
      expect(recovered).toMatchObject({ id: waiting.pending!.id, state: 'saved' });
      writeFileSync(info.outputPath('移除配对后保留笔记回执.json'), JSON.stringify({
        receiptProducedBeforeRemoval: true, localPendingPreserved: true, replayedSameOperationId: recovered.id,
        originalRevisionPreserved: true, workspace: currentWorkspace,
      }, null, 2));
      console.log('PASS a new approval reconciles the original note operation without another revision');
    });
    await test.step('cleans up a subscription created after replacing the pairing', async () => {
      await api('/reminders/settings', 'PUT', { enabled: false, quiet: null });
      await context.grantPermissions(['notifications'], { origin: hosted.url.origin });
      await page.getByRole('button', { name: '同步', exact: true }).click();
      await page.evaluate(() => {
        let subscription: PushSubscription | null = null;
        let requests = 0;
        const base64 = (bytes: Uint8Array) => btoa(String.fromCharCode(...bytes))
          .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
        PushManager.prototype.getSubscription = async () => subscription;
        PushManager.prototype.subscribe = async options => {
          const number = ++requests;
          if (number === 1) {
            await new Promise<void>(resolve => {
              window.addEventListener('release-old-push-subscription', () => resolve(), { once: true });
              document.documentElement.dataset.oldPushRequested = 'yes';
            });
          }
          const endpoint = 'https://fcm.googleapis.com/fcm/send/zhixue-pair-replacement-fixture-' + number;
          const keys = await crypto.subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, true, ['deriveBits']);
          const publicKey = new Uint8Array(await crypto.subtle.exportKey('raw', keys.publicKey));
          const created = {
            endpoint, options: { applicationServerKey: (options?.applicationServerKey as Uint8Array).buffer },
            toJSON: () => ({ endpoint, expirationTime: null, keys: {
              p256dh: base64(publicKey), auth: base64(new Uint8Array(16)),
            } }),
            unsubscribe: async () => {
              if (subscription === created) subscription = null;
              return true;
            },
          } as PushSubscription;
          subscription = created;
          if (number === 1) document.documentElement.dataset.oldPushCreated = 'yes';
          return created;
        };
      });
      await page.getByText('手机系统推送', { exact: true }).click();
      await page.getByRole('button', { name: '启用手机推送', exact: true }).click({ timeout: 12000 });
      await expect(page.locator('html')).toHaveAttribute('data-old-push-requested', 'yes');
      await page.getByRole('button', { name: '移除本机配对', exact: true }).click();
      await expect(page.getByText('等待连接', { exact: true })).toBeVisible();
      await showPairing();
      await desktop.getByRole('button', { name: '撤销手机配对' }).click();
      await desktop.getByLabel('手机网页地址').fill(hosted.url.origin);
      await desktop.getByLabel('中继地址').fill(relayOrigin);
      await desktop.getByRole('button', { name: '生成配对链接' }).click();
      await page.goto(await invite.inputValue());
      await page.getByRole('button', { name: '申请配对' }).click();
      await expect(phoneCode).toHaveText(/^[0-9A-F]{8}$/);
      await expect(desktop.getByLabel('电脑配对码')).toHaveText(await phoneCode.innerText());
      await desktop.getByRole('button', { name: '确认配对码并批准' }).click();
      await expect(page.getByText('电脑已连接', { exact: true })).toBeVisible({ timeout: 15000 });

      await page.evaluate(() => window.dispatchEvent(new Event('release-old-push-subscription')));
      await expect(page.locator('html')).toHaveAttribute('data-old-push-created', 'yes');
      const currentEndpoint = () => page.evaluate(async () =>
        (await (await navigator.serviceWorker.ready).pushManager.getSubscription())?.endpoint ?? null);
      await expect.poll(currentEndpoint, { message: 'the delayed browser approval must not leave its obsolete subscription' }).toBeNull();
      await page.getByText('手机系统推送', { exact: true }).click();
      const enableReplacement = page.getByRole('button', { name: '启用手机推送', exact: true });
      await expect(enableReplacement).toBeEnabled({ timeout: 12000 });
      await expect(page.getByRole('button', { name: '停用手机推送', exact: true })).toHaveCount(0);
      await enableReplacement.click();
      await expect(page.getByText('已启用手机系统推送。')).toBeVisible({ timeout: 12000 });
      expect(await currentEndpoint()).toMatch(/fixture-2$/);
      writeFileSync(info.outputPath('旧配对推送清理.json'), JSON.stringify({
        approvalCompletedAfterReplacement: true, obsoleteSubscriptionRemoved: true,
        obsoleteEndpointNotRegisteredOnReplacement: true, replacementSubscribed: true,
      }, null, 2));
      await page.getByRole('button', { name: '停用手机推送', exact: true }).click();
      await expect.poll(currentEndpoint).toBeNull();
      console.log('PASS delayed browser approval was discarded and the replacement pairing subscribed independently');
    });
    await test.step('discards delayed course and review downloads after re-pairing in the same workspace', async () => {
      const third = ready.lessons[2];
      const second = ready.lessons[1];
      await api('/lessons/' + second.id + '/learned', 'PUT', { learned: true });
      await api('/demo/next-day', 'POST');
      const listing = await api<{ workspace: string; reviews: { id: string; title: string; lessonId: string }[] }>('/mobile/catalog');
      const review = listing.reviews.find(choice => choice.lessonId === second.id);
      expect(review, 'the isolated computer has prepared the second lesson review').toBeDefined();

      const mobileAddress = new URL('/mobile/', hosted.url).href;
      const reviewPage = await context.newPage();
      const control = await context.newPage();
      try {
        for (const phonePage of [reviewPage, control]) await phonePage.goto(mobileAddress);
        await page.getByRole('button', { name: '课程', exact: true }).click();
        await reviewPage.getByRole('button', { name: '复习', exact: true }).click();
        await control.getByRole('button', { name: '同步', exact: true }).click();
        const downloads = [
          { page, choice: third, kind: 'lesson', label: '课程' },
          { page: reviewPage, choice: review!, kind: 'review', label: '题组' },
        ];
        const downloadButton = (target: typeof downloads[number]) =>
          target.page.getByRole('button', { name: '下载' + target.label + ' ' + target.choice.title, exact: true });
        const openButton = (target: typeof downloads[number]) =>
          target.page.getByRole('button', { name: '打开' + target.label + ' ' + target.choice.title, exact: true });
        for (const target of downloads) {
          await expect(downloadButton(target)).toBeEnabled({ timeout: 16000 });
          await target.page.evaluate(({ id, workspace }) => {
            const decrypt = crypto.subtle.decrypt;
            crypto.subtle.decrypt = async function (...args: Parameters<SubtleCrypto['decrypt']>) {
              const result = await decrypt.apply(this, args);
              const payload = JSON.parse(new TextDecoder().decode(result));
              if (payload.status === 200 && payload.data?.id === id && payload.data.workspace === workspace) {
                crypto.subtle.decrypt = decrypt;
                await new Promise<void>(release => {
                  window.addEventListener('release-test-download', () => release(), { once: true });
                  document.documentElement.dataset.heldDownload = 'yes';
                });
              }
              return result;
            };
          }, { id: target.choice.id, workspace: listing.workspace });
          await downloadButton(target).click();
          await expect(target.page.locator('html')).toHaveAttribute('data-held-download', 'yes');
        }
        await control.getByRole('button', { name: '移除本机配对', exact: true }).click();
        for (const target of downloads) await expect(target.page.getByText('等待连接', { exact: true })).toBeVisible();
        await showPairing();
        await desktop.getByRole('button', { name: '撤销手机配对' }).click();
        await desktop.getByLabel('手机网页地址').fill(hosted.url.origin);
        await desktop.getByLabel('中继地址').fill(relayOrigin);
        await desktop.getByRole('button', { name: '生成配对链接' }).click();
        await control.goto(await invite.inputValue());
        await control.getByRole('button', { name: '申请配对' }).click();
        const newCode = control.getByLabel('手机配对码');
        await expect(newCode).toHaveText(/^[0-9A-F]{8}$/);
        await expect(desktop.getByLabel('电脑配对码')).toHaveText(await newCode.innerText());
        await desktop.getByRole('button', { name: '确认配对码并批准' }).click();
        await expect(control.getByText('电脑已连接', { exact: true })).toBeVisible({ timeout: 15000 });
        expect((await api<{ workspace: string }>('/mobile/catalog')).workspace).toBe(listing.workspace);

        for (const target of downloads) {
          await target.page.bringToFront();
          await expect(downloadButton(target)).toBeDisabled({ timeout: 16000 });
          await target.page.evaluate(() => window.dispatchEvent(new Event('release-test-download')));
          await expect(downloadButton(target).or(openButton(target))).toBeEnabled({ timeout: 12000 });
        }
        const records = await page.evaluate(() => new Promise<string[]>((resolve, reject) => {
          const request = indexedDB.open('zhixue-mobile-study');
          request.onerror = () => reject(request.error);
          request.onsuccess = () => {
            const db = request.result;
            const tx = db.transaction('items');
            const stored = tx.objectStore('items').getAllKeys();
            tx.oncomplete = () => { db.close(); resolve(stored.result as string[]); };
            tx.onabort = () => { db.close(); reject(tx.error); };
          };
        }));
        const keys = downloads.map(target => [listing.workspace, target.kind, target.choice.id].join(':'));
        expect(records.filter(key => keys.includes(key)), 'the old course and paper downloads must leave no offline rows').toEqual([]);
        expect(records.length, 'earlier offline work is still present').toBeGreaterThan(0);

        for (const target of downloads) {
          await target.page.bringToFront();
          await downloadButton(target).click();
          await expect(openButton(target)).toBeVisible({ timeout: 12000 });
        }
        writeFileSync(info.outputPath('旧配对下载隔离.json'), JSON.stringify({
          sameWorkspaceAfterReplacement: true, genuineCourseAndReviewHeld: true,
          obsoleteDownloadsDiscarded: true, previousOfflineRowsRetained: true, replacementDownloadsSucceeded: true,
        }, null, 2));
        console.log('PASS delayed course and review from a removed pairing left no offline rows; replacement downloads succeeded');
      } finally {
        await reviewPage.close();
        await control.close();
      }
    });
    await test.step('rejects an invitation that finishes preparation after a second page has paired', async () => {
      const other = await context.newPage();
      try {
        await showPairing();
        await desktop.getByRole('button', { name: '撤销手机配对' }).click();
        await desktop.getByLabel('手机网页地址').fill(hosted.url.origin);
        await desktop.getByLabel('中继地址').fill(relayOrigin);
        await desktop.getByRole('button', { name: '生成配对链接' }).click();
        const address = await invite.inputValue();
        await page.goto(address);
        await page.evaluate(() => {
          const generate = crypto.subtle.generateKey;
          crypto.subtle.generateKey = async function (...args: Parameters<SubtleCrypto['generateKey']>) {
            const key = await generate.apply(this, args);
            if (typeof args[0] === 'object' && args[0].name === 'ECDH') {
              crypto.subtle.generateKey = generate;
              await new Promise<void>(resolve => {
                window.addEventListener('release-invitation-preparation', () => resolve(), { once: true });
                document.documentElement.dataset.invitationPrepared = 'held';
              });
            }
            return key;
          } as SubtleCrypto['generateKey'];
        });
        await page.getByRole('button', { name: '申请配对' }).click();
        await expect(page.locator('html')).toHaveAttribute('data-invitation-prepared', 'held');
        await other.goto(address);
        await other.getByRole('button', { name: '申请配对' }).click();
        const winningCode = other.getByLabel('手机配对码');
        await expect(winningCode).toHaveText(/^[0-9A-F]{8}$/);
        await expect(desktop.getByLabel('电脑配对码')).toHaveText(await winningCode.innerText());
        await desktop.getByRole('button', { name: '确认配对码并批准' }).click();
        await expect(other.getByText('电脑已连接', { exact: true })).toBeVisible({ timeout: 15000 });
        const savedPair = () => other.evaluate(() => new Promise<{ room: string; approved: boolean }>((resolve, reject) => {
          const request = indexedDB.open('zhixue-mobile-study');
          request.onerror = () => reject(request.error);
          request.onsuccess = () => {
            const db = request.result;
            const tx = db.transaction('device');
            const pair = tx.objectStore('device').get('pair');
            tx.oncomplete = () => { db.close(); resolve({ room: pair.result.room, approved: !pair.result.invitation }); };
            tx.onabort = () => { db.close(); reject(tx.error); };
          };
        }));
        const winner = await savedPair();
        expect(winner.approved).toBe(true);

        await page.evaluate(() => window.dispatchEvent(new Event('release-invitation-preparation')));
        await expect(page.getByRole('button', { name: '申请配对' }).or(page.getByLabel('手机配对码'))).toBeEnabled();
        expect(await savedPair(), 'late invitation preparation must preserve the newly approved pairing').toEqual(winner);

        await other.evaluate(() => {
          let active = true;
          const subscription = {
            endpoint: 'https://fcm.googleapis.com/fcm/send/zhixue-old-invitation-test',
            unsubscribe: async () => { active = false; return true; },
          } as PushSubscription;
          PushManager.prototype.getSubscription = async () => active ? subscription : null;
        });
        await showPairing();
        await desktop.getByRole('button', { name: '撤销手机配对' }).click();
        await desktop.getByLabel('手机网页地址').fill(hosted.url.origin);
        await desktop.getByLabel('中继地址').fill(relayOrigin);
        await desktop.getByRole('button', { name: '生成配对链接' }).click();
        await other.goto(await invite.inputValue());
        await other.getByRole('button', { name: '申请配对' }).click();
        await expect(winningCode).toHaveText(/^[0-9A-F]{8}$/);
        const subscription = () => other.evaluate(async () =>
          (await (await navigator.serviceWorker.ready).pushManager.getSubscription())?.endpoint ?? null);
        await expect.poll(subscription, { message: 'replacing a pair must clear its original browser subscription' }).toBeNull();
        await expect(desktop.getByLabel('电脑配对码')).toHaveText(await winningCode.innerText());
        await desktop.getByRole('button', { name: '确认配对码并批准' }).click();
        await expect(other.getByText('电脑已连接', { exact: true })).toBeVisible({ timeout: 15000 });
        expect((await savedPair()).room).not.toBe(winner.room);
        writeFileSync(info.outputPath('迟到邀请与旧订阅隔离.json'), JSON.stringify({
          concurrentInvitationPreservedNewPair: true, replacingClearedOldBrowserSubscription: true,
          replacementApproved: true,
        }, null, 2));
        console.log('PASS late invitation preparation preserved the newly approved pair and a replacement cleared its old subscription');
      } finally {
        await other.close();
      }
    });
  } finally {
    if (computer) await desktopBrowser.request.delete(new URL('/api/device-sync', computer.url).href, {
      headers: secretHeaders(), timeout: 3500,
    }).catch(() => {});
    await desktopBrowser.close();
    for (const instance of services.reverse()) await instance.stop();
    rmSync(folder, { force: true, recursive: true });
  }
});
