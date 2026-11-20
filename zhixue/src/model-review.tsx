import { useEffect, useState } from 'react';
import { ArrowLeft, Lightbulb } from 'lucide-react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { refreshReminders } from './reminders';
import { StudyTimer } from './study-timer';
import { api, type Timer, courseUrl, dateLabel } from './api';
import { abilityName, type AssessmentDetail, type AssessmentGroup, type RemediationStatus } from './assessment-types';
import { BackedStatus, Empty, ErrorNote, useAction, useRead } from './shared';
import { VoiceInput } from './voice-input';

const outcomes = { passed: '独立通过', failed: '需要再练习', hinted: '已完成辅助练习' };
const remediationProgress = { drafting: '正在起草错因补学', verifying: '正在独立核验补学', publishing: '正在保存补学讲解' };

function AssessmentAnswers({ value, changed }: { value: AssessmentGroup; changed: (group: AssessmentGroup) => void }) {
  const [answers, setAnswers] = useState(() => value.questions.map((_, index) => value.draft[index] ?? ''));
  const [hint, setHint] = useState(value.hint);
  const navigate = useNavigate();
  const action = useAction();
  const endpoint = `/assessments/${value.id}`;
  function submit(kind: 'submit' | 'unknown') {
    void action.run(async () => changed(await api<AssessmentGroup>(`${endpoint}/submit`, 'POST', { action: kind, answers })));
  }
  function changeAnswer(index: number, answer: string) {
    setAnswers(values => values.map((item, position) => position === index ? answer : item));
  }
  return <form className="review-form" onSubmit={event => { event.preventDefault(); submit('submit'); }}>
    <p className="result-count">已答 {answers.filter(answer => answer.trim()).length} / {value.questions.length} 题</p>
    {value.questions.map((question, index) => <div className="panel assessment-question" key={index}>
      <label>
        <strong>{index + 1}. {question.text}</strong>
        <textarea rows={4} value={answers[index]} maxLength={3000} disabled={action.pending}
          onChange={event => changeAnswer(index, event.currentTarget.value)} />
      </label>
      <div><VoiceInput label={`第 ${index + 1} 题语音作答`} value={answers[index]} limit={3000} disabled={action.pending}
        onChange={answer => changeAnswer(index, answer)} /></div>
    </div>)}
    {hint && <div className="notice" role="status"><Lightbulb size={17} />{hint}</div>}
    <ErrorNote message={action.error} />
    <div className="action-row">
      <button type="submit" className="button primary" disabled={action.pending || answers.some(answer => !answer.trim())}>提交本组答案</button>
      <button type="button" className="button secondary" disabled={action.pending || !!hint}
        onClick={() => void action.run(async () => setHint((await api<AssessmentGroup>(`${endpoint}/hint`, 'POST')).hint))}>给点提示</button>
      <button type="button" className="button secondary" disabled={action.pending} onClick={() => void action.run(async () => {
        await api(`${endpoint}/draft`, 'PUT', { answers });
        navigate('/reviews');
      })}>稍后再答</button>
      <button type="button" className="text-action" disabled={action.pending} onClick={() => submit('unknown')}>我不会，查看讲解</button>
    </div>
  </form>;
}

function RemediationResult({ lesson, value, retry, busy }: {
  lesson: string; value: RemediationStatus; retry: () => void; busy: boolean;
}) {
  if (value.state === 'superseded') return null;
  if (value.state === 'published') return <Link className="button secondary"
    to={`${courseUrl(lesson)}?supplement=${encodeURIComponent(value.id)}`}>查看课程中的错因补学</Link>;
  if (value.state === 'authorization_required') return <Link className="text-action" to={courseUrl(lesson)}>回课程启用错因补学</Link>;
  if (value.state === 'paused') return <p className="model-help">课程复习已暂停，恢复后继续处理补学。</p>;
  if (value.state === 'blocked' || value.state === 'disputed') return <div className="assessment-remediation">
    <ErrorNote message={value.error ?? '补学草稿未通过独立核验。'} />
    <button type="button" className="button secondary" disabled={busy} onClick={retry}>重试错因补学</button>
  </div>;
  return <p className="model-help" role="status">{remediationProgress[value.state]}</p>;
}

