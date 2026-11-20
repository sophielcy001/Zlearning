import { useEffect, useState, type DragEvent, type FormEvent } from 'react';
import { ArrowRight, CalendarClock, Clock3, GripVertical, LockKeyhole, Plus, SlidersHorizontal, X } from 'lucide-react';
import { Link } from 'react-router-dom';
import { api, courseUrl, dateLabel, reviewUrl } from './api';
import { refreshReminders } from './reminders';
import { BackedStatus, ErrorNote, Modal, useAction, useRead } from './shared';
import { dayLabel, MoveEditor, PLAN, suggestedStart, WEEKDAYS, type MoveDraft, type MoveTask, type Slot, type TimeWindow } from './study-plan-move';

type StudyTask = MoveTask & Slot & { lessonId: string; estimateSource: 'timer' | 'default' }
  & ({ kind: 'lesson' } | { kind: 'review'; reviewKind: 'local' | 'model'; key: string; dueOn: string; weak: boolean });
type PendingLock = MoveTask & Slot & { status: keyof typeof LOCK_STATUS };
type UnplannedTask = MoveTask & { earliest: string };
type Day = {
  date: string; windows: TimeWindow[]; override: boolean; tasks: StudyTask[];
  budgetMinutes: number; plannedMinutes: number; estimatedCompletedMinutes: number; remainingMinutes: number;
};
type Plan = {
  today: string; weekly: TimeWindow[][]; configured: boolean; days: Day[];
  unplannedReviews: number; unplannedLessons: number; unplanned: UnplannedTask[]; pendingLocks: PendingLock[];
  courseUpdates: { id: string; lessonId: string; title: string; projectTopic: string; version: number }[];
};

type SourceUpdateTask = { lessonId: string; title: string; projectTopic: string; sectionCount: number };

const DRAG_TASK = 'application/x-zhixue-study-task';
const LOCK_STATUS = {
  future: '已排在未来日期',
  missed: '已过计划日期',
  paused: '项目已暂停',
  before_due: '原到期日已变化',
  unavailable: '暂不可安排',
  outside_hours: '时段需调整',
};
const START_PATTERN = '(?:[01][0-9]|2[0-3]):[0-5][0-9]';
const END_PATTERN = `(?:${START_PATTERN}|24:00)`;

function LockTag() {
  return <span className="tag study-tag locked"><LockKeyhole size={12} aria-hidden="true" />已锁定</span>;
}

