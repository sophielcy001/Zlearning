import { useEffect, useState, type FormEvent } from 'react';
import { api, dateLabel } from './api';
import { ErrorNote, Loading, Modal, useAction } from './shared';

export const PLAN = '/study-plan';
export const WEEKDAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];

export function dayLabel(day: string) {
  const weekday = new Date(`${day}T00:00:00Z`).getUTCDay();
  return `${dateLabel(day)} ${WEEKDAYS[(weekday + 6) % 7]}`;
}

export type TimeWindow = { start: string; end: string };
export type MoveTask = {
  id: string; title: string; projectTopic: string; minutes: number; kind: 'lesson' | 'review'; dueOn?: string | null;
};
type Target = { date: string; start: string };
export type Slot = Target & { end: string; locked: boolean };
export type MoveDraft = { task: MoveTask; target: Target; preview?: boolean; release?: boolean };
type MoveRequest = { taskId: string; placement: Target | null };
type MoveChange = { id: string; title: string; projectTopic: string; before: Slot | null; after: Slot | null };
type Conflict = { code: string; message: string; task?: MoveTask & Slot };
type Preview = {
  before: Slot | null; after: Slot | null; changes: MoveChange[]; conflicts: Conflict[]; confirmation: string | null;
};

function minute(text: string) {
  const [hour, minutes] = text.split(':').map(Number);
  return hour * 60 + minutes;
}

export function suggestedStart(windows: TimeWindow[], length: number, preferred?: string) {
  if (preferred && windows.some(window => preferred >= window.start && minute(preferred) + length <= minute(window.end))) return preferred;
  return windows.find(window => minute(window.end) - minute(window.start) >= length)?.start ?? windows[0]?.start ?? preferred ?? '19:00';
}

function slotText(slot: Slot | null) {
  return slot ? `${dayLabel(slot.date)} · ${slot.start}–${slot.end}${slot.locked ? ' · 已锁定' : ''}` : '等待安排';
}

function PreviewDetails({ result, projectedThrough }: { result: Preview; projectedThrough: string }) {
  return <div className="study-move-preview" aria-label="排期预览">
    <dl>
      <div><dt>当前</dt><dd>{slotText(result.before)}</dd></div>
      <div><dt>{result.conflicts.length ? '所选时段' : '确认后'}</dt><dd>{slotText(result.after)}</dd></div>
    </dl>
    {result.conflicts.length > 0 && <ul className="notice error study-move-conflicts" role="alert">
      {result.conflicts.map(conflict => <li key={conflict.task?.id ?? conflict.code}>
        {conflict.message}{conflict.task && <small>{slotText(conflict.task)}</small>}
      </li>)}
    </ul>}
    {result.conflicts.length === 0 && <>
      {result.after && result.after.date > projectedThrough && <p className="dialog-hint">已核对该日的时段和锁定任务。自动安排预览覆盖当前七天。</p>}
      {result.changes.length > 0 ? <details open={result.changes.length <= 4} className="study-move-changes">
        <summary>{result.changes.length} 项自动任务会调整</summary>
        <ul>{result.changes.map(change => <li key={change.id}>
          <strong>{change.title}</strong><small>{change.projectTopic}</small>
          <span>{slotText(change.before)} → {slotText(change.after)}</span>
        </li>)}</ul>
      </details> : <p className="dialog-hint">本次调整涉及 1 项任务。</p>}
    </>}
  </div>;
}

export function MoveEditor({ draft, today, projectedThrough, close, onSaved }: {
  draft: MoveDraft; today: string; projectedThrough: string; close: () => void; onSaved: () => void;
}) {
  const [target, setTarget] = useState(draft.target);
  const [request, setRequest] = useState<MoveRequest | null>(() => draft.preview || draft.release
    ? { taskId: draft.task.id, placement: draft.release ? null : draft.target } : null);
  const [result, setResult] = useState<{ request: MoveRequest; preview?: Preview; error?: string }>();
  const [submitted, setSubmitted] = useState<MoveRequest>();
  const action = useAction();

  useEffect(() => {
    if (!request) return;
    const controller = new AbortController();
    api<Preview>(`${PLAN}/moves/preview`, 'POST', request, controller.signal)
      .then(preview => {
        if (!controller.signal.aborted) setResult({ request, preview });
      })
      .catch((error: Error) => {
        if (!controller.signal.aborted) setResult({ request, error: error.message });
      });
    return () => controller.abort();
  }, [request]);

  const response = result?.request === request ? result : undefined;
  const loading = request !== null && !response;

  function updateTarget(changes: Partial<Target>) {
    setTarget(current => ({ ...current, ...changes }));
    setRequest(null);
  }
  function preview(event: FormEvent) {
    event.preventDefault();
    setRequest({ taskId: draft.task.id, placement: draft.release ? null : target });
  }
  async function confirm() {
    const confirmation = response?.preview?.confirmation;
    if (!request || !confirmation) return;
    setSubmitted(request);
    const saved = await action.run(() => api(`${PLAN}/moves/confirm`, 'POST', { ...request, confirmation }));
    if (saved) onSaved();
  }

  return <Modal title={draft.release ? '解除锁定' : '调整排期'} onClose={close} busy={action.pending}>
    <form className="project-dialog-form study-move-form" onSubmit={preview}>
      <div><strong>{draft.task.title}</strong>
        <p className="dialog-hint">{draft.task.projectTopic} · {draft.task.minutes} 分钟
          {draft.task.dueOn && ` · 原到期日 ${dateLabel(draft.task.dueOn)}`}</p>
      </div>
      {draft.release ? <p className="dialog-hint">解除后，符合条件的任务会回到自动安排。</p>
        : <fieldset className="study-move-fields" disabled={action.pending}>
          <label>目标日期<input type="date" required min={today} value={target.date} onChange={event =>
            updateTarget({ date: event.target.value })} /></label>
          <label>开始时间<input type="time" required value={target.start} onChange={event =>
            updateTarget({ start: event.target.value })} /></label>
          <span className="dialog-hint">确认后锁定所选时段。时间为北京时间。</span>
        </fieldset>}
      {loading && <Loading text="正在计算改期影响" />}
      <ErrorNote message={response?.error} />
      {response?.preview && <PreviewDetails result={response.preview} projectedThrough={projectedThrough} />}
      {submitted === request && <ErrorNote message={action.error} />}
      <div className="project-dialog-actions">
        <button type="button" className="button secondary" onClick={close} disabled={action.pending}>取消</button>
        <button className="button secondary" disabled={loading || action.pending}>{request ? '重新预览' : '预览改期'}</button>
        {response?.preview?.confirmation && <button type="button" className="button primary"
          disabled={action.pending || Boolean(submitted === request && action.error)} onClick={() => void confirm()}>
          {draft.release ? '确认解除锁定' : '确认并锁定'}
        </button>}
      </div>
    </form>
  </Modal>;
}
