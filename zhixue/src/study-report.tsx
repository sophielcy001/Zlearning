import { useEffect, useState, type FormEvent } from 'react';
import { ArrowRight, ChevronLeft, ChevronRight } from 'lucide-react';
import { Link, useSearchParams } from 'react-router-dom';
import { api, courseUrl, dateLabel, projectUrl, reviewUrl, type TimeCategory } from './api';
import { BackedStatus, Empty, ErrorNote, Modal, useAction, useRead } from './shared';
import { TIME_CATEGORIES } from './study-timer';

type Period = 'day' | 'week' | 'month';
type Outcome = 'passed' | 'hinted' | 'failed' | 'ungraded';
type TimeRecord = {
  timerId: string; lessonId: string; lessonTitle: string; projectTopic: string;
  sourceOn: string | null; reportOn: string | null; recordedSeconds: number; seconds: number;
  recordedCategory: TimeCategory; category: TimeCategory; revision: number; reviewKey: string | null;
  reviewTitle: string | null; reviewKind: 'local' | 'model' | null;
};
type Efficiency = { trackedLessons: number; secondsPerLesson: number | null };
type Report = {
  today: string; from: string; through: string; previous: string | null; next: string | null;
  totals: {
    actualSeconds: number; learned: number; byCategory: Record<TimeCategory, number>; review: Record<Outcome, number>;
    plannedSeconds: number | null; recordedPlanDays: number; planBudgetSeconds: number | null; actualOnPlannedDaysSeconds: number | null;
    planDifferenceSeconds: number | null;
  };
  trend: { date: string; actualSeconds: number; plannedSeconds: number | null; learned: number }[];
  learned: { lessonId: string; title: string; projectTopic: string; date: string }[];
  weak: { lessonId: string; key: string; title: string; projectTopic: string; on: string; outcome: Outcome; groupId: string | null }[];
  efficiency: Efficiency; previousEfficiency: Efficiency | null;
  predictions: { projectId: string; projectTopic: string; left: number; notReady: number; expectedOn: string | null; timerEstimate: boolean }[];
  predictionDays: number;
  timeRecords: TimeRecord[]; unassignedTimers: TimeRecord[];
};

const API = '/study-report';
const PERIODS: Record<Period, string> = { day: '日', week: '周', month: '月' };
const OUTCOMES: Record<Outcome, string> = { passed: '独立通过', hinted: '辅助作答', failed: '需要再练习', ungraded: '未评分' };
const categories = Object.keys(TIME_CATEGORIES) as TimeCategory[];
const periods = Object.keys(PERIODS) as Period[];
const outcomes = Object.keys(OUTCOMES) as Outcome[];

function duration(seconds: number) {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor(seconds / 60) % 60;
  const remainder = seconds % 60;
  const parts = [hours > 0 && `${hours} 小时`, minutes > 0 && `${minutes} 分钟`, remainder > 0 && `${remainder} 秒`];
  return parts.filter(Boolean).join(' ') || '0 分钟';
}

function dateRange(start: string, end: string) {
  return start === end ? dateLabel(start) : `${dateLabel(start)} 至 ${dateLabel(end)}`;
}

function ReviewSummary({ counts }: { counts: Record<Outcome, number> }) {
  return <dl className="report-breakdown">
    {outcomes.map(outcome => <div key={outcome}>
      <dt>{OUTCOMES[outcome]}</dt><dd>{counts[outcome]}</dd>
    </div>)}
  </dl>;
}

function Trend({ data }: { data: Report['trend'] }) {
  const largest = Math.max(60, ...data.map(row => Math.max(row.actualSeconds, row.plannedSeconds ?? 0)));
  return <section className="panel report-section">
    <h2>学习趋势</h2>
    <div className="report-trend-table">
      <table>
        <thead><tr><th scope="col">日期</th><th scope="col">确认时长</th><th scope="col">计划估时</th><th scope="col">已学知识点</th></tr></thead>
        <tbody>{data.map(row => <tr key={row.date}>
          <th scope="row"><time dateTime={row.date}>{dateLabel(row.date)}</time></th>
          <td><div className="report-timed"><meter min={0} max={largest} value={row.actualSeconds}
            aria-label={`${dateLabel(row.date)}确认时长`} aria-valuetext={duration(row.actualSeconds)} />{duration(row.actualSeconds)}</div></td>
          <td>{row.plannedSeconds === null ? '未记录' : duration(row.plannedSeconds)}</td>
          <td>{row.learned}</td>
        </tr>)}</tbody>
      </table>
    </div>
  </section>;
}

