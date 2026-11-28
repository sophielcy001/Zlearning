import { useState } from 'react';
import { ArrowLeft } from 'lucide-react';
import { Link } from 'react-router-dom';
import { api, courseUrl, dateLabel } from './api';
import { BackedStatus, Empty, ErrorNote, useAction, useRead } from './shared';

type Question = { text: string; options: string[] };
type PracticeResult = { correctCount: number; answers: number[]; correct: number[]; submittedOn: string };
type Practice = {
  lessonId: string; title: string; projectTopic: string; sourceRound: number; sourceOn: string; explanation: string;
} & (
  | { state: 'read_only'; reason: string }
  | { state: 'ready'; questions: Question[]; basis: string }
  | { state: 'completed'; questions: Question[]; result: PracticeResult }
);

export function studyReportUrl(params: URLSearchParams) {
  const query = new URLSearchParams();
  const period = params.get('period');
  const on = params.get('on');
  if (period === 'day' || period === 'week' || period === 'month') query.set('period', period);
  if (on && /^\d{4}-\d{2}-\d{2}$/.test(on)) query.set('on', on);
  for (const key of ['activity', 'timerQuery', 'reviewOutcome']) {
    const value = params.get(key);
    if (value) query.set(key, value);
  }
  return query.size ? `/study-report?${query}` : '/study-report';
}

function PracticeForm({ value, endpoint, saved, refresh }: {
  value: Extract<Practice, { state: 'ready' }>; endpoint: string; saved: (result: Practice) => void; refresh: () => void;
}) {
  const [answers, setAnswers] = useState<(number | null)[]>(() => value.questions.map(() => null));
  const action = useAction();
  return <form className="review-form" onSubmit={event => {
    event.preventDefault();
    void action.run(async () => saved(await api<Practice>(endpoint, 'POST', { basis: value.basis, answers })));
  }}>
    <p className="result-count">已答 {answers.filter(answer => answer !== null).length} / {value.questions.length} 题</p>
    {value.questions.map((question, index) => <fieldset className="panel question" key={question.text} disabled={action.pending}>
      <legend>{index + 1}. {question.text}</legend>
      {question.options.map((option, choice) => <label key={option} className={answers[index] === choice ? 'selected' : ''}>
        <input type="radio" name={`practice-${index}`} required checked={answers[index] === choice}
          onChange={() => setAnswers(previous => previous.map((answer, position) => position === index ? choice : answer))} />{option}
      </label>)}
    </fieldset>)}
    <ErrorNote message={action.error} />
    <div className="action-row">
      <button className="button primary" disabled={action.pending || answers.includes(null)}>
        {action.pending ? '正在保存' : '提交专项练习'}
      </button>
      {action.error && <button type="button" className="button secondary" onClick={refresh}>刷新题组</button>}
    </div>
  </form>;
}

export function LocalWeakPractice({ endpoint, report }: { endpoint: string; report: string }) {
  const read = useRead<Practice>(endpoint);
  const [saved, setSaved] = useState<Practice>();
  const value = saved ?? read.value;
  return <section className="page narrow-page">
    <Link className="back" to={report}><ArrowLeft size={16} />返回学习报告</Link>
    {!value ? <BackedStatus error={read.error} retry={read.refresh} /> : <>
      <div className="page-head"><div><h1>{value.title}</h1>
        <p>{value.projectTopic} · 专项练习 · 来自第 {value.sourceRound} 轮复习（{dateLabel(value.sourceOn)}）</p>
      </div></div>
      <p className="report-hint">专项练习单独记录。正式复习日期沿用原安排。</p>
      <details className="panel feedback"><summary>查看原讲解</summary><p>{value.explanation}</p></details>
      <ErrorNote message={read.error} retry={read.refresh} />
      {value.state === 'ready' ? <PracticeForm key={value.basis} value={value} endpoint={endpoint} saved={setSaved} refresh={read.refresh} />
        : value.state === 'read_only' ? <Empty title={value.reason}><Link to={courseUrl(value.lessonId)}>回到课程</Link></Empty>
        : <article className="panel feedback">
          <h2>专项练习已完成</h2><p>答对 {value.result.correctCount} / {value.questions.length} 题</p>
          <ol className="assessment-feedback">{value.questions.map((question, index) => <li key={question.text}>
            <strong>{question.text}</strong>
            <span>{value.result.answers[index] === value.result.correct[index] ? '答对' : '待练习'}</span>
            <p>你的答案：{question.options[value.result.answers[index]]}</p>
            <p>参考答案：{question.options[value.result.correct[index]]}</p>
          </li>)}</ol>
        </article>}
    </>}
  </section>;
}
