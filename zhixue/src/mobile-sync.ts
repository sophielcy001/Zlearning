import { ApiError } from './api';
import { RelayUnavailable } from './mobile-pairing';
import { mobileRequest, type NoteReply, type ReviewReply } from './mobile-data';
import { acceptNote, acceptReview, downloaded, noteForSync, PairingChanged, type OfflineRecord } from './mobile-store';

export function needsSync(record: OfflineRecord) {
  return record.kind === 'lesson' ? !!record.note?.editedAt
    : !!record.submission && (!record.reply || record.reply.state === 'grading');
}

async function transfer(entry: OfflineRecord, workspace: string, room?: string) {
  if (entry.kind === 'lesson') {
    const note = await noteForSync(entry.key, room);
    if (!note?.pending) return;
    const reply = note.conflict
      ? await mobileRequest<NoteReply>({ op: 'receipt', id: note.pending.id, workspace }, room)
      : await mobileRequest<NoteReply>({ op: 'receive', change: note.pending }, room);
    await acceptNote(entry.key, reply, room);
  } else if (entry.submission) {
    const reply = entry.reply
      ? await mobileRequest<ReviewReply>({ op: 'receipt', id: entry.submission.id, workspace }, room)
      : await mobileRequest<ReviewReply>({ op: 'receive', change: entry.submission }, room);
    await acceptReview(entry.key, reply, room);
  }
}

export async function synchronize(workspace: string, room?: string) {
  const entries = (await downloaded()).filter(record => record.snapshot.workspace === workspace && needsSync(record));
  let failure: unknown;
  for (const entry of entries) {
    try { await transfer(entry, workspace, room); }
    catch (error) {
      if (error instanceof PairingChanged || error instanceof RelayUnavailable || error instanceof ApiError && [401, 410, 503].includes(error.status)) throw error;
      failure ??= error;
    }
  }
  if (failure) throw failure;
}
