const origin = self.location.origin;
const staticFiles = new Set(PRECACHE);
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const pairingTag = /^[0-9a-f]{64}$/;

function validNotice(data) {
  return data && data.v === 1 &&
    typeof data.delivery === 'string' && uuid.test(data.delivery) &&
    typeof data.workspace === 'string' && uuid.test(data.workspace) &&
    (data.pairing === undefined || typeof data.pairing === 'string' && pairingTag.test(data.pairing)) &&
    (data.lessonId === null || typeof data.lessonId === 'string' && uuid.test(data.lessonId));
}

function phonePair() {
  return new Promise((resolve, reject) => {
    const open = indexedDB.open('zhixue-mobile-study');
    let missing = false;
    open.onupgradeneeded = () => {
      missing = true;
      open.transaction.abort();
    };
    open.onerror = () => missing ? resolve(undefined) : reject(open.error);
    open.onsuccess = () => {
      const db = open.result;
      if (!db.objectStoreNames.contains('device')) {
        db.close();
        resolve(undefined);
        return;
      }
      const tx = db.transaction('device');
      const pair = tx.objectStore('device').get('pair');
      tx.oncomplete = () => { db.close(); resolve(pair.result); };
      tx.onabort = () => { db.close(); reject(tx.error); };
    };
  });
}

async function matchesPairing(notice) {
  const pair = await phonePair();
  if (!notice.pairing) return !pair;
  if (!pair || pair.invitation || typeof pair.room !== 'string') return false;
  const digest = await self.crypto.subtle.digest('SHA-256', new TextEncoder().encode(pair.room));
  const tag = Array.from(new Uint8Array(digest), value => value.toString(16).padStart(2, '0')).join('');
  return tag === notice.pairing;
}

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(
    PRECACHE.map(path => new Request(path, {
      cache: 'reload', mode: 'same-origin', credentials: 'same-origin', redirect: 'error', integrity: INTEGRITY[path],
    }))
  )));
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const retired = (await caches.keys()).filter(name => name.startsWith('zhixue-mobile-') && name !== CACHE);
    await Promise.all(retired.map(name => caches.delete(name)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== origin) return;
  const mobilePage = request.mode === 'navigate' && url.pathname === '/mobile/';
  const path = mobilePage ? '/mobile/' : url.pathname;
  if (!staticFiles.has(path)) return;
  event.respondWith(caches.open(CACHE).then(async cache => {
    const stored = await cache.match(path);
    return stored ?? fetch(request);
  }));
});

self.addEventListener('push', event => {
  let notice;
  try { notice = event.data?.json(); } catch { return; }
  if (!validNotice(notice)) return;
  event.waitUntil(self.registration.showNotification('智学', {
    body: notice.lessonId ? '到了预约课程的学习时间。' : '有复习待办。',
    icon: '/mobile/icon-192.png',
    tag: 'zhixue-' + notice.delivery,
    renotify: false,
    data: notice,
  }));
});

self.addEventListener('notificationclick', event => {
  const notice = event.notification.data;
  event.notification.close();
  if (!validNotice(notice)) return;
  event.waitUntil((async () => {
    if (!await matchesPairing(notice).catch(() => false)) return;
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    const client = windows.find(window => {
      const url = new URL(window.url);
      return url.origin === origin && url.pathname === '/mobile/';
    });
    const route = new URL(client?.url ?? '/mobile/', origin);
    const params = new URLSearchParams({ notice: notice.delivery, workspace: notice.workspace });
    if (notice.lessonId) params.set('lesson', notice.lessonId);
    if (notice.pairing) params.set('pairing', notice.pairing);
    route.hash = '/?' + params;
    if (client) {
      // Persist the notice in the existing page even when its message listener is unavailable.
      try {
        const navigated = await client.navigate(route.href);
        if (navigated) {
          await navigated.focus().catch(() => {});
          return;
        }
      } catch { /* The selected tab may have closed during navigation. */ }
    }
    await self.clients.openWindow(route.href);
  })());
});
