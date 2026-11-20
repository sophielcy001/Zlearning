import { hasDesktopSession } from './api';
import { changeDesktopRecord, tauriOfflineStore, type DesktopOfflineStore } from './desktop-offline-store';
import { studyKey, type Answer, type LessonSnapshot, type NoteChange, type NoteSnapshot, type Paper,
  type NoteReply, type ReviewChange, type ReviewReply } from './mobile-data';

type LocalNote = NoteSnapshot & {
  editedAt?: string;
  pending?: NoteChange;
  conflict?: string;
};
export type LessonRecord = { kind: 'lesson'; key: string; snapshot: LessonSnapshot; note: LocalNote | null };
export type ReviewRecord = {
  kind: 'review'; key: string; snapshot: Paper; draft: (Answer | null)[]; submission?: ReviewChange; reply?: ReviewReply;
};
export type OfflineRecord = LessonRecord | ReviewRecord;
export type RelayDevice = { relay: string; room: string; key: CryptoKey };
export type MobilePair = RelayDevice & {
  invitation?: RelayDevice & { name: string; point: string; code: string; expiresAt: number };
};

const DATABASE = 'zhixue-mobile-study';
const ITEMS = 'items';
const DEVICE = 'device';
const PAIR_VERSION = 'pair-version';
const downloadRemovalKey = (key: string) => 'download-removal:' + key;
const pushRemovalKey = (room: string) => 'push-removal:' + room;
export const MOBILE_TIMERS = 'timers';
const pairChanges = typeof BroadcastChannel === 'undefined' ? undefined : new BroadcastChannel('zhixue-mobile-pairing');

export function watchPhonePair(listener: () => void) {
  pairChanges?.addEventListener('message', listener);
  return () => pairChanges?.removeEventListener('message', listener);
}
let database: Promise<IDBDatabase> | undefined;

export function localDatabase(): Promise<IDBDatabase> {
  if (database) return database;
  database = new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open(DATABASE, 3);
    request.onupgradeneeded = () => {
      const stores = request.result.objectStoreNames;
      if (!stores.contains(ITEMS)) request.result.createObjectStore(ITEMS, { keyPath: 'key' });
      if (!stores.contains(DEVICE)) request.result.createObjectStore(DEVICE);
      if (!stores.contains(MOBILE_TIMERS)) request.result.createObjectStore(MOBILE_TIMERS);
    };
    request.onblocked = () => reject(new Error('请关闭此设备上的旧版智学页面后重试。'));
    request.onerror = () => reject(request.error);
    request.onsuccess = () => {
      const db = request.result;
      db.onversionchange = () => { db.close(); database = undefined; };
      resolve(db);
    };
  }).catch(error => { database = undefined; throw error; });
  return database;
}

export class PairingChanged extends Error {
  constructor() { super('本机配对已变更。'); }
}

export function offlineTransaction(db: IDBDatabase, name: string, mode: IDBTransactionMode,
  room: string | undefined, use: (store: IDBObjectStore) => void, pairingChanged: () => void,
  includeDevice = false) {
  // Reading the pair and applying its reply must share the same transaction as removal.
  const stores = room !== undefined || includeDevice ? [name, DEVICE] : name;
  const tx = db.transaction(stores, mode, { durability: 'strict' });
  if (room === undefined) use(tx.objectStore(name));
  else {
    const pairing = tx.objectStore(DEVICE).get('pair');
    pairing.onsuccess = () => {
      const current = pairing.result as MobilePair | undefined;
      if (current?.room === room && !current.invitation) use(tx.objectStore(name));
      else {
        pairingChanged();
        tx.abort();
      }
    };
  }
  return tx;
}

