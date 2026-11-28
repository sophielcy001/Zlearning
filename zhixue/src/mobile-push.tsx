import { useEffect, useState } from 'react';
import { mobileRequest } from './mobile-data';
import { acknowledgePushRemoval, PairingChanged, phonePair, phonePushState, queuePushRemoval,
  rememberPushRegistration, type PhonePushState } from './mobile-store';
import type { PushNotice, PushStatus } from './reminder-data';
import { ErrorNote, useAction } from './shared';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const PAIRING_TAG = /^[0-9a-f]{64}$/;
const supported = 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;

function isIdentity(value: unknown): value is string {
  return typeof value === 'string' && UUID.test(value);
}

export function readPhoneNotice(value: unknown): PushNotice | undefined {
  if (!value || typeof value !== 'object') return undefined;
  const item = value as Partial<PushNotice>;
  if (item.v === 1 && isIdentity(item.delivery) && isIdentity(item.workspace) &&
      (item.pairing === undefined || typeof item.pairing === 'string' && PAIRING_TAG.test(item.pairing)) &&
      (item.lessonId === null || isIdentity(item.lessonId))) {
    return { v: 1, delivery: item.delivery, workspace: item.workspace,
      lessonId: item.lessonId, pairing: item.pairing };
  }
}

export async function phoneNoticeMatchesPair(notice: PushNotice): Promise<boolean> {
  const pair = await phonePair();
  // Untagged links can still open local content on an unpaired device.
  if (!notice.pairing) return !pair;
  if (!pair || pair.invitation) return false;
  return await fingerprint(pair.room) === notice.pairing;
}

function noticeRoute() {
  const url = new URL(location.href);
  const query = url.hash.indexOf('?');
  const args = new URLSearchParams(query < 0 ? '' : url.hash.slice(query + 1));
  const path = (query < 0 ? url.hash : url.hash.slice(0, query)) || '#/';
  return { args, save: () => {
    url.hash = path + (args.size ? '?' + args : '');
    history.replaceState(history.state, '', url);
  } };
}

export function phoneNoticeFromUrl() {
  const { args } = noticeRoute();
  return readPhoneNotice({
    v: 1, delivery: args.get('notice'), workspace: args.get('workspace'), lessonId: args.get('lesson'),
    pairing: args.get('pairing') ?? undefined,
  });
}

export function rememberPhoneNotice(notice: PushNotice) {
  const { args, save } = noticeRoute();
  args.set('notice', notice.delivery);
  args.set('workspace', notice.workspace);
  if (notice.lessonId) args.set('lesson', notice.lessonId);
  else args.delete('lesson');
  if (notice.pairing) args.set('pairing', notice.pairing);
  else args.delete('pairing');
  save();
}

export function clearPhoneNotice(delivery: string) {
  const { args, save } = noticeRoute();
  if (args.get('notice') !== delivery) return;
  for (const key of ['notice', 'workspace', 'lesson', 'pairing']) args.delete(key);
  save();
}

function applicationKey(encoded: string) {
  return Uint8Array.from(atob(encoded.replace(/-/g, '+').replace(/_/g, '/')), character => character.charCodeAt(0));
}

async function fingerprint(value: string) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
}
async function localSubscription(subscription: PushSubscription | null) {
  if (!subscription) return undefined;
  return { subscription, id: await fingerprint(subscription.endpoint) };
}

export async function browserPushSubscription() {
  if (!supported) return;
  const registration = await navigator.serviceWorker.getRegistration('/mobile/');
  return registration?.pushManager.getSubscription();
}

export async function dismissPhoneReminders(room: string, registration?: ServiceWorkerRegistration) {
  if (!supported) return;
  try {
    const worker = registration ?? await navigator.serviceWorker.getRegistration('/mobile/');
    if (!worker) return;
    const pairing = await fingerprint(room);
    for (const notification of await worker.getNotifications()) {
      const notice = readPhoneNotice(notification.data);
      if (notice?.pairing === pairing && notification.tag === 'zhixue-' + notice.delivery) notification.close();
    }
  } catch {
    // A browser's notification center may be unavailable while a subscription is still removable.
  }
}

const removals = new Map<string, { workspace?: string; sync: Promise<PushStatus | undefined> }>();

export function synchronizePushRemoval(room: string, workspace?: string): Promise<PushStatus | undefined> {
  const active = removals.get(room);
  if (active) {
    // A reconnect must finish the desktop removal after a local-only operation.
    if (workspace && !active.workspace) return active.sync.then(() => synchronizePushRemoval(room, workspace));
    return active.sync;
  }
  const sync = (async () => {
    const { pending } = await phonePushState(room);
    if (!pending.length) return;
    const pair = await phonePair();
    if (!pair || pair.room !== room || pair.invitation) return;
    const registration = supported ? await navigator.serviceWorker.getRegistration('/mobile/') : undefined;
    const manager = registration?.pushManager;
    const local = manager ? await localSubscription(await manager.getSubscription()) : undefined;
    if (registration && (!local || pending.includes(local.id))) await dismissPhoneReminders(room, registration);
    if (manager && local && pending.includes(local.id) && !await local.subscription.unsubscribe()) {
      const remaining = await localSubscription(await manager.getSubscription());
      if (remaining?.id === local.id) throw new Error('浏览器暂未完成停用手机推送。');
    }
    if (!workspace) return;
    let status: PushStatus | undefined;
    for (const endpointId of pending) {
      status = await mobileRequest<PushStatus>({ op: 'push-unsubscribe', workspace, endpointId }, room);
      if (status.endpointId === endpointId) throw new Error('电脑暂未完成撤销手机推送。');
      await acknowledgePushRemoval(room, endpointId, status.endpointId);
    }
    return status;
  })().finally(() => removals.delete(room));
  removals.set(room, { workspace, sync });
  return sync;
}

