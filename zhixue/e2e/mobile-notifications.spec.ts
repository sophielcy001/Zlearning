import { execFile } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFile, readdir, writeFile } from 'node:fs/promises';
import type { AddressInfo } from 'node:net';
import { join, resolve } from 'node:path';
import { promisify } from 'node:util';
import { devices, expect, test, type Page, type TestInfo } from '@playwright/test';
import { preview } from 'vite';
import type { LessonSnapshot } from '../src/mobile-data';

type Notice = { v: number; delivery: string; workspace: string; lessonId: string | null; pairing?: string };
const pairingTag = (room: string) => createHash('sha256').update(room).digest('hex');
type PushReplay = { payloads: Notice[]; topics: string[]; lesson: LessonSnapshot };

async function desktopReplay(info: TestInfo): Promise<PushReplay> {
  const python = resolve(process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
  const scratch = info.outputPath('push-backend');
  await promisify(execFile)(python, ['-m', 'pytest', 'tests/test_mobile_push.py', '-q', '--basetemp', scratch,
    '-k', 'replays_the_same_encrypted_delivery'], {
    encoding: 'utf8', timeout: 30000, windowsHide: true,
    env: { ...process.env, PYTHONUTF8: '1' },
  });
  const directory = (await readdir(scratch, { withFileTypes: true }))
    .find(item => item.isDirectory() && item.name.startsWith('test_web_push_retry_'));
  if (!directory) throw new Error('The desktop push test did not produce a delivery record.');
  const raw = await readFile(join(scratch, directory.name, 'push-replay.json'), 'utf8');
  await writeFile(info.outputPath('桌面加密推送回执.json'), raw);
  return JSON.parse(raw) as PushReplay;
}

test.use({ ...devices['Pixel 7'], channel: 'chrome' });

// The CDP command queues a push event. Observe each completed browser notification separately.
const workerProbe = `
const qaNotices = new BroadcastChannel('zhixue-test-notifications');
const showNotice = ServiceWorkerRegistration.prototype.showNotification;
ServiceWorkerRegistration.prototype.showNotification = async function (title, options) {
  await showNotice.call(this, title, options);
  qaNotices.postMessage(options.tag);
};
self.addEventListener('message', event => {
  if (event.data?.type !== 'test-notification-click') return;
  const [reply] = event.ports;
  event.waitUntil((async () => {
    const [notification] = await self.registration.getNotifications({ tag: event.data.tag });
    if (!notification) throw new Error('Notification unavailable for click');
    const click = new Event('notificationclick');
    const work = [];
    click.notification = notification;
    click.waitUntil = promise => work.push(promise);
    // Synthetic clicks lack window interaction permission, but can navigate an existing client.
    const focus = WindowClient.prototype.focus;
    const openWindow = Clients.prototype.openWindow;
    let opened;
    WindowClient.prototype.focus = async function () { return this; };
    Clients.prototype.openWindow = async function (url) { opened = url; return null; };
    try {
      self.dispatchEvent(click);
      await Promise.all(work);
      return opened;
    } finally {
      WindowClient.prototype.focus = focus;
      Clients.prototype.openWindow = openWindow;
    }
  })().then(opened => reply.postMessage({ opened }), error => reply.postMessage({ error: String(error) })));
});
`;

async function saveFixturePair(page: Page, room: string) {
  await page.evaluate(async room => {
    const key = await crypto.subtle.importKey('raw', new Uint8Array(32), 'HKDF', false, ['deriveKey']);
    await new Promise<void>((resolve, reject) => {
      const open = indexedDB.open('zhixue-mobile-study', 3);
      open.onerror = () => reject(open.error);
      open.onsuccess = () => {
        const db = open.result;
        const tx = db.transaction('device', 'readwrite');
        tx.objectStore('device').put({ room, relay: location.origin, key }, 'pair');
        tx.oncomplete = () => { db.close(); resolve(); };
        tx.onabort = () => { db.close(); reject(tx.error); };
      };
    });
  }, room);
}

function clickNotice(observer: Page, tag: string): Promise<string | undefined> {
  return observer.evaluate(async tag => {
    const registration = await navigator.serviceWorker.getRegistration('/mobile/');
    const worker = registration?.active;
    if (!worker) throw new Error('Test worker was not active');
    return new Promise<string | undefined>((done, fail) => {
      const channel = new MessageChannel();
      channel.port1.onmessage = ({ data }) => {
        channel.port1.close();
        if (data.error) fail(new Error(data.error));
        else done(data.opened);
      };
      worker.postMessage({ type: 'test-notification-click', tag }, [channel.port2]);
    });
  }, tag);
}

for (const kind of ['review', 'appointment', 'replacement'] as const) {
  const name = kind === 'review'
    ? 'a closed mobile page receives one visible notification and opens the review list'
    : kind === 'appointment'
      ? 'a closed mobile page displays one appointment for two desktop sends after a lost acknowledgement'
      : 'an old appointment cannot navigate a replacement pairing in the same workspace';
  test(name, async ({ page, context }, info) => {
    const fixture = kind === 'appointment' ? await desktopReplay(info) : undefined;
    const worker = await readFile(resolve('dist/mobile/sw.js'), 'utf8');
    const server = await preview({
      preview: { host: '127.0.0.1', port: 30000, strictPort: false, open: false },
      plugins: [{
        name: 'observe-mobile-notifications',
        configurePreviewServer(previewServer) {
          previewServer.middlewares.use('/mobile/sw.js', (_request, response) => {
            response.setHeader('Content-Type', 'application/javascript; charset=utf-8');
            response.end(workerProbe + worker);
          });
        },
      }],
    });
    try {
      const address = server.httpServer.address() as AddressInfo;
      const origin = 'http://127.0.0.1:' + address.port;
      const scope = origin + '/mobile/';
      await context.grantPermissions(['notifications'], { origin });
      await page.goto(scope);
      await expect.poll(() => page.evaluate(() => navigator.serviceWorker.controller?.state)).toBe('activated');

      const observer = await context.newPage();
      await observer.goto(origin + '/zhixue.png');
      await observer.evaluate(() => {
        const messages = new BroadcastChannel('zhixue-test-notifications');
        const received: string[] = [];
        messages.onmessage = event => {
          received.push(String(event.data));
          document.documentElement.dataset.notifications = JSON.stringify(received);
        };
      });
      const cdp = await context.newCDPSession(observer);
      let registrationId: string | undefined;
      cdp.on('ServiceWorker.workerRegistrationUpdated', ({ registrations }) => {
        const registration = registrations.find(item => item.scopeURL === scope && !item.isDeleted);
        if (registration) registrationId = registration.registrationId;
      });
      await cdp.send('ServiceWorker.enable');
      await expect.poll(() => registrationId).toBeTruthy();
      if (kind === 'replacement') await saveFixturePair(page, 'original-notice-fixture-room');
      else if (fixture) await saveFixturePair(page, 'notice-fixture-pairing-room');
      await page.close();
      expect(context.pages()).toEqual([observer]);

      const fixtureReview: Notice = {
        v: 1, delivery: '30000000-0000-4000-8000-000000000001',
        workspace: '30000000-0000-4000-8000-000000000002', lessonId: null,
      };
      const candidate: Notice = kind === 'replacement' ? {
        ...fixtureReview, lessonId: '30000000-0000-4000-8000-000000000003',
        pairing: pairingTag('original-notice-fixture-room'),
      } : fixtureReview;
      const [first, replay] = fixture?.payloads ?? [candidate, candidate];
      const deliver = (payload: Notice) => cdp.send('ServiceWorker.deliverPushMessage', {
        origin, registrationId: registrationId!, data: JSON.stringify(payload),
      });
      const notices = () => observer.evaluate(async () => {
        const registration = await navigator.serviceWorker.getRegistration('/mobile/');
        const list = await registration?.getNotifications() ?? [];
        return list.map(({ title, body, data, tag }) => ({ title, body, data, tag }));
      });
      const expected = [{
        title: '智学', body: first.lessonId ? '到了预约课程的学习时间。' : '有复习待办。',
        data: first, tag: 'zhixue-' + first.delivery,
      }];
      const firstTag = expected[0].tag;
      const expectedTags = [firstTag, firstTag];
      const observedTags = () => observer.evaluate(() => JSON.parse(document.documentElement.dataset.notifications ?? '[]'));
      // Query Chrome's displayed notifications only after showNotification has resolved.
      await deliver(first);
      await expect.poll(observedTags).toEqual([firstTag]);
      await expect.poll(notices).toEqual(expected);
      await deliver(replay);
      await expect.poll(observedTags).toEqual(expectedTags);
      await expect.poll(notices).toEqual(expected);
      if (fixture) {
        await writeFile(info.outputPath('浏览器通知观察.json'), JSON.stringify({
          completedNotifications: await observedTags(), visibleNotifications: await notices(),
        }, null, 2));
      }

      const query = new URLSearchParams({ notice: first.delivery, workspace: first.workspace });
      if (first.lessonId) query.set('lesson', first.lessonId);
      if (first.pairing) query.set('pairing', first.pairing);
      const phone = await context.newPage();
      if (kind === 'replacement') {
        const currentRoom = 'replacement-notice-fixture-room';
        await context.setOffline(true);
        await observer.evaluate(async ({ room, notice }) => {
          const key = await crypto.subtle.importKey('raw', new Uint8Array(32), 'HKDF', false, ['deriveKey']);
          await new Promise<void>((resolve, reject) => {
            const open = indexedDB.open('zhixue-mobile-study', 3);
            open.onerror = () => reject(open.error);
            open.onsuccess = () => {
              const db = open.result;
              const tx = db.transaction(['device', 'items'], 'readwrite');
              tx.objectStore('device').put({ room, relay: location.origin, key }, 'pair');
              tx.objectStore('items').put({
                kind: 'lesson', key: [notice.workspace, 'lesson', notice.lessonId].join(':'),
                snapshot: { id: notice.lessonId, workspace: notice.workspace, title: '同一工作区预约',
                  revision: 'fixture', projectTopic: 'SQLite', body: '# 已下载预约内容', note: null },
                note: null,
              });
              tx.oncomplete = () => { db.close(); resolve(); };
              tx.onabort = () => { db.close(); reject(tx.error); };
            };
          });
        }, { room: currentRoom, notice: first });
        await phone.goto(scope);
        await expect(phone.getByRole('button', { name: '打开课程 同一工作区预约', exact: true })).toBeVisible();
        const beforeClick = phone.url();
        expect(await clickNotice(observer, firstTag)).toBeUndefined();
        await expect(phone, 'a notice from the old pairing must not navigate the current client').toHaveURL(beforeClick);
        await expect(phone.getByRole('button', { name: '打开课程 同一工作区预约', exact: true })).toBeVisible();
        await expect.poll(notices).toEqual([]);

        // A restored URL can bypass the worker, so the page must also verify the pairing.
        await phone.goto(scope + '#/?' + query);
        await expect(phone).toHaveURL(scope + '#/');
        await expect(phone.getByRole('button', { name: '打开课程 同一工作区预约', exact: true })).toBeVisible();

        query.delete('pairing');
        await phone.goto(scope + '#/?' + query);
        await expect(phone).toHaveURL(scope + '#/');
        await expect(phone.getByRole('button', { name: '打开课程 同一工作区预约', exact: true })).toBeVisible();

        const current = { ...first, delivery: '30000000-0000-4000-8000-000000000004', pairing: pairingTag(currentRoom) };
        const currentTag = 'zhixue-' + current.delivery;
        await deliver(current);
        await expect.poll(observedTags).toEqual([...expectedTags, currentTag]);
        expect(await clickNotice(observer, currentTag)).toBeUndefined();
        await expect(phone.getByRole('heading', { name: '同一工作区预约', exact: true, level: 2 })).toBeVisible();
        expect(new URL(phone.url()).hash).toBe('#/');
        await expect(phone.getByRole('region', { name: '本机计时' })).toHaveCount(0);
        await writeFile(info.outputPath('预约通知配对隔离.json'), JSON.stringify({
          oldClickIgnored: true, oldUrlIgnored: true, currentOfflineAppointmentOpened: true, autoTimer: false,
        }, null, 2));
      } else if (fixture) {
        await context.setOffline(true);
        await phone.addInitScript(() => {
          // Simulate a page whose notification listener is not ready when the click arrives.
          navigator.serviceWorker.addEventListener('message', event => {
            if (event.data?.type === 'zhixue-notice') event.stopImmediatePropagation();
          }, { capture: true });
        });
        await phone.goto(scope);
        await expect(phone.getByRole('heading', { name: '智学离线' })).toBeVisible();
        await phone.evaluate(() => { document.documentElement.dataset.existingClient = 'yes'; });
        expect(await clickNotice(observer, expectedTags[0])).toBeUndefined();
        const waiting = phone.getByText('连接电脑后可打开预约课程。', { exact: true });
        await expect(phone).toHaveURL(scope + '#/?' + query, { timeout: 5000 });
        await expect(phone.locator('html')).toHaveAttribute('data-existing-client', 'yes');
        await expect(waiting).toBeVisible();
        await phone.reload();
        await expect(waiting).toBeVisible();
        await expect(phone.getByRole('region', { name: '本机计时' })).toHaveCount(0);

        await observer.evaluate(lesson => new Promise<void>((done, fail) => {
          const open = indexedDB.open('zhixue-mobile-study', 3);
          open.onerror = () => fail(open.error);
          open.onsuccess = () => {
            const database = open.result;
            const transaction = database.transaction('items', 'readwrite');
            transaction.objectStore('items').put({
              kind: 'lesson', key: [lesson.workspace, 'lesson', lesson.id].join(':'),
              snapshot: lesson, note: lesson.note,
            });
            transaction.oncomplete = () => { database.close(); done(); };
            transaction.onabort = () => { database.close(); fail(transaction.error); };
          };
        }), fixture.lesson);
        await phone.reload();
        await expect(phone.getByRole('heading', { name: fixture.lesson.title, level: 2, exact: true })).toBeVisible();
        expect(new URL(phone.url()).hash).toBe('#/');
        await expect(phone.getByRole('button', { name: '开始学习计时', exact: true })).toBeVisible();
        await expect(phone.getByRole('region', { name: '本机计时' })).toHaveCount(0);
      } else {
        const opened = await clickNotice(observer, expectedTags[0]);
        expect(opened).toBe(scope + '#/?' + query);
        await phone.goto(opened!);
        await expect(phone.getByRole('button', { name: '复习', exact: true })).toHaveAttribute('aria-pressed', 'true');
        await expect(phone.getByRole('heading', { name: '已下载题组' })).toBeVisible();
      }
    } finally {
      await server.close();
    }
  });
}
