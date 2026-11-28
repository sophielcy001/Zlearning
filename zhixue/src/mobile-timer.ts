import { mobileRequest } from './mobile-data';
import { changeDesktopRecord } from './desktop-offline-store';
import { desktopStudyStorage, localDatabase, MOBILE_TIMERS, offlineTransaction, PairingChanged } from './mobile-store';

export type TimerTarget = {
  workspace: string; lessonId: string; title: string;
  category: 'learning' | 'notes' | 'review'; reviewKey?: string;
};
type Action = 'start' | 'pause' | 'resume' | 'finish';
type Clock = { offset: number; dayShift: number };
type Mark = { wall: number; clock?: Clock; stable: boolean; server?: boolean };
type Segment = { mark: Mark; startedBy: string };
export type LocalTimer = TimerTarget & { id: string; elapsed: number; running?: Segment };
type QueuedTimerEvent = TimerTarget & {
  id: string; timerId: string; action: Action; at: Mark; connected: boolean;
  segment?: Segment; request?: PhoneTimerEvent;
};
export type PhoneTimerEvent = {
  id: string; workspace: string; kind: 'timer'; timerId: string; lessonId: string;
  category: TimerTarget['category']; reviewKey?: string; action: Action; capturedAt: string;
  live: boolean; expected: string | null; present: boolean;
  span: { beganAt: string; endedAt: string; reliable: boolean; dayShift: number; startedBy: string } | null;
};
export type TimerClock = {
  workspace: string; serverAt: string; dayShift: number;
  active: { id: string; device: 'desktop' | 'phone' } | null;
  interruptedAt: string | null; pendingReview: number;
};
type PhoneTimerReply = { id: string; state: 'saved' | 'held' | 'resolved'; seconds: number };
type Ledger = { active?: LocalTimer; events: QueuedTimerEvent[]; clocks: Record<string, Clock> };

const EMPTY: Ledger = { events: [], clocks: {} };
const WALL_TOLERANCE = 2500;
const MAX_HANDOFF_AGE = 7000;
let wallReference = { wall: Date.now(), mono: performance.now() };

async function operation<T>(edit?: (ledger: Ledger) => T, room?: string): Promise<Ledger | T> {
  const desktop = await desktopStudyStorage();
  if (desktop) {
    if (!edit) {
      const { value } = await desktop.read(MOBILE_TIMERS, 'ledger');
      return (value as Ledger | undefined) ?? { events: [], clocks: {} };
    }
    return changeDesktopRecord<Ledger, T>(desktop, MOBILE_TIMERS, 'ledger', previous => {
      const ledger = previous ?? { events: [], clocks: {} };
      return { value: ledger, result: edit(ledger) };
    });
  }
  const db = await localDatabase();
  return new Promise((resolve, reject) => {
    let value: Ledger | T;
    let failure: unknown;
    const tx = offlineTransaction(db, MOBILE_TIMERS, edit ? 'readwrite' : 'readonly', room, store => {
      const lookup = store.get('ledger');
      lookup.onsuccess = () => {
        try {
          const ledger: Ledger = lookup.result ?? { events: [], clocks: {} };
          value = edit ? edit(ledger) : ledger;
          if (edit) store.put(ledger, 'ledger');
        } catch (error) {
          failure = error;
          tx.abort();
        }
      };
    }, () => { failure = new PairingChanged(); });
    tx.oncomplete = () => resolve(value);
    tx.onabort = () => reject(failure ?? tx.error ?? new Error('本机计时保存未完成。'));
  });
}

export function timerLedger() {
  return operation() as Promise<Ledger>;
}

function editLedger<T>(edit: (ledger: Ledger) => T, room?: string) {
  return operation(edit, room) as Promise<T>;
}

export function timerCount(ledger: Ledger = EMPTY) {
  return ledger.events.length;
}

function mark(ledger: Ledger, workspace: string): Mark {
  const wall = Date.now();
  const expected = wallReference.wall + performance.now() - wallReference.mono;
  return { wall, clock: ledger.clocks[workspace], stable: Math.abs(wall - expected) <= WALL_TOLERANCE };
}

function adjusted(at: Mark) {
  return at.wall + (at.clock?.offset ?? 0);
}

function event(timer: LocalTimer, action: Action, at: Mark, connected: boolean): QueuedTimerEvent {
  const { workspace, lessonId, title, category, reviewKey, id, running } = timer;
  return {
    workspace, lessonId, title, category, reviewKey, timerId: id, id: crypto.randomUUID(), action, at, connected,
    segment: action === 'finish' || action === 'pause' ? running : undefined,
  };
}

function sameTarget(first: TimerTarget, second: TimerTarget) {
  return first.workspace === second.workspace && first.lessonId === second.lessonId
    && first.category === second.category && first.reviewKey === second.reviewKey;
}

export function timerMilliseconds(timer: LocalTimer, now: number) {
  return timer.elapsed + (timer.running ? Math.max(0, now - timer.running.mark.wall) : 0);
}