function Report({ lesson, value, canRecheck, recheck, retryRemediation, busy }: {
  lesson: string; value: AssessmentGroup; canRecheck: boolean; recheck: () => void; retryRemediation: () => void; busy: boolean;
}) {
  const result = value.result;
  if (!result) return null;
  return <article className="panel feedback">
    <h2>{outcomes[result.outcome]}</h2><p>{result.explanation}</p>
    <ol className="assessment-feedback">{result.items.map(item => <li key={item.index}>
      <strong>{value.questions[item.index]?.text}</strong>
      <span>{item.required ? '核心能力' : '补充检查'} · {item.met ? '已达到' : '待练习'}</span>
      {value.answers[item.index] && <blockquote className="assessment-answer evidence-text">{value.answers[item.index]}</blockquote>}
      <p>{item.reason}</p>
    </li>)}</ol>
    {result.gradingDelayed && <p className="notice" role="status">评分延迟 · 作答于 {dateLabel(result.submittedOn)}，评价于 {dateLabel(result.scoredOn)}</p>}
    <p>{value.mode === 'practice' ? '本次为额外练习，正式复习日期沿用原安排。'
      : result.dueOn ? `下次复习 ${dateLabel(result.dueOn)}` : '已完成自动复习周期'}</p>
    {!!value.noteReferences?.length && <details className="assessment-note-picker">
      <summary>出题时的笔记参考范围</summary>
      <ul className="assessment-note-list">{value.noteReferences.map(item => <li key={item.route + '/' + item.text}>
        <Link to={item.route}>{item.title} · 第 {item.line} 行</Link>
        <blockquote>{item.text}</blockquote>
      </li>)}</ul>
    </details>}
    {value.history.length > 1 && <details><summary>历次评价</summary><ul>
      {value.history.map((item, index) => <li key={index}>
        {outcomes[item.outcome]} · {item.reviewer?.name ?? '用户选择查看讲解'} · {item.explanation}
      </li>)}
    </ul></details>}
    {value.remediation && <RemediationResult lesson={lesson} value={value.remediation} retry={retryRemediation} busy={busy} />}
    <div className="action-row">
      <Link className="button primary" to="/reviews">返回复习列表</Link>
      {canRecheck && <button type="button" className="button secondary" disabled={busy} onClick={recheck}>重新核查</button>}
    </div>
  </article>;
}