function DayCard({ day, today, openDay, editDay, editMove, dropTask, highlight, onDrag }: {
  day: Day; today: string; openDay?: () => void; editDay: () => void;
  editMove: (draft: MoveDraft) => void; dropTask: (id: string, start?: string) => void;
  highlight: boolean; onDrag: (dragging: boolean, over?: string) => void;
}) {
  function dragOver(event: DragEvent) {
    if (!event.dataTransfer.types.includes(DRAG_TASK)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    onDrag(true, day.date);
  }
  function drop(event: DragEvent, start?: string) {
    const task = event.dataTransfer.getData(DRAG_TASK);
    if (!task) return;
    event.preventDefault();
    event.stopPropagation();
    onDrag(false);
    dropTask(task, start);
  }
  const overBudget = day.estimatedCompletedMinutes + day.plannedMinutes - day.budgetMinutes;
  return <article className={`panel study-day${highlight ? ' study-drop-target' : ''}`} aria-label={dayLabel(day.date)}
    onDragOver={dragOver} onDrop={event => drop(event)}>
    <header className="study-day-heading">
      <div>
        <h2><time dateTime={day.date}>{dayLabel(day.date)}</time>{day.date === today && <span className="tag">今天</span>}</h2>
        <p>{day.windows.length ? day.windows.map(window => `${window.start}–${window.end}`).join(' · ') : '没有学习时段'}
          {day.override && <span className="study-override">当日调整</span>}</p>
        <p>预算 {day.budgetMinutes} 分钟 · 待学 {day.plannedMinutes} 分钟
          {day.estimatedCompletedMinutes > 0 && ` · 已学估计 ${day.estimatedCompletedMinutes} 分钟`}</p>
        {overBudget > 0
          ? <p>已超预算 {overBudget} 分钟</p>
          : day.budgetMinutes > 0 && <p>可用 ${day.remainingMinutes} 分钟</p>}
      </div>
      <div className="study-day-actions">
        {openDay && <button type="button" className="text-action" onClick={openDay}>查看当天</button>}
        <button type="button" className="text-action" onClick={editDay}>调整当天时段</button>
      </div>
    </header>
    {day.tasks.length ? <ul className="study-tasks">
      {day.tasks.map(task => {
        const url = task.kind === 'lesson' ? courseUrl(task.lessonId) : reviewUrl({ ...task, kind: task.reviewKind });
        const target = { date: day.date, start: task.start };
        return <li className="study-task" key={task.id} onDrop={event => drop(event, task.start)}>
          <span className="study-time">{task.start}–{task.end}</span>
          <div className="study-task-main">
            <div className="study-task-title">
              <span className={`tag study-tag ${task.kind}`}>{task.kind === 'review' ? '复习' : '新课'}</span>
              {task.kind === 'review' && task.weak && <span className="tag study-tag weak">薄弱点</span>}
              {task.locked && <LockTag />}
              <Link to={url}>{task.title}<ArrowRight size={14} aria-hidden="true" /></Link>
            </div>
            <small>{task.projectTopic} · 预计 {task.minutes} 分钟 · {task.estimateSource === 'timer' ? '参考历史计时' : '默认估时'}
              {task.kind === 'review' && ` · 原到期日 ${dateLabel(task.dueOn)}`}</small>
          </div>
          <div className="study-task-actions">
            <button type="button" className="text-action study-move-handle" draggable title="拖动或点击改期"
              aria-label={`改期 ${task.title}`} onClick={() => editMove({ task, target })}
              onDragStart={event => {
                event.dataTransfer.setData(DRAG_TASK, task.id);
                event.dataTransfer.effectAllowed = 'move';
                onDrag(true);
              }} onDragEnd={() => onDrag(false)}>
              <GripVertical size={16} />改期
            </button>
            {task.locked && <button type="button" className="text-action" aria-label={`解除锁定 ${task.title}`}
              onClick={() => editMove({ task, target, release: true })}>解除锁定</button>}
          </div>
        </li>;
      })}
    </ul> : <p className="study-day-empty">当天没有待学任务</p>}
  </article>;
}

function WindowFields({ label, windows, onChange }: {
  label: string; windows: TimeWindow[]; onChange: (windows: TimeWindow[]) => void;
}) {
  function update(index: number, field: keyof TimeWindow, text: string) {
    onChange(windows.map((window, current) => current === index ? { ...window, [field]: text } : window));
  }
  return <fieldset className="study-windows">
    <legend>{label}</legend>
    {windows.map((window, index) => <div className="study-window-inputs" key={index}>
      <input aria-label={`${label}第 ${index + 1} 段开始时间`} required placeholder="开始 HH:MM" maxLength={5}
        pattern={START_PATTERN} value={window.start} onChange={event => update(index, 'start', event.target.value)} />
      <span>至</span>
      <input aria-label={`${label}第 ${index + 1} 段结束时间`} required placeholder="结束 HH:MM" maxLength={5}
        pattern={END_PATTERN} value={window.end} onChange={event => update(index, 'end', event.target.value)} />
      <button type="button" className="icon-button" aria-label={`移除${label}第 ${index + 1} 段`}
        onClick={() => onChange(windows.filter((_, current) => current !== index))}><X size={16} /></button>
    </div>)}
    {!windows.length && <span className="study-no-window">休息日</span>}
    <button type="button" className="text-action" onClick={() => onChange([...windows, { start: '', end: '' }])}>
      <Plus size={15} />添加{label}时段
    </button>
  </fieldset>;
}

function TimeEditor({ weekly, day, close, onSaved }: {
  weekly: TimeWindow[][]; day: Day | null; close: () => void; onSaved: () => void;
}) {
  const [windows, setWindows] = useState(day ? [day.windows] : weekly);
  const action = useAction();
  const title = day ? '调整当天时段' : '每周学习时间';
  const labels = day ? [dayLabel(day.date)] : WEEKDAYS;

  async function save(useWeekly = false) {
    const result = await action.run(() => day
      ? api(`${PLAN}/days/${day.date}`, 'PUT', { windows: useWeekly ? null : windows[0] })
      : api(`${PLAN}/settings`, 'PUT', { weekly: windows }));
    if (result) onSaved();
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    void save();
  }

  return <Modal title={title} onClose={close} busy={action.pending}>
    <form className="project-dialog-form study-editor" onSubmit={submit}>
      <p className="dialog-hint">{day ? '以下时段仅用于这一天。' : '常规时段适用于没有单独调整的日期。'}
        使用北京时间和 24 小时制。当天结束于午夜时可填写 24:00。</p>
      <fieldset className="study-editor-fields" disabled={action.pending}>
        {labels.map((label, index) => <WindowFields key={label} label={label} windows={windows[index]} onChange={value =>
          setWindows(previous => previous.map((row, current) => current === index ? value : row))} />)}
      </fieldset>
      <ErrorNote message={action.error} />
      <div className="project-dialog-actions">
        {day?.override && <button type="button" className="text-action clear-draft" disabled={action.pending}
          onClick={() => void save(true)}>恢复每周时段</button>}
        <button type="button" className="button secondary" onClick={close} disabled={action.pending}>取消</button>
        <button className="button primary" disabled={action.pending}>{action.pending ? '正在保存' : '保存时段'}</button>
      </div>
    </form>
  </Modal>;
}

export function DailyPlan({ working }: { working: boolean }) {
  const { value, error, refresh } = useRead<Plan>(PLAN);
  const { value: sourceUpdates, error: sourceError, refresh: refreshSources } = useRead<SourceUpdateTask[]>(PLAN + '/source-updates');
  const [view, setView] = useState<'week' | 'day'>('week');
  const [selectedDate, setSelectedDate] = useState<string>();
  const [editing, setEditing] = useState<{ day: Day | null }>();
  const [move, setMove] = useState<MoveDraft>();
  const [drag, setDrag] = useState<{ active: boolean; over?: string }>({ active: false });
  const [saved, setSaved] = useState('');

  function setDragState(active: boolean, over?: string) {
    setDrag(current => current.active === active && current.over === over ? current : { active, over });
  }
  function editMove(draft: MoveDraft) {
    setSaved('');
    setMove(draft);
  }
  function onDrop(id: string, day: Day, start?: string) {
    const task = value?.days.flatMap(item => item.tasks).find(item => item.id === id);
    if (!task) return;
    editMove({ task, target: { date: day.date, start: start ?? suggestedStart(day.windows, task.minutes, task.start) }, preview: true });
  }
  useEffect(() => {
    function refreshVisible() {
      if (document.visibilityState === 'visible') refresh();
    }
    const interval = window.setInterval(refreshVisible, working ? 2000 : 45_000);
    document.addEventListener('visibilitychange', refreshVisible);
    return () => {
      window.clearInterval(interval);
      document.removeEventListener('visibilitychange', refreshVisible);
    };
  }, [working, refresh]);

  useEffect(() => {
    function refreshVisible() {
      if (document.visibilityState === 'visible') refreshSources();
    }
    const interval = window.setInterval(refreshVisible, 45_000);
    window.addEventListener('focus', refreshVisible);
    document.addEventListener('visibilitychange', refreshVisible);
    return () => {
      window.clearInterval(interval);
      window.removeEventListener('focus', refreshVisible);
      document.removeEventListener('visibilitychange', refreshVisible);
    };
  }, [refreshSources]);

  const selected = value?.days.find(day => day.date === selectedDate) ?? value?.days[0];
  return <section className="page study-plan">
    <div className="page-head"><div><h1>今日任务</h1><p>按共用时间预算安排新课和到期复习。</p></div>
      {value && <button type="button" className="button secondary" onClick={() => { setSaved(''); setEditing({ day: null }); }}>
        <SlidersHorizontal size={16} />每周学习时间
      </button>}
    </div>
    {value && selected ? <>
      <ErrorNote message={error} retry={refresh} />
      <div className="study-plan-controls">
        <div role="group" className="segmented" aria-label="课程表视图">
          <button type="button" aria-pressed={view === 'week'} onClick={() => setView('week')}>周视图</button>
          <button type="button" aria-pressed={view === 'day'} onClick={() => setView('day')}>日视图</button>
        </div>
        {view === 'week' ? <span>未来 7 天 · 北京时间</span>
          : <label>日期 <select value={selected.date} onChange={event => setSelectedDate(event.target.value)}>
            {value.days.map(day => <option key={day.date} value={day.date}>{dayLabel(day.date)}</option>)}
          </select></label>}
      </div>
      {!value.configured && <p className="study-plan-note"><Clock3 size={16} /> 当前使用默认时段，每天 19:00–19:30。</p>}
      {saved && <p className="notice success" role="status">{saved}</p>}
      {value.courseUpdates.length > 0 && <section className="panel study-extra">
        <h2>待查看课程更新</h2>
        <ul>{value.courseUpdates.map(update => <li key={update.id}>
          <div><strong>{update.title}</strong><p>{update.projectTopic} · 第 {update.version} 版</p></div>
          <Link className="text-action" to={`${courseUrl(update.lessonId)}?courseUpdate=${update.id}`}>查看课程更新</Link>
        </li>)}</ul>
      </section>}
      <ErrorNote message={sourceError} retry={refreshSources} />
      {!!sourceUpdates?.length && <section className="panel study-extra">
        <h2>待核对的来源变更</h2>
        <ul>{sourceUpdates.map(update => <li key={update.lessonId}>
          <div><strong>{update.title}</strong><p>{update.projectTopic} · {update.sectionCount} 个小节受影响</p></div>
          <Link className="text-action" to={courseUrl(update.lessonId) + '?sourceUpdate=1'}>核对来源变更</Link>
        </li>)}</ul>
      </section>}
      <div className="study-days" data-dragging={drag.active}>
        {(view === 'week' ? value.days : [selected]).map(day => <DayCard key={day.date} day={day} today={value.today}
          openDay={view === 'week' ? () => { setSelectedDate(day.date); setView('day'); } : undefined}
          editDay={() => { setSaved(''); setEditing({ day }); }}
          editMove={editMove} dropTask={(id, start) => onDrop(id, day, start)}
          highlight={drag.over === day.date} onDrag={setDragState} />)}
      </div>
      {value.pendingLocks.length > 0 && <section className="panel study-extra">
        <h2>其他锁定任务</h2>
        <ul>{value.pendingLocks.map(task => {
          let date = task.date > value.today ? task.date : value.today;
          if (task.dueOn && task.dueOn > date) date = task.dueOn;
          const target = { date, start: task.start };
          return <li key={task.id}>
            <div><strong>{task.title}</strong><p>{task.projectTopic} · {dayLabel(task.date)} · {task.start}–{task.end}</p>
              <LockTag /> <small>{LOCK_STATUS[task.status]}</small></div>
            <div className="study-task-actions">
              {task.status !== 'paused' && <button type="button" className="text-action" aria-label={`改期 ${task.title}`}
                onClick={() => editMove({ task, target })}>改期</button>}
              <button type="button" className="text-action" aria-label={`解除锁定 ${task.title}`}
                onClick={() => editMove({ task, target, release: true })}>解除锁定</button>
            </div>
          </li>;
        })}</ul>
      </section>}
      {value.unplanned.length > 0 && <details className="panel study-extra">
        <summary>还有 {value.unplannedReviews} 项复习、{value.unplannedLessons} 节新课等候安排</summary>
        <ul>{value.unplanned.map(task => {
          const date = task.earliest > value.today ? task.earliest : value.today;
          const windows = value.days.find(day => day.date === date)?.windows ?? [];
          return <li key={task.id}>
            <div><strong>{task.title}</strong><p>{task.projectTopic} · 预计 {task.minutes} 分钟
              {task.dueOn && ` · 原到期日 ${dateLabel(task.dueOn)}`}</p></div>
            <button type="button" className="text-action" aria-label={`安排 ${task.title}`} onClick={() =>
              editMove({ task, target: { date, start: suggestedStart(windows, task.minutes) } })}>
              <CalendarClock size={16} />安排时间
            </button>
          </li>;
        })}</ul>
      </details>}
      {editing && <TimeEditor weekly={value.weekly} day={editing.day} close={() => setEditing(undefined)}
        onSaved={() => { setEditing(undefined); setSaved('学习时段已保存。'); refresh(); refreshReminders(); }} />}
      {move && <MoveEditor draft={move} today={value.today} projectedThrough={value.days[value.days.length - 1].date}
        close={() => setMove(undefined)} onSaved={() => {
          setMove(undefined);
          setSaved(move.release ? '已解除锁定。' : '排期已保存并锁定。');
          refresh();
          refreshReminders();
        }} />}
    </> : <BackedStatus error={error} retry={refresh} />}
  </section>;
}
