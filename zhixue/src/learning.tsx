import { lazy, Suspense, useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type DragEvent } from 'react';
import { ArrowLeft, ArrowRight, BookOpen, CalendarDays, Check, ChevronDown, CircleHelp, FilePenLine, Lightbulb, Maximize2, MessageCircle, Minimize2, NotebookPen } from 'lucide-react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { api, courseUrl, dateLabel, projectUrl, reviewUrl, type Lesson, type LessonDetail, type Review, type ReviewDetail, type ReviewList, type ReviewResult, type Timer } from './api';
import { abilityName } from './assessment-types';
import type { CourseSelection } from './conversation';
import { courseExcerptMime, type CourseExcerpt, type ExcerptRequest } from './course-excerpt';
import { LessonNotes, readNoteDraft } from './notes';
import type { PrerequisiteSelection } from './prerequisite';
import { courseHeadings, goToCourseHeading, useReadingPosition, type ReadingViewport } from './reading-position';
import { refreshReminders } from './reminders';
import { StudyTimer } from './study-timer';
import { BackedStatus, Empty, ErrorNote, Loading, useAction, useRead } from './shared';

const LessonAssistant = lazy(() => import('./chat').then(module => ({ default: module.LessonAssistant })));
const LectureMarkdown = lazy(() => import('./lecture-markdown').then(module => ({ default: module.LectureMarkdown })));
const ModelCourseBody = lazy(() => import('./course-reading').then(module => ({ default: module.ModelCourseBody })));
const LessonAssessmentPanel = lazy(() => import('./assessment-settings').then(module => ({ default: module.LessonAssessmentPanel })));
const LessonConsolidationPanel = lazy(() => import('./study-consolidation').then(module => ({ default: module.LessonConsolidationPanel })));
const PrerequisiteDialog = lazy(() => import('./prerequisite').then(module => ({ default: module.PrerequisiteDialog })));

export function LessonPage({ active, onChange }: { active: Timer | null; onChange: () => void }) {
  const { lessonId = '' } = useParams();
  const read = useRead<LessonDetail>(`/lessons/${lessonId}`);
  if (!read.value) return <section className="page"><BackedStatus error={read.error} retry={read.refresh} /></section>;
  return <LessonContent key={lessonId} lessonId={lessonId} detail={read.value} active={active} onChange={() => { read.refresh(); onChange(); }} />;
}

function CourseDirectory({ lessons, current, headings, hidden }: {
  lessons: Lesson[]; current: string; headings: HTMLElement[]; hidden: boolean;
}) {
  const [open, setOpen] = useState(false);
  const list = useId();
  return <nav className="panel reading-directory" aria-label="课程目录" hidden={hidden}>
    <h2>课程目录</h2>
    <button className="reading-directory-toggle" type="button" aria-controls={list} aria-expanded={open} onClick={() => setOpen(!open)}>
      课程目录 <ChevronDown size={17} />
    </button>
    <div id={list} className="reading-directory-content" data-open={open}>
      <ol>{lessons.map(lesson => <li key={lesson.id}>
        <Link to={courseUrl(lesson.id)} aria-current={lesson.id === current ? 'page' : undefined}>{lesson.title}
          {!lesson.ready && <small>等待课程</small>}
        </Link>
      </li>)}</ol>
      {!!headings.length && <div className="reading-sections">
        <h3>本节小节</h3>
        <ul>{headings.map((heading, index) => <li key={index}>
          <button type="button" onClick={() => goToCourseHeading(heading)}>{heading.textContent}</button>
        </li>)}</ul>
      </div>}
    </div>
  </nav>;
}