export async function phonePair(): Promise<MobilePair | undefined> {
  const db = await localDatabase();
  return new Promise((resolve, reject) => {
    const request = db.transaction(DEVICE).objectStore(DEVICE).get('pair');
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

type PairVersion = { room?: string; revision?: string };

export async function phonePairVersion(): Promise<PairVersion> {
  const db = await localDatabase();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(DEVICE);
    const store = tx.objectStore(DEVICE);
    const pair = store.get('pair');
    const revision = store.get(PAIR_VERSION);
    tx.oncomplete = () => resolve({ room: pair.result?.room, revision: revision.result });
    tx.onabort = () => reject(tx.error ?? new Error('本机配对读取未完成。'));
  });
}

export async function savePair(pair: MobilePair | undefined, expected?: string | PairVersion): Promise<boolean> {
  const db = await localDatabase();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(DEVICE, 'readwrite', { durability: 'strict' });
    const store = tx.objectStore(DEVICE);
    let changed = false;
    let roomChanged = false;
    const previous = store.get('pair');
    const revision = store.get(PAIR_VERSION);
    revision.onsuccess = () => {
      const oldRoom = previous.result?.room;
      if (typeof expected === 'string' ? expected !== oldRoom :
        expected && (expected.room !== oldRoom || expected.revision !== revision.result)) return;
      roomChanged = oldRoom !== pair?.room;
      if (roomChanged) {
        // Distinguish a device that returned to being unpaired from one that never changed.
        store.put(crypto.randomUUID(), PAIR_VERSION);
        if (oldRoom) store.delete(pushRemovalKey(oldRoom));
      }
      if (pair) store.put(pair, 'pair');
      else store.delete('pair');
      changed = true;
    };
    tx.oncomplete = () => {
      if (roomChanged) pairChanges?.postMessage(null);
      resolve(changed);
    };
    tx.onabort = () => reject(tx.error ?? new Error('本机配对保存未完成。'));
  });
}

export async function pendingPushRemoval(room: string): Promise<string | undefined> {
  const db = await localDatabase();
  return new Promise((resolve, reject) => {
    const request = db.transaction(DEVICE).objectStore(DEVICE).get(pushRemovalKey(room));
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

export async function savePushRemoval(room: string, endpointId: string, acknowledged = false): Promise<boolean> {
  const db = await localDatabase();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(DEVICE, 'readwrite', { durability: 'strict' });
    const store = tx.objectStore(DEVICE);
    const key = pushRemovalKey(room);
    let changed = false;
    const pair = store.get('pair');
    pair.onsuccess = () => {
      if (pair.result?.room !== room || pair.result.invitation) return;
      if (!acknowledged) {
        store.put(endpointId, key);
        changed = true;
      } else {
        const pending = store.get(key);
        pending.onsuccess = () => {
          if (pending.result !== endpointId) return;
          store.delete(key);
          changed = true;
        };
      }
    };
    tx.oncomplete = () => resolve(changed);
    tx.onabort = () => reject(tx.error ?? new Error('手机推送停用状态保存未完成。'));
  });
}

let desktopReady: Promise<DesktopOfflineStore> | undefined;

async function importExistingBrowserStudy(store: DesktopOfflineStore) {
  if (!(await indexedDB.databases()).some(database => database.name === DATABASE)) return store;
  const db = await localDatabase();
  const records = await new Promise<{ items: OfflineRecord[]; ledger?: unknown }>((resolve, reject) => {
    const tx = db.transaction([ITEMS, MOBILE_TIMERS]);
    const items = tx.objectStore(ITEMS).getAll();
    const ledger = tx.objectStore(MOBILE_TIMERS).get('ledger');
    tx.oncomplete = () => resolve({ items: items.result, ledger: ledger.result });
    tx.onabort = () => reject(tx.error ?? new Error('无法读取旧版离线记录。'));
  });
  for (const item of records.items) await store.write(ITEMS, item.key, 0, item);
  if (records.ledger !== undefined) await store.write(MOBILE_TIMERS, 'ledger', 0, records.ledger);
  return store;
}

export function desktopStudyStorage() {
  if (!hasDesktopSession()) return undefined;
  const store = window.zhixueDesktop?.offline ?? (window.zhixueTauriOffline ? tauriOfflineStore : undefined);
  if (!store) return undefined;
  desktopReady ??= importExistingBrowserStudy(store).catch(error => { desktopReady = undefined; throw error; });
  return desktopReady;
}

export async function downloaded(): Promise<OfflineRecord[]> {
  const desktop = await desktopStudyStorage();
  if (desktop) return await desktop.list() as OfflineRecord[];
  const db = await localDatabase();
  const request = db.transaction(ITEMS).objectStore(ITEMS).getAll();
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

type DownloadVersion = string | number | null;
type DownloadMutation = { action: 'download'; version: DownloadVersion } | { action: 'remove' };

export async function downloadVersion(key: string): Promise<DownloadVersion> {
  const desktop = await desktopStudyStorage();
  if (desktop) return (await desktop.read(ITEMS, key)).revision;
  const db = await localDatabase();
  const tx = db.transaction(DEVICE);
  const request = tx.objectStore(DEVICE).get(downloadRemovalKey(key));
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve(request.result ?? null);
    tx.onabort = () => reject(tx.error ?? new Error('无法读取本机下载版本。'));
  });
}

