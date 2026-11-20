import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import type { AddressInfo } from 'node:net';
import { devices, expect, test, type Page } from '@playwright/test';
import { preview } from 'vite';
import type { LessonSnapshot } from '../src/mobile-data';

test.use({ ...devices['Pixel 7'], channel: 'chrome' });

function waitForUpdate(page: Page) {
  return page.evaluate(async () => {
    const registration = await navigator.serviceWorker.getRegistration('/mobile/');
    if (!registration?.active) throw new Error('The previous release is not active.');
    const state = new Promise<string>(resolve => {
      registration.addEventListener('updatefound', () => {
        const next = registration.installing;
        if (!next) throw new Error('The update did not create an installing worker.');
        const read = () => {
          if (next.state === 'installed' || next.state === 'redundant') resolve(next.state);
        };
        next.addEventListener('statechange', read);
        read();
      }, { once: true });
    });
    await registration.update();
    return state;
  });
}

test('mixed mobile releases leave the working offline version and its draft in place', async ({ page, context }) => {
  const [worker, entry] = await Promise.all([
    readFile('dist/mobile/sw.js', 'utf8'), readFile('dist/mobile/index.html', 'utf8'),
  ]);
  const cache = worker.match(/^const CACHE = "([^"]+)";/m)![1];
  const modulePath = entry.match(/<script type="module"[^>]* src="([^"]+)"/)![1];
  const moduleBytes = await readFile('dist' + modulePath);
  const previousCache = 'zhixue-mobile-previous-release';
  const previousModule = '/assets/previous-mobile-release.js';
  const previousPage = entry.replace(modulePath, previousModule)
    .replace('</head>', '  <meta name="release-fixture" content="previous">\n  </head>');
  const digest = (bytes: string | Buffer) => 'sha256-' + createHash('sha256').update(bytes).digest('base64');
  const previousWorker = worker.replace(cache, previousCache)
    .replace(JSON.stringify(modulePath), JSON.stringify(previousModule))
    .replace(/^const INTEGRITY = (.+);$/m, (_line, body: string) => {
      const hashes = JSON.parse(body) as Record<string, string>;
      delete hashes[modulePath];
      hashes[previousModule] = digest(moduleBytes);
      hashes['/mobile/'] = digest(previousPage);
      return 'const INTEGRITY = ' + JSON.stringify(hashes) + ';';
    });
  let phase: 'previous' | 'mixed' | 'current' = 'previous';
  const server = await preview({
    preview: { host: '127.0.0.1', port: 30000, strictPort: false, open: false },
    plugins: [{
      name: 'mobile-release-sequence',
      configurePreviewServer(app) {
        app.middlewares.use((request, response, next) => {
          if (request.url === '/mobile/sw.js') {
            response.setHeader('Content-Type', 'application/javascript');
            response.setHeader('Cache-Control', 'no-cache');
            return response.end(phase === 'previous' ? previousWorker : worker);
          }
          if (request.url === '/mobile/' && phase !== 'current') {
            response.setHeader('Content-Type', 'text/html; charset=utf-8');
            response.setHeader('Cache-Control', 'no-cache');
            return response.end(previousPage);
          }
          if (request.url === previousModule) {
            response.setHeader('Content-Type', 'application/javascript');
            response.statusCode = phase === 'previous' ? 200 : 404;
            return response.end(phase === 'previous' ? moduleBytes : undefined);
          }
          next();
        });
      },
    }],
  });
  try {
    const origin = 'http://127.0.0.1:' + (server.httpServer.address() as AddressInfo).port;
    const url = origin + '/mobile/';
    await page.goto(url);
    await expect(page.getByText('此设备还没有下载内容。')).toBeVisible();
    await expect.poll(() => page.evaluate(() => !!navigator.serviceWorker.controller)).toBe(true);
    const lesson: LessonSnapshot = {
      id: '30000000-0000-4000-8000-000000000001', workspace: '30000000-0000-4000-8000-000000000002',
      title: '离线换版课程', projectTopic: 'SQLite', body: '# 保留的课程', revision: 'fixture',
      note: { id: '30000000-0000-4000-8000-000000000003', title: '本节笔记', revision: 'fixture', content: '下载的原笔记' },
    };
    await page.evaluate(snapshot => new Promise<void>((resolve, reject) => {
      const open = indexedDB.open('zhixue-mobile-study');
      open.onerror = () => reject(open.error);
      open.onsuccess = () => {
        const db = open.result;
        const tx = db.transaction('items', 'readwrite');
        tx.oncomplete = () => { db.close(); resolve(); };
        tx.onabort = () => { db.close(); reject(tx.error); };
        tx.objectStore('items').put({
          kind: 'lesson', key: [snapshot.workspace, 'lesson', snapshot.id].join(':'), snapshot, note: snapshot.note,
        });
      };
    }), lesson);
    await page.reload();
    await page.getByRole('button', { name: '打开课程 ' + lesson.title }).click();
    const draft = '换版前保存的离线笔记';
    await page.getByRole('textbox', { name: '本节笔记' }).fill(draft);
    await page.getByRole('button', { name: '保存到本机' }).click();
    await expect(page.getByText('笔记已保存到本机', { exact: true })).toBeVisible();

    phase = 'mixed';
    expect(await waitForUpdate(page)).toBe('redundant');
    expect(await page.evaluate(async ([oldCache, newCache]) => {
      const old = await caches.match('/mobile/', { cacheName: oldCache });
      return { previous: !!old, mixed: !!(await caches.match('/mobile/', { cacheName: newCache })) };
    }, [previousCache, cache])).toEqual({ previous: true, mixed: false });
    await context.setOffline(true);
    const offline = await context.newPage();
    await offline.goto(url);
    await expect(offline.locator('meta[name="release-fixture"]')).toHaveAttribute('content', 'previous');
    await offline.getByRole('button', { name: '打开课程 ' + lesson.title }).click();
    await expect(offline.getByRole('textbox', { name: '本节笔记' })).toHaveValue(draft);

    await context.setOffline(false);
    phase = 'current';
    expect(await waitForUpdate(page)).toBe('installed');
    const observer = await context.newPage();
    await observer.goto(origin + '/zhixue.png');
    await page.close();
    await offline.close();
    await expect.poll(() => observer.evaluate(() => caches.keys())).toEqual([cache]);
    await context.setOffline(true);
    const updated = await context.newPage();
    await updated.goto(url);
    await expect(updated.locator('meta[name="release-fixture"]')).toHaveCount(0);
    await updated.getByRole('button', { name: '同步', exact: true }).click();
    await expect(updated.getByText('1 项待同步', { exact: true })).toBeVisible();
    await updated.getByRole('button', { name: '课程', exact: true }).click();
    await updated.getByRole('button', { name: '打开课程 ' + lesson.title }).click();
    await expect(updated.getByRole('textbox', { name: '本节笔记' })).toHaveValue(draft);
  } finally {
    await server.close();
  }
});