function LessonContent({ lessonId, detail, active, onChange }: { lessonId: string; detail: LessonDetail; active: Timer | null; onChange: () => void }) {
  const lesson = detail.project.lessons.find(item => item.id === lessonId)!;
  const [side, setSide] = useState<'note' | 'assistant' | null>(null);
  const [focused, setFocused] = useState(false);
  const [openedAssistant, setOpenedAssistant] = useState(false);
  const [openedNote, setOpenedNote] = useState(false);
  const [excerpts, setExcerpts] = useState<ExcerptRequest[]>([]);
  const excerptNumber = useRef(0);
  const noteTarget = useRef<string | undefined>(undefined);
  const draggedExcerpt = useRef<{ token: string; excerpt: CourseExcerpt } | undefined>(undefined);
  const [draggingExcerpt, setDraggingExcerpt] = useState(false);
  const [readingBody, setReadingBody] = useState<HTMLDivElement | null>(null);
  const [headings, setHeadings] = useState<HTMLElement[]>([]);
  const [selection, setSelection] = useState<CourseSelection>();
  const [prerequisite, setPrerequisite] = useState<PrerequisiteSelection>();
  const returnFromPrerequisite = useRef<ReadingViewport | undefined>(undefined);
  const learnedControl = useRef<HTMLInputElement>(null);
  const jumpToLearned = useRef(false);
  const shownSide = focused ? null : side;
  const noteOpen = shownSide === 'note';
  const [dirtyNotes, setDirtyNotes] = useState(() => new Set(readNoteDraft(lessonId) ? [lessonId] : []));
  const noteDirty = dirtyNotes.size > 0;
  const setNoteTarget = useCallback((id: string | undefined) => {
    noteTarget.current = id;
    if (id) setExcerpts(queue => queue.some(item => !item.noteId)
      ? queue.map(item => item.noteId ? item : { ...item, noteId: id }) : queue);
  }, []);
  const action = useAction();
  const index = detail.project.lessons.indexOf(lesson);
  const canNavigate = (item: Lesson) => item.kind !== 'practice' || item.ready;
  const previous = detail.project.lessons.slice(0, index).findLast(canNavigate);
  const next = detail.project.lessons.slice(index + 1).find(canNavigate);
  const prerequisiteLessons = lesson.prerequisites.map(id => detail.project.lessons.find(item => item.id === id))
    .filter((item): item is Lesson => item !== undefined);
  const practice = lesson.kind === 'practice';
  const model = !practice && detail.project.generationMode === 'model';
  const [params] = useSearchParams();
  const showArchivedCourse = detail.courseVersion !== null;
  const reader = useReadingPosition({
    lesson: lessonId, body: readingBody, revision: detail.readingRevision, initial: detail.readingPosition,
    targeted: params.has('readingVersion') || params.has('supplement'),
    layout: `${focused ? 'focus' : shownSide ?? ''}/${detail.courseVersion ?? ''}`,
  });
  useLayoutEffect(() => {
    setHeadings(readingBody ? courseHeadings(readingBody) : []);
  }, [readingBody, detail.readingRevision]);
  const { restore: restoreReading } = reader;
  useLayoutEffect(() => {
    if (prerequisite || !returnFromPrerequisite.current) return;
    const frame = window.requestAnimationFrame(() => {
      const position = returnFromPrerequisite.current;
      returnFromPrerequisite.current = undefined;
      if (position) restoreReading(position);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [prerequisite, restoreReading]);
  useLayoutEffect(() => {
    if (shownSide || !jumpToLearned.current) return;
    jumpToLearned.current = false;
    learnedControl.current?.focus({ preventScroll: true });
    learnedControl.current?.scrollIntoView({ block: 'center' });
  }, [shownSide]);

  function changeSide(next: 'note' | 'assistant' | null) {
    if (focused || next !== side) reader.hold();
    setFocused(false);
    setSide(next);
  }
  function openNote() {
    setOpenedNote(true);
    changeSide('note');
  }
  function markNoteDirty(id: string, dirty: boolean) {
    setDirtyNotes(previous => {
      if (previous.has(id) === dirty) return previous;
      const next = new Set(previous);
      if (dirty) next.add(id); else next.delete(id);
      return next;
    });
  }
  function addExcerpt(excerpt: CourseExcerpt) {
    const request = { ...excerpt, id: ++excerptNumber.current, noteId: noteTarget.current };
    setExcerpts(queue => [...queue, request]);
    openNote();
  }
  function finishExcerpt(id: number) {
    setExcerpts(queue => queue.filter(excerpt => excerpt.id !== id));
  }
  function endExcerptDrag() {
    draggedExcerpt.current = undefined;
    setDraggingExcerpt(false);
  }
  function startExcerptDrag(event: DragEvent, excerpt: CourseExcerpt) {
    const token = crypto.randomUUID();
    event.dataTransfer.setData(courseExcerptMime, token);
    event.dataTransfer.setData('text/plain', excerpt.markdown);
    event.dataTransfer.effectAllowed = 'copy';
    draggedExcerpt.current = { token, excerpt };
    setDraggingExcerpt(true);
    openNote();
  }
  function dragOverNote(event: DragEvent) {
    if (!draggedExcerpt.current || !event.dataTransfer.types.includes(courseExcerptMime)) return;
    event.preventDefault();
    event.stopPropagation();
    event.dataTransfer.dropEffect = 'copy';
  }
  function dropOnNote(event: DragEvent) {
    const active = draggedExcerpt.current;
    if (!active || event.dataTransfer.getData(courseExcerptMime) !== active.token) return;
    event.preventDefault();
    event.stopPropagation();
    endExcerptDrag();
    addExcerpt(active.excerpt);
  }
  const excerptActions = { add: addExcerpt, drag: startExcerptDrag, endDrag: endExcerptDrag };

  function showAssistant() {
    setOpenedAssistant(true);
    changeSide('assistant');
  }
  function showLearnedControl() {
    jumpToLearned.current = true;
    changeSide(null);
  }
  function showPrerequisite(request: PrerequisiteSelection) {
    returnFromPrerequisite.current = reader.remember();
    setPrerequisite(request);
  }

  return <section className={`page reading-page ${focused ? 'reading-focus' : ''}`}>
    {!focused && <Link className="back" to={projectUrl(detail.project.id)}><ArrowLeft size={16} />{detail.project.topic} · 学习路线</Link>}
    <div className="page-head reading-header"><div><h1>{lesson.title}</h1><p>{lesson.outcome}</p></div></div>
    {detail.body && <StudyTimer active={active} lessonId={lessonId} onChange={onChange}
      category={shownSide === 'note' ? 'notes' : shownSide === 'assistant' ? 'assistant' : 'learning'} />}
    {detail.body && <div className="reading-tools action-row">
      <button type="button" className="button secondary" onClick={() => { reader.hold(); setFocused(!focused); }}>
        {focused ? <><Minimize2 size={16} />退出专注阅读</> : <><Maximize2 size={16} />专注阅读</>}
      </button>
      <button type="button" className="button secondary" onClick={() => showPrerequisite({})} aria-haspopup="dialog">
        <BookOpen size={16} />补前置知识
      </button>
      {!focused && <button type="button" className="button secondary" aria-expanded={noteOpen}
        onClick={() => { if (noteOpen) changeSide(null); else openNote(); }}>
        <NotebookPen size={17} />笔记{noteDirty && ' · 未保存'}</button>}
    </div>}
    <ErrorNote message={reader.failure?.message} retry={reader.failure?.changed ? onChange : reader.retry} />
    <ErrorNote message={action.error} />
    <div className={`reading-grid ${shownSide ? 'with-reading-side' : ''}`}>
      <CourseDirectory lessons={detail.project.lessons} current={lessonId} headings={headings} hidden={focused} />
      <article className="panel reading-panel">
        <div className="eyeline"><BookOpen size={16} />{practice ? '实战任务'
          : model ? '模型学习路线' : detail.source ? '内置讲义 · 附参考资料' : '通用学习方法模板'}</div>
        {!!prerequisiteLessons.length && <div className="action-row prerequisite-links" role="group" aria-label="本节前置知识">
          <strong>前置知识</strong>
          {prerequisiteLessons.map(item => <button type="button" className="text-action" aria-haspopup="dialog"
            key={item.id} onClick={() => showPrerequisite({ targetLessonId: item.id })}>{item.title}</button>)}
        </div>}
        {detail.body || showArchivedCourse ? <Suspense fallback={<Loading text="正在加载讲稿" />}>
          {detail.courseVersion ? <ModelCourseBody lesson={lessonId} project={detail.project.id} body={detail.body ?? ''}
            version={detail.courseVersion} intact={detail.courseMatchesVerification} onReadingBody={setReadingBody}
            onRevised={() => { reader.hold(); onChange(); }}
            revision={detail.readingRevision} excerpts={excerptActions}
            onPrerequisite={topic => showPrerequisite({ topic })} onQuestion={value => {
              setSelection(value);
              showAssistant();
            }} /> : <LectureMarkdown lesson={lessonId} body={detail.body ?? ''} revision={detail.readingRevision}
            excerpts={excerptActions} onReadingBody={setReadingBody} />}
        </Suspense> : detail.courseIssue
          ? <Empty title={detail.courseIssue}>{practice && !lesson.ready
            ? <Link to={projectUrl(detail.project.id)}>回路线重试</Link>
            : <Link to={`/library?file=course-${lessonId}`}>在知识库查看文件位置</Link>}</Empty>
          : <Empty title={model ? '课程等待核验' : '课程正在准备'}><Link to={projectUrl(detail.project.id)}>回路线查看进度</Link></Empty>}
        {detail.body && <div className="learning-footer">
          {detail.objectives.length > 0 && <div className="objectives"><strong>本节知识目标</strong><ul>{detail.objectives.map(item => <li key={item.key}>{item.title}</li>)}</ul></div>}
          {lesson.learnedOn && <div className="notice success" role="status"><Check size={17} />{detail.objectives.length
            ? '已学完。首次复习安排在次日，可在今日复习查看。' : '已学完。'}</div>}
          {detail.courseVersion && <Suspense fallback={<Loading />}>
            <LessonAssessmentPanel key={detail.courseVersion} lesson={lessonId} learned={!!lesson.learnedOn} onChange={onChange} />
            <LessonConsolidationPanel lesson={lessonId} learned={!!lesson.learnedOn} version={detail.courseVersion}
              onPublished={() => { reader.hold(); onChange(); }} />
          </Suspense>}
          <div className="action-row">
            <label className="learned-check">
              <input type="checkbox" ref={learnedControl} checked={Boolean(lesson.learnedOn)} disabled={action.pending}
                onChange={event => {
                  const learned = event.target.checked;
                  void action.run(async () => { await api(`/lessons/${lessonId}/learned`, 'PUT', { learned }); onChange(); refreshReminders(); });
                }} />
              <span>已学完</span>
            </label>
            {!lesson.learnedOn && <button type="button" className="button secondary" onClick={showAssistant}><CircleHelp size={17} />还没懂</button>}
            {lesson.learnedOn && !practice && <Link className="button soft" to="/reviews">查看复习安排 <ArrowRight size={17} /></Link>}
            {previous?.ready && <Link className="text-action" to={courseUrl(previous.id)}><ArrowLeft size={16} />上一节</Link>}
            {next?.ready && <Link className="text-action" to={`/lessons/${next.id}`}>下一节 <ArrowRight size={16} /></Link>}
          </div>
        </div>}
      </article>
      {openedNote && <div className="reading-side" hidden={!noteOpen} data-excerpt-drop={draggingExcerpt || undefined}
        onDragOverCapture={dragOverNote} onDropCapture={dropOnNote}>
        <LessonNotes lessonId={lessonId} visible={noteOpen} onDirty={markNoteDirty} onChange={onChange}
          excerptQueue={excerpts} onExcerptComplete={finishExcerpt} onNoteTarget={setNoteTarget} close={() => changeSide(null)} />
      </div>}
      {openedAssistant && <div className="reading-side" hidden={shownSide !== 'assistant'}>
        <Suspense fallback={<Loading text="正在打开课内助手" />}>
          <LessonAssistant lessonId={lessonId} model={model} selection={selection} clearSelection={() => setSelection(undefined)}
            close={() => changeSide(null)} visible={shownSide === 'assistant'}
            onRouteChanged={onChange}
            showLearnedControl={detail.body && !lesson.learnedOn ? showLearnedControl : undefined} />
        </Suspense>
      </div>}
    </div>
    {!focused && <button type="button" className="assistant-button" aria-label="课内助手" aria-expanded={shownSide === 'assistant'}
      onClick={() => {
        if (shownSide === 'assistant') changeSide(null);
        else showAssistant();
      }}><MessageCircle size={20} aria-hidden="true" /><span>课内助手</span></button>}
    {prerequisite && <Suspense fallback={null}>
      <PrerequisiteDialog lesson={lessonId} request={prerequisite} close={() => setPrerequisite(undefined)} />
    </Suspense>}
  </section>;
}

export function ReviewQueue({ onChange }: { onChange: () => void }) {
  const action = useAction();
  const { value, error, refresh } = useRead<ReviewList>('/reviews');
  const preparing = !!value?.assessmentQueued;
  useEffect(() => {
    if (!preparing) return;
    const interval = window.setInterval(refresh, 900);
    return () => window.clearInterval(interval);
  }, [preparing, refresh]);
  if (!value) return <section className="page"><BackedStatus error={error} retry={refresh} /></section>;
  const due = value.reviews.filter(item => item.status === 'due').sort((a, b) => a.dueOn!.localeCompare(b.dueOn!));
  const upcoming = value.reviews.filter(item => item.status === 'upcoming').sort((a, b) => a.dueOn!.localeCompare(b.dueOn!));
  const paused = value.reviews.filter(item => ['paused', 'complete', 'held', 'waiting'].includes(item.status));

  function reviewRow(item: Review) {
    return <div className="review-row" key={`${item.lessonId}/${item.key}`}>
      <div><strong>{item.title}</strong><span>{item.projectTopic}{item.ability && ` · ${abilityName[item.ability]}`}</span></div>
      <span>{item.status === 'complete' ? '周期已结束' : item.status === 'paused' ? '已暂停' : item.status === 'held' ? '暂缺核验依据'
        : item.status === 'waiting' ? '等待评价' : dateLabel(item.dueOn!)}
        {item.gradingDelayed && <> · <span>评分延迟</span></>}</span>
      {item.status === 'due' && <Link to={reviewUrl(item)} className="button soft" aria-label={`开始复习：${item.title}`}>开始复习 <ArrowRight size={16} /></Link>}
      {item.kind === 'model' && (item.status === 'held' ? <Link className="text-action" to={courseUrl(item.lessonId)}>查看课程依据</Link>
        : item.status === 'waiting' ? <Link className="text-action" to={reviewUrl(item)}>查看评价进度</Link>
        : item.status === 'upcoming' ? <Link className="text-action" to={reviewUrl(item)}>提前练习</Link>
        : item.status === 'complete' ? <Link className="text-action" to={reviewUrl(item)}>查看评价</Link> : null)}
    </div>;
  }

  return <section className="page narrow-page">
    <div className="page-head"><div><h1>今日复习</h1><p>每个知识目标各自安排下一次复习。</p></div></div>
    <div className="notice clock-notice"><CalendarDays size={18} /><span>试用日期 <strong>{dateLabel(value.today)}</strong></span>
      <button className="button secondary" disabled={action.pending} onClick={() => { void action.run(async () => { await api('/demo/next-day', 'POST'); refresh(); onChange(); refreshReminders(); }); }}>推进试用日期一天</button>
    </div>
    <ErrorNote message={error} retry={refresh} />
    <ErrorNote message={action.error} />
    <div className="panel review-section"><h2>到期复习 <span className="count">{due.length}</span></h2>
      {due.length ? due.map(reviewRow) : <Empty title="今天没有到期复习">{upcoming.length ? '可推进试用日期体验次日安排。' : <Link to="/">从首页创建 SQLite 样例</Link>}</Empty>}
    </div>
    {upcoming.length > 0 && <div className="panel review-section"><h2>后续安排</h2>{upcoming.map(reviewRow)}</div>}
    {paused.length > 0 && <div className="panel review-section"><h2>其他复习目标</h2>{paused.map(reviewRow)}</div>}
  </section>;
}

const resultTitles = { passed: '独立通过', failed: '需要再练习', hinted: '已完成辅助练习' };

export function ReviewPage({ active, onChange }: { active: Timer | null; onChange: () => void }) {
  const { lessonId, key } = useParams();
  const endpoint = `/lessons/${lessonId}/reviews/${key}`;
  const read = useRead<ReviewDetail>(endpoint);
  if (!read.value) return <section className="page"><BackedStatus error={read.error} retry={read.refresh} /></section>;
  return <ReviewForm key={`${endpoint}/${read.value.round}`} detail={read.value} endpoint={endpoint} active={active} onChange={onChange} />;
}

function ReviewForm({ detail, endpoint, active, onChange }: { detail: ReviewDetail; endpoint: string; active: Timer | null; onChange: () => void }) {
  const [answers, setAnswers] = useState<(number | null)[]>(() => detail.questions.map((_, index) => detail.answers[index] ?? null));
  const [hint, setHint] = useState(detail.hint);
  const [result, setResult] = useState<ReviewResult | null>(null);
  const navigate = useNavigate();
  const action = useAction();
  const feedback = result ?? (detail.status === 'due' ? null : detail.latest);
  const payload = { round: detail.round, answers };

  function submit(kind: 'submit' | 'unknown') {
    void action.run(async () => {
      setResult(await api<ReviewResult>(`${endpoint}/submit`, 'POST', { ...payload, action: kind }));
      onChange();
      refreshReminders();
    });
  }

  return <section className="page narrow-page">
    <Link className="back" to="/reviews"><ArrowLeft size={16} />今日复习</Link>
    <div className="page-head"><div><h1>{detail.title}</h1><p>{detail.projectTopic} · 第 {feedback && !result ? detail.round - 1 : detail.round} 轮</p></div></div>
    <StudyTimer active={active} lessonId={detail.lessonId} category="review" reviewKey={detail.key} onChange={onChange} />
    <ErrorNote message={action.error} />
    {feedback ? <article className="panel feedback"><h2>{resultTitles[feedback.outcome]}</h2><p>{feedback.explanation}</p>
      <p>{feedback.dueOn ? `下次复习 ${dateLabel(feedback.dueOn)}` : '已完成自动复习周期'}</p><Link className="button primary" to="/reviews">返回复习列表 <ArrowRight size={16} /></Link>
    </article> : detail.status !== 'due' ? <Empty title={detail.status === 'paused' ? '这个知识目标的复习已暂停' : '本轮复习尚未到期'}>
      {detail.dueOn && `计划日期 ${dateLabel(detail.dueOn)}`}
    </Empty> : <form className="review-form" onSubmit={event => { event.preventDefault(); submit('submit'); }}>
      <p className="result-count">已答 {answers.filter(answer => answer !== null).length} / {detail.questions.length} 题</p>
      {detail.questions.map((question, index) => <fieldset className="panel question" key={question.text} disabled={action.pending}>
        <legend>{index + 1}. {question.text}</legend>
        {question.options.map((option, choice) => <label key={option} className={answers[index] === choice ? 'selected' : ''}>
          <input type="radio" name={`question-${index}`} checked={answers[index] === choice} onChange={() => setAnswers(answers.map((answer, position) => index === position ? choice : answer))} />{option}
        </label>)}
      </fieldset>)}
      {hint && <div className="notice" role="status"><Lightbulb size={17} />{hint}</div>}
      <div className="action-row">
        <button className="button primary" disabled={action.pending || answers.includes(null)}>{action.pending ? '正在保存' : '提交本组答案'}</button>
        <button className="button secondary" type="button" disabled={action.pending || !!hint} onClick={() => {
          void action.run(async () => setHint((await api<{ hint: string }>(`${endpoint}/hint`, 'POST', { round: detail.round })).hint));
        }}><Lightbulb size={16} />给点提示</button>
        <button className="button secondary" type="button" disabled={action.pending} onClick={() => {
          void action.run(async () => { await api(`${endpoint}/draft`, 'PUT', payload); navigate('/reviews'); });
        }}><FilePenLine size={16} />稍后再答</button>
        <button className="text-action" type="button" disabled={action.pending} onClick={() => submit('unknown')}><CircleHelp size={16} />我不会，查看讲解</button>
      </div>
    </form>}
  </section>;
}
