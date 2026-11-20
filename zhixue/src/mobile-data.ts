import { api, hasDesktopSession, type ReviewResult } from './api';
import { relayRequest } from './mobile-pairing';
import { PairingChanged, phonePair } from './mobile-store';
import type { PhoneTimerEvent } from './mobile-timer';
import type { ReminderDecision, ReminderInput, ReminderReceipt, ReminderSettings } from './reminder-data';

export type LessonChoice = { id: string; title: string; projectTopic: string };
export type ReviewChoice = LessonChoice & {
  lessonId: string; key: string; kind: 'local' | 'model'; round: number; dueOn: string;
};
export type Catalog = { workspace: string; lessons: LessonChoice[]; reviews: ReviewChoice[] };
export type NoteSnapshot = { id: string; title: string; revision: string | null; content: string };
export type LessonSnapshot = LessonChoice & { workspace: string; body: string; revision: string; note: NoteSnapshot | null };
export type Paper = ReviewChoice & {
  workspace: string; basis: string; groupId: string | null;
  questions: { text: string; options?: string[] }[];
};
export type Answer = string | number;
export type NoteChange = {
  id: string; workspace: string; kind: 'note'; capturedAt: string; noteId: string; baseRevision: string | null; content: string;
};
export type ReviewChange = {
  id: string; workspace: string; kind: 'review'; capturedAt: string; lessonId: string;
  key: string; round: number; groupId: string | null; basis: string; answers: Answer[]; action: 'submit' | 'unknown';
};
export type NoteReply = { id: string; submittedRevision: string } & (
  { state: 'saved'; revision: string; content: string; merged: boolean } |
  { state: 'conflict'; conflictId: string }
);
export type ReviewReply = { id: string; state: 'settled' | 'grading' | 'held'; reason?: string; result?: ReviewResult | null };
export type ReviewHistory = ReviewReply & {
  lessonId: string; key: string; round: number; answers: Answer[]; capturedAt: string; receivedAt: string;
};

export type StudyCommand = { op: 'catalog' | 'history' } | { op: 'course'; lesson: string } |
  { op: 'paper'; lesson: string; key: string } | { op: 'receive'; change: NoteChange | ReviewChange } |
  { op: 'receipt'; id: string; workspace: string } |
  { op: 'timer-state'; workspace: string; timerId?: string } | { op: 'timer-event'; event: PhoneTimerEvent } |
  ({ op: 'reminder-check'; workspace: string } & ReminderInput) |
  ({ op: 'reminder-action'; workspace: string; action: ReminderDecision } & ReminderReceipt) |
  { op: 'reminder-settings'; workspace: string } |
  ({ op: 'reminder-save'; workspace: string } & ReminderSettings) |
  { op: 'push-status'; workspace: string } |
  { op: 'push-subscribe'; workspace: string; subscription: PushSubscriptionJSON } |
  { op: 'push-unsubscribe'; workspace: string; endpointId: string };

export async function mobileRequest<T>(command: StudyCommand, room?: string): Promise<T> {
  if (!hasDesktopSession()) {
    const pair = await phonePair();
    if (room !== undefined && (pair?.room !== room || pair.invitation)) throw new PairingChanged();
    if (!pair || pair.invitation) throw new Error('请先在手机上打开电脑生成的配对链接。');
    const reply = await relayRequest<T>(pair, command);
    if (room !== undefined) {
      const current = await phonePair();
      if (current?.room !== room || current.invitation) throw new PairingChanged();
    }
    return reply;
  }
  const path = '/mobile';
  const timeout = AbortSignal.timeout(10000);
  switch (command.op) {
    case 'reminder-check': return api<T>(path + '/reminders/check', 'POST', command, timeout);
    case 'reminder-action': return api<T>(path + '/reminders/action', 'POST', command, timeout);
    case 'reminder-settings': return api<T>(path + '/reminders/settings?' + new URLSearchParams({ workspace: command.workspace }), 'GET', undefined, timeout);
    case 'reminder-save': return api<T>(path + '/reminders/settings', 'PUT', command, timeout);
    case 'push-status':
    case 'push-subscribe':
    case 'push-unsubscribe': throw new Error('请在已配对的手机上管理系统推送。');
    case 'timer-state': {
      const query = new URLSearchParams({ workspace: command.workspace });
      if (command.timerId) query.set('timerId', command.timerId);
      return api<T>(path + '/timer?' + query, 'GET', undefined, timeout);
    }
    case 'timer-event': return api<T>(path + '/timer', 'POST', command.event, timeout);
    case 'catalog': return api<T>(path + '/catalog', 'GET', undefined, timeout);
    case 'history': return api<T>(path + '/changes', 'GET', undefined, timeout);
    case 'course': return api<T>(path + '/lessons/' + command.lesson, 'GET', undefined, timeout);
    case 'paper': return api<T>(path + '/lessons/' + command.lesson + '/reviews/' + encodeURIComponent(command.key), 'GET', undefined, timeout);
    case 'receive': return api<T>(path + '/changes', 'POST', command.change, timeout);
    case 'receipt': return api<T>(path + '/changes/' + command.id + '?' + new URLSearchParams({ workspace: command.workspace }), 'GET', undefined, timeout);
  }
}

export function studyKey(kind: 'lesson' | 'review', workspace: string, identity: string) {
  return [workspace, kind, identity].join(':');
}
