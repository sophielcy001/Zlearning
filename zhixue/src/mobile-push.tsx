import { useEffect, useState } from 'react';
import { mobileRequest } from './mobile-data';
import { PairingChanged, pendingPushRemoval, phonePair, savePushRemoval } from './mobile-store';
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
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(pair.room));
  const tag = Array.from(new Uint8Array(digest), value => value.toString(16).padStart(2, '0')).join('');
  return tag === notice.pairing;
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

async function localSubscription(subscription: PushSubscription | null) {
  if (!subscription) return undefined;
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(subscription.endpoint));
  const id = Array.from(new Uint8Array(digest), value => value.toString(16).padStart(2, '0')).join('');
  return { subscription, id };
}

export async function browserPushSubscription() {
  if (!supported) return;
  const registration = await navigator.serviceWorker.getRegistration('/mobile/');
  return registration?.pushManager.getSubscription();
}

const removals = new Map<string, Promise<PushStatus | undefined>>();

export function synchronizePushRemoval(room: string, workspace?: string) {
  const active = removals.get(room);
  if (active) return active;
  const sync = (async () => {
    const endpointId = await pendingPushRemoval(room);
    if (!endpointId) return;
    const pair = await phonePair();
    if (!pair || pair.room !== room || pair.invitation) return;
    const registration = supported ? await navigator.serviceWorker.getRegistration('/mobile/') : undefined;
    const manager = registration?.pushManager;
    const local = manager ? await localSubscription(await manager.getSubscription()) : undefined;
    if (manager && local?.id === endpointId && !await local.subscription.unsubscribe()) {
      const remaining = await localSubscription(await manager.getSubscription());
      if (remaining?.id === endpointId) throw new Error('浏览器暂未完成停用手机推送。');
    }
    if (!workspace) return;
    const status = await mobileRequest<PushStatus>({ op: 'push-unsubscribe', workspace, endpointId }, room);
    if (status.endpointId === endpointId) throw new Error('电脑暂未完成撤销手机推送。');
    await savePushRemoval(room, endpointId, true);
    return status;
  })().finally(() => removals.delete(room));
  removals.set(room, sync);
  return sync;
}

export function PhonePushSettings({ room, workspace, syncVersion }: { room: string; workspace?: string; syncVersion: number }) {
  const [expanded, setExpanded] = useState(false);
  const [worker, setWorker] = useState<ServiceWorkerRegistration>();
  const [local, setLocal] = useState<Awaited<ReturnType<typeof localSubscription>>>();
  const [remote, setRemote] = useState<PushStatus>();
  const [pending, setPending] = useState<string>();
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
      const pending = await pendingPushRemoval(room);
      if (live) { setWorker(registration); setLocal(subscription); setPending(pending); }
      if (!workspace) return;
      const status = (pending ? await synchronizePushRemoval(room, workspace) : undefined) ??
        await mobileRequest<PushStatus>({ op: 'push-status', workspace }, room);
      const remaining = await pendingPushRemoval(room);
      const current = pending ? await localSubscription(await registration.pushManager.getSubscription()) : subscription;
      if (live) { setRemote(status); setPending(remaining); setLocal(current); }
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
        setRemote(await mobileRequest<PushStatus>({
          op: 'push-subscribe', workspace, subscription: subscription.toJSON(),
        }, room));
      } catch (error) {
        // The permission prompt can outlive the pairing that supplied the application key.
        if (error instanceof PairingChanged && !existing) await subscription.unsubscribe();
        throw error;
      }
    });
  }

  function disable() {
    const endpointId = local?.id ?? remote?.endpointId;
    if (!endpointId) return;
    void action.run(async () => {
      if (!await savePushRemoval(room, endpointId)) throw new Error('此手机的配对已更换。');
      setPending(endpointId);
      const status = await synchronizePushRemoval(room, workspace);
      if (status) setRemote(status);
      setLocal(worker ? await localSubscription(await worker.pushManager.getSubscription()) : undefined);
      setPending(await pendingPushRemoval(room));
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
        {!pending && (local || remote?.registered) && <button className="action secondary" type="button"
          disabled={action.pending} onClick={disable}>{oldSubscription ? '移除旧订阅' : '停用手机推送'}</button>}
      </div>
    </>}
    <ErrorNote message={action.error || statusError} retry={statusError ? () => setRevision(value => value + 1) : undefined} />
  </details>;
}