export async function changeItem(key: string, edit: (current?: OfflineRecord) => OfflineRecord | undefined,
  room?: string, mutation?: DownloadMutation): Promise<OfflineRecord | undefined> {
  const desktop = await desktopStudyStorage();
  if (desktop) return changeDesktopRecord<OfflineRecord, OfflineRecord | undefined>(desktop, ITEMS, key, (current, revision) => {
    if (!current && mutation?.action === 'download' && mutation.version !== revision) {
      return { value: undefined, result: undefined, unchanged: true };
    }
    const value = edit(current);
    // Ignore no-op receipts for an absent item. Its removal revision still guards racing downloads.
    return { value, result: value, unchanged: !current && !value && mutation?.action !== 'remove' };
  });
  const db = await localDatabase();
  return new Promise((resolve, reject) => {
    let result: OfflineRecord | undefined;
    let failure: unknown;
    const tx = offlineTransaction(db, ITEMS, 'readwrite', room, storage => {
      const lookup = storage.get(key);
      const device = mutation ? storage.transaction.objectStore(DEVICE) : undefined;
      const marker = mutation?.action === 'download' ? device!.get(downloadRemovalKey(key)) : undefined;
      const ready = marker ?? lookup;
      ready.onsuccess = () => {
        try {
          const previous = lookup.result as OfflineRecord | undefined;
          if (!previous && mutation?.action === 'download' && (marker!.result ?? null) !== mutation.version) return;
          result = edit(previous);
          if (result) storage.put(result);
          else if (previous) storage.delete(key);
          if (mutation?.action === 'remove') device!.put(crypto.randomUUID(), downloadRemovalKey(key));
        } catch (error) {
          failure = error;
          tx.abort();
        }
      };
    }, () => { failure = new PairingChanged(); }, mutation !== undefined);
    tx.oncomplete = () => resolve(result);
    tx.onabort = () => reject(failure ?? tx.error ?? new Error('本机保存未完成。'));
  });
}

export function saveLesson(snapshot: LessonSnapshot, room?: string, version?: DownloadVersion) {
  const key = studyKey('lesson', snapshot.workspace, snapshot.id);
  // Another tab may have downloaded the lesson and synced its note while this request was in flight.
  return changeItem(key, previous => previous ?? { kind: 'lesson', key, snapshot, note: snapshot.note },
    room, version === undefined ? undefined : { action: 'download', version });
}

export function savePaper(snapshot: Paper, room?: string, version?: DownloadVersion) {
  const key = studyKey('review', snapshot.workspace, snapshot.id);
  return changeItem(key, previous => previous ?? {
    kind: 'review', key, snapshot, draft: snapshot.questions.map(() => null),
  }, room, version === undefined ? undefined : { action: 'download', version });
}