export function ModelReviewPage({ timer, onChange }: { timer: Timer | null; onChange: () => void }) {
  const { lessonId = '', key = '' } = useParams();
  const [params, setParams] = useSearchParams();
  const selected = params.get('group');
  const endpoint = `/lessons/${lessonId}/assessments/${key}`;
  const read = useRead<AssessmentDetail>(endpoint);
  const active = useRead<AssessmentGroup>(selected ? `/assessments/${encodeURIComponent(selected)}` : null);
  const action = useAction();
  const detail = read.value;
  const candidate = selected ? active.value : detail?.group;
  const refreshDetail = read.refresh;
  const refreshGroup = active.refresh;
  const supplement = candidate?.remediation?.state;
  const running = candidate?.jobState === 'queued' || candidate?.jobState === 'running'
    || supplement === 'drafting' || supplement === 'verifying' || supplement === 'publishing';
  const state = candidate?.state;
  useEffect(() => {
    if (!running) return;
    const interval = window.setInterval(selected ? refreshGroup : refreshDetail, 700);
    return () => window.clearInterval(interval);
  }, [running, selected, refreshGroup, refreshDetail]);
  useEffect(() => {
    if (selected && state === 'graded') {
      refreshDetail();
      onChange();
    }
  }, [selected, state, refreshDetail, onChange]);
  if (!detail) return <section className="page"><BackedStatus error={read.error} retry={refreshDetail} /></section>;
  const group = candidate;
  function changed(saved: AssessmentGroup) {
    setParams({ group: saved.id });
    refreshGroup();
    refreshDetail();
    onChange();
    refreshReminders();
  }
  function prepare(mode: 'formal' | 'practice') {
    void action.run(async () => changed(await api<AssessmentGroup>(`${endpoint}/prepare`, 'POST', { mode })));
  }
  function control(next: 'retry' | 'recheck' | 'remediation/retry') {
    if (group) void action.run(async () => changed(await api<AssessmentGroup>(`/assessments/${group.id}/${next}`, 'POST')));
  }
  const mayWrite = group?.round === detail.round && (group?.mode === 'formal' ? detail.status === 'due'
    : ['due', 'upcoming', 'complete'].includes(detail.status));
  return <section className="page narrow-page">
    <Link className="back" to="/reviews"><ArrowLeft size={16} />今日复习</Link>
    <div className="page-head"><div><h1>{detail.title}</h1>
      <p>{detail.projectTopic}{detail.ability && ` · ${abilityName[detail.ability]}`}
        {group ? ` · ${group.mode === 'practice' ? '提前练习' : `第 ${group.round} 轮`}` : ''}</p>
    </div></div>
    <StudyTimer active={timer} lessonId={lessonId} category="review" reviewKey={key} onChange={onChange} />
    <ErrorNote message={action.error || read.error} retry={read.error ? refreshDetail : undefined} />
    {selected && !group ? <BackedStatus error={active.error} retry={refreshGroup} /> : group?.state === 'graded'
      ? <Report lesson={lessonId} value={group}
        canRecheck={group.mode === 'formal' && group.answers.some(answer => answer.trim()) && group.round + 1 === detail.round}
        recheck={() => control('recheck')} retryRemediation={() => control('remediation/retry')} busy={action.pending} />
      : group && group.state !== 'ready' ? <div className="panel assessment-status">
        <strong role="status">{group.state === 'superseded' ? '课程更新 · 旧题组已保留'
          : group.state === 'preparing' ? '正在准备题组'
          : group.state === 'grading' ? '答案已保存，等待评分' : '正在重新核查，已暂撤原判定的排期影响'}</strong>
        <ErrorNote message={group.error ?? undefined} retry={group.jobState === 'blocked' ? () => control('retry') : undefined} />
        {group.answers.length > 0 && <details><summary>查看已保存答案</summary><ol>
          {group.answers.map((answer, index) => <li className="evidence-text" key={index}>{answer}</li>)}
        </ol></details>}
        {group.jobState === 'blocked' && <Link className="text-action" to={courseUrl(lessonId)}>返回课程查看复习授权</Link>}
        {group.state === 'superseded' && <Link className="text-action" to={courseUrl(lessonId)}>回到课程确认新版依据</Link>}
      </div> : group && mayWrite ? <AssessmentAnswers key={group.id} value={group} changed={changed} /> : <div className="panel assessment-status">
        {detail.status === 'due' ? <button type="button" className="button primary" disabled={action.pending} onClick={() => prepare('formal')}>准备本轮变式题</button>
          : detail.status === 'upcoming' || detail.status === 'complete' ? <>
            <p>{detail.dueOn ? `正式复习安排在 ${dateLabel(detail.dueOn)}` : '自动复习周期已结束'}</p>
            <button type="button" className="button secondary" disabled={action.pending} onClick={() => prepare('practice')}>提前练习</button>
          </> : <Empty title={detail.status === 'held' ? '等待可核验依据' : '当前复习暂不可作答'}>
            <Link to={courseUrl(lessonId)}>回到课程</Link>
          </Empty>}
        {detail.latest && <Link className="text-action" to={`?group=${detail.latest.id}`}>最近一次评价</Link>}
      </div>}
  </section>;
}
