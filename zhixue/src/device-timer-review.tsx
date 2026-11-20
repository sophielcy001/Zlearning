import { useEffect, useState } from 'react';
import { api, type TimeCategory } from './api';
import { ErrorNote, useAction, useRead } from './shared';
import { TIME_CATEGORIES } from './study-timer';

type Period = { beganAt: string; endedAt: string };
type RecordForReview = {
  id: string; reason: 'clock' | 'overlap'; title: string | null; category: TimeCategory;
  span: Period; seconds: number; overlaps: (Period & { timerId: string; device: 'desktop' | 'phone' | 'handoff' })[];
};
const PATH = '/mobile/timer/conflicts';
const display = (value: string) => new Date(value).toLocaleString();

function localField(value: string) {
  const date = new Date(value);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 19);
}

function TimerReview({ record, refresh }: { record: RecordForReview; refresh: () => void }) {
  const action = useAction();
  const [begin, setBegin] = useState(() => localField(record.span.beganAt));
  const [end, setEnd] = useState(() => localField(record.span.endedAt));
  const apply = (body: object) => { void action.run(async () => {
    await api(PATH + '/' + record.id, 'POST', body);
    refresh();
  }); };
  return <details className="panel">
    <summary>{record.title ?? '手机计时'} · {TIME_CATEGORIES[record.category]} · {record.reason === 'clock' ? '校对时间' : '有重叠'}</summary>
    <p>手机原记录 {display(record.span.beganAt)} 至 {display(record.span.endedAt)}</p>
    <p>已计入 {record.seconds} 秒</p>
    {record.reason === 'overlap' && <ul className="report-list">
      {record.overlaps.map((period, index) => <li key={period.timerId + '/' + index}>
        {period.device === 'handoff' ? '计时已交接' : period.device === 'desktop' ? '电脑记录' : '已收手机记录'} · {' '}
        {display(period.beganAt)} 至 {display(period.endedAt)}
      </li>)}
    </ul>}
    {record.reason === 'clock' && <form className="action-row" onSubmit={event => {
      event.preventDefault();
      apply({ action: 'confirm', beganAt: new Date(begin).toISOString(), endedAt: new Date(end).toISOString() });
    }}>
      <label>开始时间 <input type="datetime-local" step="1" required value={begin} onChange={event => setBegin(event.target.value)} /></label>
      <label>结束时间 <input type="datetime-local" step="1" required value={end} onChange={event => setEnd(event.target.value)} /></label>
      <button type="submit" className="button primary" disabled={action.pending}>确认时段</button>
    </form>}
    <button type="button" className="button secondary" disabled={action.pending} onClick={() => apply({ action: 'keep' })}>
      {record.reason === 'clock' ? '仅留原始记录' : '保留已计入时段'}
    </button>
    <ErrorNote message={action.error} />
  </details>;
}

export function DeviceTimerReview({ onChange }: { onChange: () => void }) {
  const review = useRead<{ records: RecordForReview[] }>(PATH);
  const records = review.value?.records ?? [];
  const { refresh } = review;
  useEffect(() => {
    const timer = window.setInterval(() => { if (document.visibilityState === 'visible') refresh(); }, 6000);
    return () => window.clearInterval(timer);
  }, [refresh]);
  if (!records.length && !review.error) return null;
  return <section className="panel settings">
    <h2>手机计时待核对</h2>
    <ErrorNote message={review.error} retry={refresh} />
    {records.map(record => <TimerReview key={record.id} record={record} refresh={() => { refresh(); onChange(); }} />)}
  </section>;
}