export function hasLocalWork(record: OfflineRecord) {
  return record.kind === 'lesson' ? !!record.note?.editedAt
    : record.submission ? !record.reply || record.reply.state === 'grading' : record.draft.some(answer => answer !== null && answer !== '');
}

export function removeDownload(key: string) {
  return changeItem(key, current => {
    if (current && hasLocalWork(current)) throw new Error('这份内容有本机草稿或待同步记录。');
    return undefined;
  }, undefined, { action: 'remove' });
}

export class NoteEditConflict extends Error {
  constructor(readonly current: string) {
    super('本机笔记已更新，输入内容还在编辑框中。');
  }
}

export function saveNote(key: string, expected: string, content: string) {
  return changeItem(key, entry => {
    if (entry?.kind !== 'lesson' || !entry.note) throw new Error('找不到本机笔记。');
    if (entry.note.content !== expected) throw new NoteEditConflict(entry.note.content);
    if (content === expected) return entry;
    return { ...entry, note: { ...entry.note, content, editedAt: new Date().toISOString() } };
  });
}

export function saveAnswers(key: string, answers: (Answer | null)[], action?: ReviewChange['action']) {
  return changeItem(key, entry => {
    if (entry?.kind !== 'review') throw new Error('找不到本机题组。');
    if (entry.submission) {
      if (action) return entry;
      throw new Error('这一轮的作答已提交。');
    }
    const paper = entry.snapshot;
    if (!action) return { ...entry, draft: answers };
    if (action === 'submit' && answers.some(value => value === null || typeof value === 'string' && !value.trim())) {
      throw new Error('请答完本组所有题目。');
    }
    const submission: ReviewChange = {
      id: crypto.randomUUID(), workspace: paper.workspace, kind: 'review', capturedAt: new Date().toISOString(),
      lessonId: paper.lessonId, key: paper.key, round: paper.round, groupId: paper.groupId, basis: paper.basis,
      action, answers: action === 'unknown' ? [] : answers.filter(value => value !== null),
    };
    return { ...entry, draft: answers, submission };
  });
}

export async function noteForSync(key: string, room?: string): Promise<LocalNote | null> {
  const saved = await changeItem(key, entry => {
    if (entry?.kind !== 'lesson' || !entry.note?.editedAt || entry.note.pending) return entry;
    const { note } = entry;
    const pending: NoteChange = {
      id: crypto.randomUUID(), workspace: entry.snapshot.workspace, kind: 'note', capturedAt: note.editedAt!,
      noteId: note.id, baseRevision: note.revision, content: note.content,
    };
    return { ...entry, note: { ...note, pending } };
  }, room);
  return saved?.kind === 'lesson' ? saved.note : null;
}

export function acceptNote(key: string, reply: NoteReply, room?: string) {
  return changeItem(key, entry => {
    if (entry?.kind !== 'lesson' || entry.note?.pending?.id !== reply.id) return entry;
    if (reply.state === 'conflict') return { ...entry, note: { ...entry.note, conflict: reply.conflictId } };
    const note = entry.note;
    // The computer retained the exact sent draft. Later phone edits are based on that draft.
    const latest = note.content !== note.pending!.content;
    return { ...entry, note: { id: note.id, title: note.title, content: latest ? note.content : reply.content,
      revision: latest ? reply.submittedRevision : reply.revision, editedAt: latest ? note.editedAt : undefined } };
  }, room);
}

export function acceptReview(key: string, reply: ReviewReply, room?: string) {
  return changeItem(key, entry => {
    if (entry?.kind !== 'review' || entry.submission?.id !== reply.id) return entry;
    // Another tab can finish syncing before an earlier grading acknowledgement arrives.
    if (entry.reply && entry.reply.state !== 'grading') return entry;
    return { ...entry, reply };
  }, room);
}