function Comparison({ data }: { data: Report }) {
  const { totals, efficiency, previousEfficiency } = data;
  return <section className="panel report-section">
    <h2>计划与实际</h2>
    {totals.plannedSeconds === null ? <p className="report-hint">这个时段还没有保存的计划估时。</p> : <>
      <p className="report-hint">比较有计划记录的 {totals.recordedPlanDays} 天。当天基准在首次学习操作时锁定。</p>
      <dl className="report-breakdown">
        <div><dt>可用时间预算</dt><dd>{duration(totals.planBudgetSeconds ?? 0)}</dd></div>
        <div><dt>计划估时</dt><dd>{duration(totals.plannedSeconds)}</dd></div>
        <div><dt>对应确认时长</dt><dd>{duration(totals.actualOnPlannedDaysSeconds ?? 0)}</dd></div>
        <div><dt>实际与计划差额</dt><dd>{!totals.planDifferenceSeconds ? '0 分钟'
          : `${totals.planDifferenceSeconds > 0 ? '多 ' : '少 '}${duration(Math.abs(totals.planDifferenceSeconds))}`}</dd></div>
      </dl>
    </>}
    <h3>平均阅读时长</h3>
    {efficiency.secondsPerLesson === null ? <p className="report-hint">当前时段暂无已学且有确认阅读计时的知识点。</p> : <dl className="report-breakdown">
      <div><dt>本周期 · {efficiency.trackedLessons} 个知识点</dt><dd>{duration(efficiency.secondsPerLesson)}</dd></div>
      {previousEfficiency?.secondsPerLesson != null && <div>
        <dt>上一周期对应时段 · {previousEfficiency.trackedLessons} 个知识点</dt>
        <dd>{duration(previousEfficiency.secondsPerLesson)}</dd>
      </div>}
    </dl>}
  </section>;
}

function TimerEditor({ entry, today, close, onSaved }: {
  entry: TimeRecord; today: string; close: () => void; onSaved: () => void;
}) {
  const [minutes, setMinutes] = useState(String(Math.floor(entry.seconds / 60)));
  const [seconds, setSeconds] = useState(String(entry.seconds % 60));
  const [day, setDay] = useState(entry.reportOn ?? '');
  const [category, setCategory] = useState(entry.category);
  const action = useAction();
  async function save(event: FormEvent) {
    event.preventDefault();
    const result = await action.run(() => api(`${API}/timers/${entry.timerId}/correction`, 'PUT', {
      sourceOn: entry.sourceOn, reportOn: entry.sourceOn ?? (day || null), category,
      seconds: Number(minutes) * 60 + Number(seconds), baseRevision: entry.revision,
    }));
    if (result) onSaved();
  }
  return <Modal title="校正计时" onClose={close} busy={action.pending}>
    <form className="project-dialog-form" onSubmit={save}>
      <div><strong>{entry.lessonTitle}</strong><p className="dialog-hint">{entry.projectTopic} · 原始计时 {duration(entry.recordedSeconds)} · {TIME_CATEGORIES[entry.recordedCategory]}
        {entry.sourceOn && ` · ${dateLabel(entry.sourceOn)}`}</p></div>
      <fieldset className="report-correction-fields" disabled={action.pending}>
        <label>有效分钟<input type="number" min="0" step="1" required value={minutes} onChange={event => setMinutes(event.target.value)} /></label>
        <label>有效秒数<input type="number" min="0" max="59" step="1" required value={seconds} onChange={event => setSeconds(event.target.value)} /></label>
        <label>活动类别<select value={category} onChange={event => setCategory(event.target.value as TimeCategory)}>
          {categories.map(item => <option key={item} value={item}>{TIME_CATEGORIES[item]}</option>)}
        </select></label>
        {entry.sourceOn === null && <label>归入日期<input type="date" value={day} max={today} onChange={event => setDay(event.target.value)} /></label>}
      </fieldset>
      <ErrorNote message={action.error} />
      <div className="project-dialog-actions">
        <button type="button" className="button secondary" disabled={action.pending} onClick={close}>取消</button>
        <button className="button primary" disabled={action.pending}>{action.pending ? '正在保存' : '保存校正'}</button>
      </div>
    </form>
  </Modal>;
}