export function saveTimerAction(intent: { action: 'start'; target: TimerTarget } | {
  action: 'pause' | 'resume' | 'finish'; id: string;
}, connectedWorkspace?: string) {
  return editLedger(ledger => {
    const current = ledger.active;
    if (intent.action === 'start') {
      if (current && sameTarget(current, intent.target)) return ledger;
      if (current) ledger.events.push(event(current, 'finish', mark(ledger, current.workspace), false));
      const timer: LocalTimer = { ...intent.target, id: crypto.randomUUID(), elapsed: 0 };
      const start = event(timer, 'start', mark(ledger, timer.workspace), timer.workspace === connectedWorkspace);
      ledger.events.push(start);
      ledger.active = { ...timer, running: { mark: start.at, startedBy: start.id } };
    } else {
      if (!current || current.id !== intent.id) throw new Error('计时已经变化，请刷新后再操作。');
      if ((intent.action === 'pause' && !current.running) || (intent.action === 'resume' && current.running)) return ledger;
      const change = event(current, intent.action, mark(ledger, current.workspace), current.workspace === connectedWorkspace);
      ledger.events.push(change);
      switch (intent.action) {
        case 'pause':
          ledger.active = { ...current, elapsed: timerMilliseconds(current, change.at.wall), running: undefined };
          break;
        case 'resume':
          ledger.active = { ...current, running: { mark: change.at, startedBy: change.id } };
          break;
        case 'finish':
          delete ledger.active;
          break;
      }
    }
    return ledger;
  });
}

function reliable(at: Mark, sample: Clock | undefined) {
  return at.server || !!(at.stable && at.clock && sample && Math.abs(at.clock.offset - sample.offset) <= WALL_TOLERANCE);
}

function request(value: QueuedTimerEvent, sample: Clock | undefined, reading: TimerClock, present: boolean): PhoneTimerEvent {
  const { id, workspace, timerId, lessonId, category, reviewKey, action, at, segment } = value;
  const phaseStart = action === 'start' || action === 'resume';
  const when = reliable(at, sample) ? adjusted(at) : at.wall + (sample?.offset ?? 0);
  const age = Date.now() + (sample?.offset ?? 0) - when;
  const live = phaseStart && present && value.connected && !!sample && reliable(at, sample) && age >= -1000 && age < MAX_HANDOFF_AGE;
  return {
    id, workspace, kind: 'timer', timerId, lessonId, category, reviewKey, action, capturedAt: new Date(when).toISOString(),
    live, present, expected: reading.active?.id ?? null,
    span: segment ? {
      beganAt: new Date(adjusted(segment.mark)).toISOString(), endedAt: new Date(adjusted(at)).toISOString(),
      dayShift: segment.mark.clock?.dayShift ?? sample?.dayShift ?? 0, startedBy: segment.startedBy,
      reliable: !!(reliable(segment.mark, sample) && reliable(at, sample)),
    } : null,
  };
}

async function readTimerClock(workspace: string, timerId: string | undefined, room?: string) {
  const startedWall = Date.now();
  const started = performance.now();
  const reading = await mobileRequest<TimerClock>({ op: 'timer-state', workspace, timerId }, room);
  const reference = { wall: Date.now(), mono: performance.now() };
  const elapsed = reference.mono - started;
  const serverTime = Date.parse(reading.serverAt);
  if (reading.workspace !== workspace || !Number.isFinite(serverTime)) throw new Error('计时工作区已变化。');
  const sample = elapsed <= 2000 && Math.abs((reference.wall - startedWall) - elapsed) <= 250
    ? { offset: serverTime - (startedWall + elapsed / 2), dayShift: reading.dayShift } : undefined;
  return { reading, reference, sample };
}

export async function synchronizeTimers(workspace: string, room?: string) {
  const original = await timerLedger();
  const timerId = original.active?.workspace === workspace ? original.active.id : undefined;
  let clock = await readTimerClock(workspace, timerId, room);
  if (!clock.sample) {
    // Recheck the current queue because a pause may have been saved during the first read.
    const current = await timerLedger();
    const hasUnsentSpan = current.events.some(event => event.workspace === workspace && event.segment && !event.request);
    const interruptedSpan = clock.reading.interruptedAt && current.active?.id === timerId && current.active?.running;
    if (hasUnsentSpan || interruptedSpan) clock = await readTimerClock(workspace, timerId, room);
  }
  const { reading, reference, sample } = clock;
  await editLedger(ledger => {
    if (sample) {
      wallReference = reference;
      ledger.clocks[workspace] = sample;
    }
    const active = ledger.active;
    if (!active || active.workspace !== workspace || active.id !== original.active?.id) return;
    if (reading.interruptedAt) {
      const cutoff = Date.parse(reading.interruptedAt);
      const ended = event(active, 'finish', { wall: cutoff, stable: true, server: true }, false);
      if (ended.segment && cutoff <= adjusted(ended.segment.mark) && reliable(ended.segment.mark, sample)) {
        ended.segment = undefined;
      }
      ledger.events.push(ended);
      delete ledger.active;
    } else if (!reading.active && active.running && !ledger.events.some(item => item.timerId === active.id)) {
      ledger.events.push(event(active, 'resume', mark(ledger, workspace), false));
    }
  }, room);

  let refreshReviewCount = false;
  for (;;) {
    const outgoing = await editLedger(ledger => {
      const next = ledger.events.find(item => item.workspace === workspace);
      if (!next) return undefined;
      const present = ledger.active?.id === next.timerId && ledger.active.running !== undefined
        && !ledger.events.some(item => item.timerId === next.timerId && item !== next && item.action !== 'start' && item.action !== 'resume');
      next.request ??= request(next, sample, reading, present);
      return next.request;
    }, room);
    if (!outgoing) break;
    const answer = await mobileRequest<PhoneTimerReply>({ op: 'timer-event', event: outgoing }, room);
    if (answer.id !== outgoing.id) throw new Error('计时回执编号有误。');
    await editLedger(ledger => { ledger.events = ledger.events.filter(item => item.id !== outgoing.id); }, room);
    if (answer.state !== 'saved') refreshReviewCount = true;
  }
  // Replayed receipts may already be counted in the initial snapshot.
  if (!refreshReviewCount) return reading.pendingReview;
  const latest = await mobileRequest<TimerClock>({ op: 'timer-state', workspace }, room);
  return latest.pendingReview;
}
