import { useEffect, useRef, useState, type FormEvent, type RefObject } from 'react';
import { ArrowRight, ChevronLeft, ChevronRight } from 'lucide-react';
import { Link, useSearchParams } from 'react-router-dom';
import { api, courseUrl, dateLabel, projectUrl, reviewUrl, type TimeCategory } from './api';
import { BackedStatus, Empty, ErrorNote, Modal, useAction, useRead } from './shared';
import { LOCK_STATUS, PLAN, type LockStatus } from './study-plan-move';
import { TIME_CATEGORIES } from './study-timer';

type Period = 'day' | 'week' | 'month';
type Outcome = 'passed' | 'hinted' | 'failed' | 'ungraded';
type TimeRecord = {
  timerId: string; lessonId: string; lessonTitle: string; projectTopic: string;
  sourceOn: string | null; reportOn: string | null; recordedSeconds: number; seconds: number;
  recordedCategory: TimeCategory; category: TimeCategory; revision: number; reviewKey: string | null;
  reviewTitle: string | null; reviewKind: 'local' | 'model' | null;
};
type ReviewRecord = {
  lessonId: string; lessonTitle: string; projectTopic: string; title: string | null;
  key: string; round: number; on: string; outcome: Outcome; groupId: string | null;
};
type Efficiency = { trackedLessons: number; secondsPerLesson: number | null };
type Report = {
  today: string; from: string; through: string; previous: string | null; next: string | null;
  totals: {
    actualSeconds: number; learned: number; byCategory: Record<TimeCategory, number>; review: Record<Outcome, number>;
    plannedSeconds: number | null; recordedPlanDays: number; planBudgetSeconds: number | null; actualOnPlannedDaysSeconds: number | null;
    planDifferenceSeconds: number | null; periodDays: number; actualWithoutPlanSeconds: number;
  };
  trend: { date: string; actualSeconds: number; plannedSeconds: number | null; learned: number }[];
  learned: { lessonId: string; title: string; projectTopic: string; date: string }[];
  reviewRecords: ReviewRecord[];
  weak: { lessonId: string; key: string; title: string; projectTopic: string; on: string; outcome: Outcome; groupId: string | null; sourceRound: number }[];
  efficiency: Efficiency; previousEfficiency: (Efficiency & { from: string; through: string }) | null;
  predictions: {
    projectId: string; projectTopic: string; left: number; notReady: number; expectedOn: string | null; timerEstimate: boolean;
    pendingLessonLocks: { id: string; title: string; status: LockStatus; date: string; start: string; end: string }[];
  }[];
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

const RECORD_PAGE_SIZE = 20;

function ReviewSummary({ counts, select }: { counts: Record<Outcome, number>; select: (outcome: Outcome) => void }) {
  return <dl className="report-breakdown">
    {outcomes.map(outcome => <div key={outcome}>
      <dt><button type="button" className="text-action" aria-controls="report-review-details"
        aria-label={'查看' + OUTCOMES[outcome] + '的正式复习记录'} onClick={() => select(outcome)}>{OUTCOMES[outcome]}</button></dt>
      <dd>{counts[outcome]}</dd>
    </div>)}
  </dl>;
}

function ReviewRecordList({ items, reportQuery }: { items: ReviewRecord[]; reportQuery: string }) {
  const [limit, setLimit] = useState(RECORD_PAGE_SIZE);
  return <>
    <ul className="report-list">{items.slice(0, limit).map(item => <li
      key={item.groupId ?? item.lessonId + '/' + item.key + '/' + item.round}>
      <div>
        {item.title && <strong>{item.title}</strong>}
        <small><Link className="text-action" to={courseUrl(item.lessonId)}>{item.lessonTitle}</Link> · {item.projectTopic}</small>
        <small>{dateLabel(item.on)} · 第 {item.round} 轮 · {OUTCOMES[item.outcome]}</small>
      </div>
      {item.groupId && item.title && <Link className="text-action"
        to={reviewUrl({ kind: 'model', lessonId: item.lessonId, key: item.key }) + '?' +
          new URLSearchParams({ group: item.groupId, returnReport: reportQuery })}>
        查看当轮评价
      </Link>}
    </li>)}</ul>
    {limit < items.length && <button type="button" className="button secondary report-record-more"
      onClick={() => setLimit(count => count + RECORD_PAGE_SIZE)}>显示更多复习记录</button>}
  </>;
}

function ReviewRecords({ data, outcome, select, details, input, params }: {
  data: Report; outcome: Outcome | ''; select: (outcome: Outcome | '') => void; params: URLSearchParams;
  details: RefObject<HTMLDetailsElement | null>; input: RefObject<HTMLSelectElement | null>;
}) {
  const items = outcome ? data.reviewRecords.filter(item => item.outcome === outcome) : data.reviewRecords;
  const showRecords = Boolean(outcome || params.get('reviewOutcome') === 'all');
  const reportParams = new URLSearchParams(params);
  reportParams.set('reviewOutcome', outcome || 'all');
  useEffect(() => {
    if (showRecords && details.current) details.current.open = true;
  }, [showRecords, details]);
  return <section className="panel report-section report-formal-records" aria-label="正式复习记录">
    <details ref={details} id="report-review-details">
      <summary>正式复习记录（{items.length}）</summary>
      <div className="report-correction-fields report-review-filter">
        <label>复习结果<select ref={input} value={outcome} onChange={event => select(event.target.value as Outcome | '')}>
          <option value="">全部结果</option>
          {outcomes.map(item => <option key={item} value={item}>{OUTCOMES[item]}</option>)}
        </select></label>
      </div>
      {items.length ? <ReviewRecordList key={data.from + '/' + data.through + '/' + outcome}
        items={items} reportQuery={reportParams.toString()} />
        : <p className="report-hint">{outcome ? '本周期没有符合筛选条件的正式复习记录。' : '本周期还没有正式复习记录。'}</p>}
    </details>
  </section>;
}

function periodParams(params: URLSearchParams, period: Period, on: string | null) {
  const next = new URLSearchParams(params);
  next.set('period', period);
  if (on) next.set('on', on);
  else next.delete('on');
  return next;
}

function Trend({ data, selectedDay, params, onNavigate }: {
  data: Report['trend']; selectedDay?: string; params: URLSearchParams; onNavigate: () => void;
}) {
  const largest = Math.max(60, ...data.map(row => Math.max(row.actualSeconds, row.plannedSeconds ?? 0)));
  return <section className="panel report-section">
    <h2>学习趋势</h2>
    <div className="report-trend-table">
      <table>
        <thead><tr><th scope="col">日期</th><th scope="col">确认时长</th><th scope="col">计划估时</th><th scope="col">已学知识点</th></tr></thead>
        <tbody>{data.map(row => <tr key={row.date}>
          <th scope="row"><time dateTime={row.date}>
            {row.date === selectedDay ? dateLabel(row.date) : <Link to={`/study-report?${periodParams(params, 'day', row.date)}`}
              className="text-action" aria-label={`查看 ${dateLabel(row.date)} 日报`} onClick={onNavigate}>{dateLabel(row.date)}</Link>}
          </time></th>
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
  const planned = totals.plannedSeconds;
  const reading = efficiency.secondsPerLesson;
  const earlier = previousEfficiency?.secondsPerLesson ?? null;
  const difference = reading !== null && earlier !== null ? reading - earlier : null;
  return <section className="panel report-section">
    <h2>计划与实际</h2>
    <p className="report-hint">所选日期中 {totals.recordedPlanDays} / {totals.periodDays} 天有计划记录。</p>
    {planned === null && <p className="report-hint">这个时段还没有保存的计划估时。</p>}
    {(planned !== null || totals.actualWithoutPlanSeconds > 0) && <dl className="report-breakdown">
      {planned !== null && <>
        <div><dt>可用时间预算</dt><dd>{duration(totals.planBudgetSeconds ?? 0)}</dd></div>
        <div><dt>计划估时</dt><dd>{duration(planned)}</dd></div>
        <div><dt>有计划日期的确认时长</dt><dd>{duration(totals.actualOnPlannedDaysSeconds ?? 0)}</dd></div>
        <div><dt>实际与计划差额</dt><dd>{!totals.planDifferenceSeconds ? '0 分钟'
          : `${totals.planDifferenceSeconds > 0 ? '多 ' : '少 '}${duration(Math.abs(totals.planDifferenceSeconds))}`}</dd></div>
      </>}
      {totals.actualWithoutPlanSeconds > 0 && <div>
        <dt>无计划记录日期的确认时长</dt><dd>{duration(totals.actualWithoutPlanSeconds)}</dd>
      </div>}
    </dl>}
    <h3>平均阅读时长</h3>
    {reading === null ? <p className="report-hint">当前时段暂无已学且有确认阅读计时的知识点。</p> : <dl className="report-breakdown">
      <div><dt>本周期 · {efficiency.trackedLessons} 个知识点</dt><dd>{duration(reading)}</dd></div>
      {previousEfficiency && <div>
        <dt>上一周期（{dateRange(previousEfficiency.from, previousEfficiency.through)}） · {previousEfficiency.trackedLessons} 个知识点</dt>
        <dd>{earlier === null ? '暂无可比计时' : duration(earlier)}</dd>
      </div>}
      {difference !== null && <div>
        <dt>较上一周期</dt>
        <dd>{difference === 0 ? '持平' : `${difference < 0 ? '缩短' : '增加'} ${duration(Math.abs(difference))}`}</dd>
      </div>}
    </dl>}
  </section>;
}

type CorrectionVersion = {
  revision: number; reportOn: string | null; seconds: number; category: TimeCategory; savedAt: string | null;
};

const correctionStamp = new Intl.DateTimeFormat('zh-CN', {
  dateStyle: 'short', timeStyle: 'medium', timeZone: 'Asia/Shanghai',
});

function CorrectionHistory({ entry, load, busy }: {
  entry: TimeRecord; load: (version: CorrectionVersion, baseRevision: number) => void; busy: boolean;
}) {
  const path = API + '/timers/' + entry.timerId + '/corrections';
  const { value, error, refresh } = useRead<{ versions: CorrectionVersion[] }>(entry.sourceOn ? path + '?sourceOn=' + entry.sourceOn : path);
  const [selected, setSelected] = useState<number>();
  if (!value) return <BackedStatus error={error} retry={refresh} />;
  const latest = value.versions[0];
  const version = value.versions.find(item => item.revision === selected) ?? latest;
  const name = (item: CorrectionVersion) => item.revision === 0 ? '原始记录' : '第 ' + item.revision + ' 版';
  return <fieldset className="report-correction-fields" disabled={busy}>
    <label>校正版本 <select value={version.revision} onChange={event => setSelected(Number(event.target.value))}>
      {value.versions.map(item => <option key={item.revision} value={item.revision}>
        {name(item)}{item.revision === latest.revision ? '（当前生效）' : ''}
      </option>)}
    </select></label>
    <div aria-live="polite">
      <p className="dialog-hint">{version.reportOn ? dateLabel(version.reportOn) : '日期待补录'} · {TIME_CATEGORIES[version.category]} · {duration(version.seconds)}</p>
      {version.savedAt && <p className="dialog-hint"><time dateTime={version.savedAt}>{correctionStamp.format(new Date(version.savedAt))}</time></p>}
    </div>
    <ErrorNote message={error} retry={refresh} />
    <button type="button" className="button secondary" onClick={() => load(version, latest.revision)}>载入此版本</button>
  </fieldset>;
}

function TimerEditor({ entry, today, close, onSaved }: {
  entry: TimeRecord; today: string; close: () => void; onSaved: () => void;
}) {
  const [minutes, setMinutes] = useState(String(Math.floor(entry.seconds / 60)));
  const [seconds, setSeconds] = useState(String(entry.seconds % 60));
  const [day, setDay] = useState(entry.reportOn ?? '');
  const [category, setCategory] = useState(entry.category);
  const [baseRevision, setBaseRevision] = useState(entry.revision);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [loaded, setLoaded] = useState<number>();
  const minuteField = useRef<HTMLInputElement>(null);
  const action = useAction();

  function loadVersion(version: CorrectionVersion, current: number) {
    setMinutes(String(Math.floor(version.seconds / 60)));
    setSeconds(String(version.seconds % 60));
    setDay(version.reportOn ?? '');
    setCategory(version.category);
    setBaseRevision(current);
    setLoaded(version.revision);
    setHistoryOpen(false);
    minuteField.current?.focus();
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    const result = await action.run(() => api(`${API}/timers/${entry.timerId}/correction`, 'PUT', {
      sourceOn: entry.sourceOn, reportOn: entry.sourceOn ?? (day || null), category,
      seconds: Number(minutes) * 60 + Number(seconds), baseRevision,
    }));
    if (result) onSaved();
  }
  return <Modal title="校正计时" onClose={close} busy={action.pending}>
    <form className="project-dialog-form" onSubmit={save}>
      <div><strong>{entry.lessonTitle}</strong><p className="dialog-hint">{entry.projectTopic} · 原始计时 {duration(entry.recordedSeconds)} · {TIME_CATEGORIES[entry.recordedCategory]}
        {entry.sourceOn && ` · ${dateLabel(entry.sourceOn)}`}</p></div>
      <fieldset className="report-correction-fields" disabled={action.pending}>
        <label>有效分钟<input ref={minuteField} type="number" min="0" step="1" required value={minutes} onChange={event => setMinutes(event.target.value)} /></label>
        <label>有效秒数<input type="number" min="0" max="59" step="1" required value={seconds} onChange={event => setSeconds(event.target.value)} /></label>
        <label>活动类别<select value={category} onChange={event => setCategory(event.target.value as TimeCategory)}>
          {categories.map(item => <option key={item} value={item}>{TIME_CATEGORIES[item]}</option>)}
        </select></label>
        {entry.sourceOn === null && <label>归入日期<input type="date" value={day} max={today} onChange={event => setDay(event.target.value)} /></label>}
      </fieldset>
      <div><button type="button" className="text-action" aria-expanded={historyOpen} disabled={action.pending}
        onClick={() => setHistoryOpen(open => !open)}>{historyOpen ? '收起校正历史' : '查看校正历史'}</button></div>
      {historyOpen && <CorrectionHistory entry={entry} load={loadVersion} busy={action.pending} />}
      {loaded !== undefined && <p className="dialog-hint" role="status">
        已载入{loaded === 0 ? '原始记录' : '第 ' + loaded + ' 版'}，请复核后保存。
      </p>}
      <ErrorNote message={action.error} />
      <div className="project-dialog-actions">
        <button type="button" className="button secondary" disabled={action.pending} onClick={close}>取消</button>
        <button className="button primary" disabled={action.pending}>{action.pending ? '正在保存' : '保存校正'}</button>
      </div>
    </form>
  </Modal>;
}

type TimeRecordFilters = { activity: TimeCategory | ''; query: string };
function TimeRecordList({ items, edit }: { items: TimeRecord[]; edit: (entry: TimeRecord) => void }) {
  const [limit, setLimit] = useState(RECORD_PAGE_SIZE);
  return <>
    <ul className="report-list">{items.slice(0, limit).map(item => <li key={item.timerId + '/' + item.sourceOn}>
      <div><Link className="text-action" to={courseUrl(item.lessonId)}>{item.lessonTitle}</Link>
        <small>{item.projectTopic} · {item.reportOn ? dateLabel(item.reportOn) : '日期待补录'} · {TIME_CATEGORIES[item.category]}</small>
        {item.reviewKey && item.reviewKind && <small><Link className="text-action"
          to={reviewUrl({ kind: item.reviewKind, lessonId: item.lessonId, key: item.reviewKey })}>{item.reviewTitle}</Link></small>}
        <small>{duration(item.seconds)}{item.revision > 0 && ' · 原始 ' + duration(item.recordedSeconds)}</small>
      </div>
      <button type="button" className="text-action" aria-label={'校正 ' + item.lessonTitle + ' ' + (item.reportOn ?? '旧计时')}
        onClick={() => edit(item)}>校正</button>
    </li>)}</ul>
    {limit < items.length && <button type="button" className="button secondary report-record-more"
      onClick={() => setLimit(count => count + RECORD_PAGE_SIZE)}>显示更多计时记录</button>}
  </>;
}

function TimerRecords({ data, edit, filters, update, confirmedRef, activityRef }: {
  data: Report; edit: (entry: TimeRecord) => void;
  filters: TimeRecordFilters; update: (filters: TimeRecordFilters) => void;
  confirmedRef: RefObject<HTMLDetailsElement | null>; activityRef: RefObject<HTMLSelectElement | null>;
}) {
  const legacy = useRef<HTMLDetailsElement>(null);
  const { activity, query } = filters;
  const keyword = query.trim().toLowerCase();
  const filtering = Boolean(activity || keyword);
  const matches = (item: TimeRecord) => (!activity || item.category === activity)
    && (!keyword || item.lessonTitle.toLowerCase().includes(keyword) || item.projectTopic.toLowerCase().includes(keyword));
  const confirmed = data.timeRecords.filter(matches);
  const unassigned = data.unassignedTimers.filter(matches);
  const listKey = activity + '/' + keyword;
  useEffect(() => {
    if (!activity && !keyword) return;
    if (confirmedRef.current) confirmedRef.current.open = true;
    if (legacy.current) legacy.current.open = true;
  }, [activity, keyword, confirmedRef]);

  return <section className="panel report-section report-timer-records" aria-labelledby="report-timer-heading">
    <h2 id="report-timer-heading">计时记录</h2>
    <div className="report-correction-fields report-timer-filters">
      <label>活动类别<select ref={activityRef} value={activity}
        onChange={event => update({ ...filters, activity: event.target.value as TimeRecordFilters['activity'] })}>
        <option value="">全部活动</option>
        {categories.map(item => <option key={item} value={item}>{TIME_CATEGORIES[item]}</option>)}
      </select></label>
      <label>搜索课程或项目<input type="search" value={query} onChange={event => update({ ...filters, query: event.target.value })} /></label>
      {filtering && <button type="button" className="text-action" onClick={() => update({ activity: '', query: '' })}>清除筛选</button>}
    </div>
    <details ref={confirmedRef}>
      <summary>已确认计时记录（{confirmed.length}）</summary>
      {confirmed.length ? <TimeRecordList key={listKey} items={confirmed} edit={edit} />
        : <p className="report-hint">{filtering ? '没有符合筛选条件的计时记录。' : '这个时段暂无计时记录。'}</p>}
    </details>
    {!!data.unassignedTimers.length && <details ref={legacy}>
      <summary>日期待补录的旧计时（{unassigned.length}）</summary>
      {unassigned.length ? <>
        <p className="report-hint">旧版记录保存了总时长。补填日期后可归入该日的统计。</p>
        <TimeRecordList key={listKey} items={unassigned} edit={edit} />
      </> : <p className="report-hint">没有符合筛选条件的旧计时。</p>}
    </details>}
  </section>;
}

export function StudyReport({ working }: { working: boolean }) {
  const [params, setParams] = useSearchParams();
  const requested = params.get('period');
  const period = requested === 'day' || requested === 'month' ? requested : 'week';
  const anchor = params.get('on');
  const filters: TimeRecordFilters = {
    activity: categories.find(item => item === params.get('activity')) ?? '',
    query: params.get('timerQuery') ?? '',
  };
  const selectedOutcome = outcomes.find(item => item === params.get('reviewOutcome')) ?? '';
  const reviewRecords = useRef<HTMLDetailsElement>(null);
  const reviewOutcome = useRef<HTMLSelectElement>(null);
  const confirmedRecords = useRef<HTMLDetailsElement>(null);
  const recordActivity = useRef<HTMLSelectElement>(null);
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
    setParams(periodParams(params, nextPeriod, on));
  }

  function updateReviewOutcome(outcome: Outcome | '', replace = true) {
    const next = new URLSearchParams(params);
    next.set('reviewOutcome', outcome || 'all');
    setParams(next, { replace });
  }

  function showReviewOutcome(outcome: Outcome) {
    updateReviewOutcome(outcome, false);
    if (reviewRecords.current) reviewRecords.current.open = true;
    reviewOutcome.current?.focus();
  }

  function updateTimerFilters(next: TimeRecordFilters, replace = true) {
    const updated = new URLSearchParams(params);
    if (next.activity) updated.set('activity', next.activity);
    else updated.delete('activity');
    if (next.query) updated.set('timerQuery', next.query);
    else updated.delete('timerQuery');
    setParams(updated, { replace });
  }

  function showActivity(activity: TimeCategory) {
    updateTimerFilters({ activity, query: '' }, false);
    if (confirmedRecords.current) confirmedRecords.current.open = true;
    recordActivity.current?.focus();
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
              <dt><button type="button" className="text-action" aria-label={'查看' + TIME_CATEGORIES[category] + '计时记录'}
                onClick={() => showActivity(category)}>{TIME_CATEGORIES[category]}</button></dt>
              <dd>{duration(value.totals.byCategory[category])}</dd>
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
        <section className="panel report-section"><h2>正式复习表现</h2>
          <ReviewSummary counts={value.totals.review} select={showReviewOutcome} />
        </section>
      </div>
      <ReviewRecords data={value} outcome={selectedOutcome} select={updateReviewOutcome}
        details={reviewRecords} input={reviewOutcome} params={params} />
      <div className="report-columns">
        <Trend data={value.trend} selectedDay={period === 'day' ? value.from : undefined} params={params} onNavigate={() => setSaved(false)} />
        <Comparison data={value} />
      </div>
      <section className="panel report-section">
        <h2>薄弱知识点</h2>
        {value.weak.length ? <ul className="report-list">{value.weak.map(item => {
          const practice = new URLSearchParams();
          if (requested === 'day' || requested === 'week' || requested === 'month') practice.set('period', requested);
          if (anchor) practice.set('on', anchor);
          if (item.groupId) practice.set('focus', item.groupId);
          else practice.set('practice', String(item.sourceRound));
          const target = reviewUrl({ kind: item.groupId ? 'model' : 'local', lessonId: item.lessonId, key: item.key });
          return <li key={`${item.lessonId}/${item.key}`}>
          <div><strong>{item.title}</strong><small>{item.projectTopic} · {dateLabel(item.on)} · {OUTCOMES[item.outcome]}</small></div>
          <div className="report-actions">
            <Link className="text-action" to={courseUrl(item.lessonId)} aria-label={`复习课程 ${item.title}`}>回到课程 <ArrowRight size={15} /></Link>
            <Link className="text-action" to={`${target}?${practice}`} aria-label={`专项练习 ${item.title}`}>专项练习</Link>
            {item.groupId && <Link className="text-action" to={`${target}?group=${encodeURIComponent(item.groupId)}`}>查看评价与补学</Link>}
          </div>
        </li>; })}</ul> : <Empty title="暂无薄弱知识点" />}
      </section>
      <section className="panel report-section">
        <h2>课程完成预估</h2>
        <p className="report-hint">按当前可用时段和已知复习安排，向后估计 {value.predictionDays} 天。</p>
        {value.predictions.length ? <ul className="report-list">{value.predictions.map(item => {
          const needsAction = item.pendingLessonLocks.some(lock => lock.status !== 'future');
          const waiting = needsAction ? '先处理锁定任务' : item.notReady > 0 ? '等待课程准备'
            : item.pendingLessonLocks.length ? '已锁定在预测期后' : '当前预测期内排期不足';
          return <li key={item.projectId}>
            <div><Link className="text-action" to={projectUrl(item.projectId)}>{item.projectTopic}</Link>
              <small>剩余 {item.left} 个知识点{item.notReady > 0 && ` · ${item.notReady} 节课程待就绪`}</small>
              {item.pendingLessonLocks.map(lock => <small key={lock.id}>
                {lock.title} · {dateLabel(lock.date)} · {lock.start}–{lock.end} · {lock.status === 'future' ? '超出预测期' : LOCK_STATUS[lock.status]}
                {' · '}<Link className="text-action" to={PLAN + '?lock=' + encodeURIComponent(lock.id)}
                  aria-label={`查看预约 ${lock.title}`}>查看预约</Link>
              </small>)}
            </div>
            <span>{item.expectedOn ? `${dateLabel(item.expectedOn)} · ${item.timerEstimate ? '参考历史计时' : '使用默认估时'}` : waiting}</span>
          </li>;
        })}</ul> : <Empty title="暂无进行中的课程" />}
      </section>
      <TimerRecords data={value} edit={entry => { setSaved(false); setEditing(entry); }} filters={filters} update={updateTimerFilters}
        confirmedRef={confirmedRecords} activityRef={recordActivity} />
      {editing && <TimerEditor entry={editing} today={value.today} close={() => setEditing(undefined)}
        onSaved={() => { setEditing(undefined); setSaved(true); refresh(); }} />}
    </> : <BackedStatus error={error} retry={refresh} />}
  </section>;
}