function TimerRecords({ data, edit }: { data: Report; edit: (entry: TimeRecord) => void }) {
  function entries(items: TimeRecord[]) {
    return <ul className="report-list">{items.map(item => <li key={`${item.timerId}/${item.sourceOn}`}>
      <div><Link className="text-action" to={courseUrl(item.lessonId)}>{item.lessonTitle}</Link>
        <small>{item.projectTopic} · {item.reportOn ? dateLabel(item.reportOn) : '日期待补录'} · {TIME_CATEGORIES[item.category]}</small>
        {item.reviewKey && item.reviewKind && <small><Link className="text-action"
          to={reviewUrl({ kind: item.reviewKind, lessonId: item.lessonId, key: item.reviewKey })}>{item.reviewTitle}</Link></small>}
        <small>{duration(item.seconds)}{item.revision > 0 && ` · 原始 ${duration(item.recordedSeconds)}`}</small>
      </div>
      <button type="button" className="text-action" aria-label={`校正 ${item.lessonTitle} ${item.reportOn ?? '旧计时'}`} onClick={() => edit(item)}>校正</button>
    </li>)}</ul>;
  }
  return <>
    <details className="panel report-section">
      <summary>已确认计时记录（{data.timeRecords.length}）</summary>
      {data.timeRecords.length ? entries(data.timeRecords) : <p className="report-hint">这个时段暂无计时记录。</p>}
    </details>
    {!!data.unassignedTimers.length && <details className="panel report-section">
      <summary>日期待补录的旧计时（{data.unassignedTimers.length}）</summary>
      <p className="report-hint">旧版记录保存了总时长。补填日期后可归入该日的统计。</p>
      {entries(data.unassignedTimers)}
    </details>}
  </>;
}