export function PhonePushSettings({ room, workspace, syncVersion }: { room: string; workspace?: string; syncVersion: number }) {
  const [expanded, setExpanded] = useState(false);
  const [worker, setWorker] = useState<ServiceWorkerRegistration>();
  const [local, setLocal] = useState<Awaited<ReturnType<typeof localSubscription>>>();
  const [remote, setRemote] = useState<PushStatus>();
  const [saved, setSaved] = useState<PhonePushState>({ endpointId: null, pending: [] });
  const pending = saved.pending.length > 0;
  const [statusError, setStatusError] = useState('');
  const [revision, setRevision] = useState(0);
  const action = useAction();

  useEffect(() => {
    if (!expanded || !supported) return;
    let live = true;
    setRemote(undefined);
    setStatusError('');
    void (async () => {
      const registration = await navigator.serviceWorker.ready;
      const subscription = await localSubscription(await registration.pushManager.getSubscription());
      const saved = await phonePushState(room);
      if (live) { setWorker(registration); setLocal(subscription); setSaved(saved); }
      if (!workspace) return;
      const synchronized = saved.pending.length ? await synchronizePushRemoval(room, workspace) : undefined;
      const status = synchronized ?? await mobileRequest<PushStatus>({ op: 'push-status', workspace }, room);
      if (!live) return;
      if (!synchronized) await rememberPushRegistration(room, status.endpointId);
      const current = await localSubscription(await registration.pushManager.getSubscription());
      const remaining = await phonePushState(room);
      if (live) { setRemote(status); setSaved(remaining); setLocal(current); }
    })().catch(error => {
      if (live) setStatusError(error instanceof Error ? error.message : '暂时无法读取手机推送设置。');
    });
    return () => { live = false; };
  }, [expanded, room, workspace, revision, syncVersion]);

  const key = remote ? applicationKey(remote.publicKey) : undefined;
  const storedKey = local?.subscription.options.applicationServerKey;
  const oldSubscription = local && key && (!storedKey || storedKey.byteLength !== key.byteLength ||
    !new Uint8Array(storedKey).every((value, index) => value === key[index]));
  const allowed = supported && Notification.permission === 'granted';
  const active = !pending && allowed && remote?.registered && local && local.id === remote.endpointId && !oldSubscription;
  const denied = supported && Notification.permission === 'denied';

  function enable() {
    if (!worker || !workspace || !key || denied || oldSubscription || pending) return;
    void action.run(async () => {
      const existing = allowed ? local?.subscription : undefined;
      const subscription = existing ??
        await worker.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
      setLocal(await localSubscription(subscription));
      try {
        const status = await mobileRequest<PushStatus>({
          op: 'push-subscribe', workspace, subscription: subscription.toJSON(),
        }, room);
        if (!await rememberPushRegistration(room, status.endpointId)) throw new PairingChanged();
        setRemote(status);
        setSaved(await phonePushState(room));
      } catch (error) {
        // The permission prompt can outlive the pairing that supplied the application key.
        if (error instanceof PairingChanged && !existing) await subscription.unsubscribe();
        throw error;
      }
    });
  }

  function disable() {
    void action.run(async () => {
      if (!await queuePushRemoval(room, local?.id ?? null)) throw new PairingChanged();
      setSaved(await phonePushState(room));
      const status = await synchronizePushRemoval(room, workspace);
      if (status) setRemote(status);
      setLocal(worker ? await localSubscription(await worker.pushManager.getSubscription()) : undefined);
      setSaved(await phonePushState(room));
    });
  }

  return <details className="study-card" onToggle={event => setExpanded(event.currentTarget.open)}>
    <summary>手机系统推送</summary>
    {!supported ? <p>在手机主屏幕打开智学，或使用支持系统推送的浏览器。</p> : <>
      <p className="subtle">电脑运行时，手机关页也可收到固定课程和「30 分钟后」的复习提醒。</p>
      {denied && <p role="status">请在系统或浏览器中允许智学通知。</p>}
      {active && <p role="status">已启用手机系统推送。</p>}
      {pending && <p role="status">等待同步停用手机推送。</p>}
      {!workspace && <p role="status">连接电脑后可以管理推送登记。</p>}
      <div className="actions">
        {!active && !oldSubscription && <button className="action" type="button"
          disabled={action.pending || denied || !!pending || !workspace || !remote || !worker}
          onClick={enable}>{local ? '重新连接手机推送' : '启用手机推送'}</button>}
        {!pending && (local || remote?.registered || saved.endpointId) && <button className="action secondary" type="button"
          disabled={action.pending} onClick={disable}>{oldSubscription ? '移除旧订阅' : '停用手机推送'}</button>}
      </div>
    </>}
    <ErrorNote message={action.error || statusError} retry={statusError ? () => setRevision(value => value + 1) : undefined} />
  </details>;
}