export function StudyReport({ working }: { working: boolean }) {
  const [params, setParams] = useSearchParams();
  const requested = params.get('period');
  const period = requested === 'day' || requested === 'month' ? requested : 'week';
  const anchor = params.get('on');
  const query = new URLSearchParams({ period });
  if (anchor) query.set('on', anchor);
  const { value, error, refresh } = useRead<Report>(`${API}?${query}`);
  const [editing, setEditing] = useState<TimeRecord>();
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    function refreshVisible() {
      if (document.visibilityState === 'visible') refresh();
    }
    const timer = window.setInterval(refreshVisible, working ? 2200 : 45_000);
    document.addEventListener('visibilitychange', refreshVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', refreshVisible);
    };
  }, [working, refresh]);

  function select(nextPeriod: Period, on: string | null) {
    setSaved(false);
    setParams(on ? { period: nextPeriod, on } : { period: nextPeriod });
  }
  return <section className="page study-report">
    <div className="page-head"><div><h1>学习报告</h1><p>使用北京时间和已确认的有效计时。</p></div></div>
    <div className="study-report-controls">
      <div className="segmented" role="group" aria-label="报告周期">
        {periods.map(item => <button type="button" key={item} aria-pressed={item === period} onClick={() => select(item, anchor)}>
          {PERIODS[item]}
        </button>)}
      </div>
      {value && <div className="report-period">
        <button type="button" className="icon-button" aria-label="上一周期" disabled={!value.previous}
          onClick={() => select(period, value.previous)}><ChevronLeft size={19} /></button>
        <strong>{dateRange(value.from, value.through)}</strong>
        <button type="button" className="icon-button" aria-label="下一周期" disabled={!value.next}
          onClick={() => select(period, value.next)}><ChevronRight size={19} /></button>
        <label>选择日期 <input type="date" max={value.today} value={anchor ?? value.today}
          onChange={event => { if (event.target.value && event.target.validity.valid) select(period, event.target.value); }} /></label>
      </div>}
    </div>
    {value ? <>
      <ErrorNote message={error} retry={refresh} />
      {saved && <p className="notice success" role="status">计时校正已保存。</p>}
      <div className="report-summary">
        <section className="panel report-section"><h2>学习时长</h2><strong className="report-primary">{duration(value.totals.actualSeconds)}</strong>
          <dl className="report-breakdown">
            {categories.filter(category => category !== 'unclassified' || value.totals.byCategory[category] > 0).map(category => <div key={category}>
              <dt>{TIME_CATEGORIES[category]}</dt><dd>{duration(value.totals.byCategory[category])}</dd>
            </div>)}
          </dl>
        </section>
        <section className="panel report-section"><h2>已学知识点</h2><strong className="report-primary">{value.totals.learned}</strong>
          {!!value.learned.length && <details className="report-learned"><summary>查看已学知识点</summary>
            <ul className="report-list">{value.learned.map(item => <li key={item.lessonId}>
              <div><Link className="text-action" to={courseUrl(item.lessonId)}>{item.title}</Link>
                <small>{item.projectTopic} · {dateLabel(item.date)}</small></div>
            </li>)}</ul>
          </details>}
        </section>
        <section className="panel report-section"><h2>正式复习表现</h2><ReviewSummary counts={value.totals.review} /></section>
      </div>
      <div className="report-columns"><Trend data={value.trend} /><Comparison data={value} /></div>
      <section className="panel report-section">
        <h2>薄弱知识点</h2>
        {value.weak.length ? <ul className="report-list">{value.weak.map(item => <li key={`${item.lessonId}/${item.key}`}>
          <div><strong>{item.title}</strong><small>{item.projectTopic} · {dateLabel(item.on)} · {OUTCOMES[item.outcome]}</small></div>
          <div className="report-actions">
            <Link className="text-action" to={courseUrl(item.lessonId)} aria-label={`复习课程 ${item.title}`}>回到课程 <ArrowRight size={15} /></Link>
            {item.groupId && <Link className="text-action" to={`${reviewUrl({ kind: 'model', lessonId: item.lessonId, key: item.key })}?group=${encodeURIComponent(item.groupId)}`}>查看评价与补学</Link>}
          </div>
        </li>)}</ul> : <Empty title="暂无薄弱知识点" />}
      </section>
      <section className="panel report-section">
        <h2>课程完成预估</h2>
        <p className="report-hint">按当前可用时段和已知复习安排，向后估计 {value.predictionDays} 天。</p>
        {value.predictions.length ? <ul className="report-list">{value.predictions.map(item => <li key={item.projectId}>
          <div><Link className="text-action" to={projectUrl(item.projectId)}>{item.projectTopic}</Link>
            <small>剩余 {item.left} 个知识点{item.notReady > 0 && ` · ${item.notReady} 节课程待就绪`}</small></div>
          <span>{item.expectedOn ? `${dateLabel(item.expectedOn)} · ${item.timerEstimate ? '参考历史计时' : '使用默认估时'}`
            : item.notReady > 0 ? '等待课程准备' : '当前预测期内排期不足'}</span>
        </li>)}</ul> : <Empty title="暂无进行中的课程" />}
      </section>
      <TimerRecords data={value} edit={entry => { setSaved(false); setEditing(entry); }} />
      {editing && <TimerEditor entry={editing} today={value.today} close={() => setEditing(undefined)}
        onSaved={() => { setEditing(undefined); setSaved(true); refresh(); }} />}
    </> : <BackedStatus error={error} retry={refresh} />}
  </section>;
}
