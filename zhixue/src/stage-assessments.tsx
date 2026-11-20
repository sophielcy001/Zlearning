import { useEffect, useState, type ChangeEvent } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { api, apiResponse, courseUrl } from './api';
import { BackedStatus, ErrorNote, Modal, useAction, useRead } from './shared';
import { AuthorizedScope, MODEL_REVIEW_STATES, recordLines, StageModelReview, type ModelReviewJob } from './stage-model-review';

type StageStatus = 'awaiting' | 'skipped' | 'awaiting_review' | 'needs_work' | 'insufficient' | 'passed';
type Verdict = 'met' | 'needs_work' | 'insufficient';
type EvidenceKind = 'code' | 'project' | 'report' | 'response';
type Context = { lessonId: string; title: string; outcome: string };
type Question = { lessonId: string; prompt: string; options: string[] };
type GradedQuestion = Question & { correct: number; answer: number; matched: boolean; explanation: string };
type Evidence = { name: string; kind: EvidenceKind; text: string; coverage?: string | null };
type Citation = { evidenceIndex: number; quote: string };
type LineCitation = Citation & { firstLine: number; lastLine: number };
type Finding = { lessonId: string; verdict: Verdict; note?: string; citation?: Citation | null; citations?: LineCitation[] };
type Attempt = { id: string; number: number; kind: 'quiz' | 'evidence'; outcome: StageStatus };
type ReviewModel = { name: string; model: string };
type AttemptDetail = Attempt & {
  scope: 'static' | 'quiz'; reviewer?: 'quiz' | 'self' | 'model'; questions?: GradedQuestion[];
  modelReview?: (ModelReviewJob & { heldReport?: { model: ReviewModel; findings: Finding[] } | null }) | null;
  model?: ReviewModel;
  findings: Finding[]; evidence: Evidence[];
};
type Stage = {
  id: string; phase: string; routeVersion: number; status: StageStatus; eligible: boolean; currentScope: boolean;
  attemptCount: number; revision: number; modelReviewState?: ModelReviewJob['state'] | null;
};
type StageDetail = Stage & { criteria: Context[]; quiz: Question[]; attempts: [AttemptDetail, ...Attempt[]] | [] };
type QuizSubmission = { id: string; kind: 'quiz'; expectedAttempt: number; answers: number[] };
type EvidenceSubmission = { id: string; kind: 'evidence'; expectedAttempt: number; evidence: Evidence[] };
type Mode = 'quiz' | 'evidence';

const STATUSES: Record<StageStatus, string> = {
  awaiting: '待验收', skipped: '已跳过', awaiting_review: '待评审',
  needs_work: '需补学', insufficient: '证据不足', passed: '已通过',
};
const VERDICTS: Record<Verdict, string> = { met: '符合要求', needs_work: '需补学', insufficient: '证据不足' };
const EVIDENCE_TYPES: Record<EvidenceKind, string> = {
  response: '成果说明', code: '外部代码', project: '项目文件', report: '实验报告',
};
const MAX_EVIDENCE_COUNT = 8;
const MAX_EVIDENCE_BYTES = 250_000;

function EvidencePreview({ evidence }: { evidence: Evidence[] }) {
  return <div className="stage-evidence">
    {evidence.map((item, index) => <details key={index}>
      <summary><strong>{item.name}</strong><span>{EVIDENCE_TYPES[item.kind]}</span></summary>
      {item.coverage && <p className="model-help">{item.coverage}</p>}
      <pre tabIndex={0} aria-label={item.name}>{item.text}</pre>
    </details>)}
  </div>;
}

function QuizForm({ stage, onSave, pending }: {
  stage: StageDetail; onSave: (data: QuizSubmission) => void; pending: boolean;
}) {
  const [identity] = useState(() => crypto.randomUUID());
  const [answers, setAnswers] = useState<Record<number, number>>({});
  return <form className="stage-form" onSubmit={event => {
    event.preventDefault();
    onSave({ id: identity, kind: 'quiz', expectedAttempt: stage.attemptCount, answers: stage.quiz.map((_, index) => answers[index]) });
  }}>
    {stage.quiz.map((question, index) => <fieldset className="question stage-fieldset" key={index} disabled={pending}>
      <legend>{question.prompt}</legend>
      {question.options.map((option, choice) => <label className={answers[index] === choice ? 'selected' : ''} key={option}>
        <input type="radio" name={'stage-question-' + index} checked={answers[index] === choice}
          onChange={() => setAnswers(previous => ({ ...previous, [index]: choice }))} />
        <span>{option}</span>
      </label>)}
    </fieldset>)}
    <div className="action-row"><button className="button primary" disabled={pending || Object.keys(answers).length !== stage.quiz.length}>提交答案</button></div>
  </form>;
}

function EvidenceForm({ path, stage, onSave, pending }: {
  path: string; stage: StageDetail; onSave: (data: EvidenceSubmission) => void; pending: boolean;
}) {
  const upload = useAction();
  const [identity] = useState(() => crypto.randomUUID());
  const [kind, setKind] = useState<EvidenceKind>('response');
  const [evidence, setEvidence] = useState<Evidence[]>([]);
  const [text, setText] = useState('');
  const hasText = Boolean(text.trim());
  const all = hasText ? [...evidence, { name: EVIDENCE_TYPES[kind], kind, text }] : evidence;
  const oversized = all.length > MAX_EVIDENCE_COUNT
    || all.reduce((sum, item) => sum + new TextEncoder().encode(item.text).length, 0) > MAX_EVIDENCE_BYTES;
  const locked = upload.pending || pending;
  function selectFiles(event: ChangeEvent<HTMLInputElement>) {
    const chosen = Array.from(event.currentTarget.files ?? []);
    event.currentTarget.value = '';
    if (!chosen.length) return;
    void upload.run(async () => {
      if (all.length + chosen.length > MAX_EVIDENCE_COUNT) throw new Error('每次测评最多可提交 8 份证据。');
      const added: Evidence[] = [];
      for (const file of chosen) {
        const query = new URLSearchParams({ name: file.name });
        const response = await apiResponse(`${path}/evidence-preview?${query}`, 'POST', file, 'application/octet-stream');
        const preview: Omit<Evidence, 'kind'> = await response.json();
        added.push({ ...preview, kind });
      }
      setEvidence(previous => [...previous, ...added]);
    });
  }
  return <form className="stage-form stage-inputs" onSubmit={event => {
    event.preventDefault();
    if (!oversized && all.length) onSave({ id: identity, kind: 'evidence', expectedAttempt: stage.attemptCount, evidence: all });
  }}>
    <p className="model-help">可添加 UTF-8 源码、多个项目文件，或能提取文字的 PDF、DOCX 实验报告。保存这次测评时会保留可审查的文字。</p>
    <label>证据类型<select value={kind} disabled={locked} onChange={event => setKind(event.target.value as EvidenceKind)}>
      {Object.entries(EVIDENCE_TYPES).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
    </select></label>
    <label>添加证据文件<input type="file" multiple disabled={locked || all.length >= MAX_EVIDENCE_COUNT} onChange={selectFiles} /></label>
    {upload.pending && <p role="status">正在读取证据文字</p>}
    {evidence.map((item, index) => <div className="stage-file-entry" key={index}>
      <EvidencePreview evidence={[item]} />
      <button type="button" className="text-action" disabled={locked} aria-label={'移除 ' + item.name}
        onClick={() => setEvidence(previous => previous.filter((_, at) => index !== at))}>移除</button>
    </div>)}
    <label>粘贴证据文字<textarea rows={4} value={text} disabled={locked} maxLength={MAX_EVIDENCE_BYTES}
      onChange={event => setText(event.target.value)} /></label>
    <ErrorNote message={upload.error || (oversized ? '证据合计最多 8 份，文字合计最多 250 KB。' : undefined)} />
    <div className="action-row"><button className="button primary" disabled={locked || oversized || !all.length}>保存成果与证据</button></div>
  </form>;
}

type SelfFinding = { verdict: Verdict | ''; note: string; source: string; quote: string };
const EMPTY_FINDING: SelfFinding = { verdict: '', note: '', source: '', quote: '' };

function SelfReview({ attempt, criteria, onSave, pending }: {
  attempt: AttemptDetail; criteria: Context[]; onSave: (findings: Finding[]) => void; pending: boolean;
}) {
  const [drafts, setDrafts] = useState<Record<string, SelfFinding>>({});
  function change(lessonId: string, update: Partial<SelfFinding>) {
    setDrafts(previous => ({ ...previous, [lessonId]: { ...(previous[lessonId] ?? EMPTY_FINDING), ...update } }));
  }
  const complete = criteria.every(({ lessonId }) => {
    const item = drafts[lessonId];
    return Boolean(item?.verdict && item.note.trim()
      && (item.verdict === 'insufficient' && item.source === '' || item.source !== '' && item.quote.trim()));
  });
  return <form className="stage-form stage-inputs" onSubmit={event => {
    event.preventDefault();
    if (!complete) return;
    onSave(criteria.map(({ lessonId }) => {
      const item = drafts[lessonId];
      return { lessonId, verdict: item.verdict as Verdict, note: item.note, citation: item.source === '' ? null
        : { evidenceIndex: Number(item.source), quote: item.quote } };
    }));
  }}>
    <p className="model-help">对照每项要求，从提交内容中复制可支撑判断的原文。</p>
    {criteria.map(context => {
      const item = drafts[context.lessonId] ?? EMPTY_FINDING;
      return <fieldset className="stage-fieldset stage-form" key={context.lessonId} disabled={pending}>
        <legend>{context.title}</legend>
        <p>{context.outcome}</p>
        <label>自评结果<select value={item.verdict} onChange={event => change(context.lessonId, {
          verdict: event.target.value as SelfFinding['verdict'],
        })}>
          <option value="">选择结果</option>
          {Object.entries(VERDICTS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select></label>
        <label>判断理由<textarea rows={2} value={item.note} maxLength={1000}
          onChange={event => change(context.lessonId, { note: event.target.value })} /></label>
        <label>引用证据<select value={item.source} onChange={event => change(context.lessonId, { source: event.target.value, quote: '' })}>
          <option value="">选择证据</option>
          {attempt.evidence.map((evidence, index) => <option value={index} key={index}>{index + 1}. {evidence.name}</option>)}
        </select></label>
        {item.source !== '' && <label>证据原文<textarea rows={2} value={item.quote} maxLength={800}
          onChange={event => change(context.lessonId, { quote: event.target.value })} /></label>}
      </fieldset>;
    })}
    <div className="action-row"><button className="button primary" disabled={pending || !complete}>保存成果自评</button></div>
  </form>;
}

function AttemptResults({ attempt, criteria }: { attempt: AttemptDetail; criteria: Context[] }) {
  const passed = attempt.outcome === 'passed';
  const passLabels = { quiz: '测评通过', self: '成果自评通过', model: '模型评审通过' };
  const returned = attempt.modelReview?.heldReport;
  const model = returned?.model ?? attempt.model;
  const result = returned ? '已返回的模型评审' : passed ? passLabels[attempt.reviewer ?? 'quiz'] : STATUSES[attempt.outcome];
  return <div className="stage-form">
    <div className="stage-status-line"><strong>{result}</strong>
      {attempt.scope === 'static' && <span className="tag">仅静态审查</span>}
      {model && <span className="model-help">{model.name} · {model.model}</span>}
    </div>
    {attempt.scope === 'static' && <EvidencePreview evidence={attempt.evidence} />}
    {attempt.modelReview && (attempt.reviewer === 'model' || returned) && <AuthorizedScope job={attempt.modelReview} />}
    {(returned?.findings ?? attempt.findings).map(finding => {
      const context = criteria.find(item => item.lessonId === finding.lessonId);
      if (!context) return null;
      return <div className="stage-finding" key={finding.lessonId}>
        <div className="stage-status-line"><strong>{context.title}</strong><span>{VERDICTS[finding.verdict]}</span></div>
        {finding.note && <p>{finding.note}</p>}
        {finding.citation && <blockquote>{finding.citation.quote}<cite>{attempt.evidence[finding.citation.evidenceIndex]?.name}</cite></blockquote>}
        {finding.citations?.map((citation, index) => <blockquote key={index}><span>{citation.quote}</span>
          <cite>{attempt.evidence[citation.evidenceIndex]?.name} · {recordLines(citation.firstLine, citation.lastLine)}</cite>
        </blockquote>)}
        {finding.verdict !== 'met' && <Link className="text-action" to={courseUrl(context.lessonId)}>补学 {context.title}</Link>}
      </div>;
    })}
    {attempt.questions?.map(question => <div className="stage-finding" key={question.prompt}>
      <strong>{question.prompt}</strong>
      <p>你的答案 · {question.options[question.answer]}</p>
      {!question.matched && <p>参考答案 · {question.options[question.correct]}</p>}
      <p className="model-help">{question.explanation}</p>
    </div>)}
  </div>;
}

function PastAttempt({ path, criteria }: { path: string; criteria: Context[] }) {
  const data = useRead<AttemptDetail>(path);
  return data.value ? <AttemptResults attempt={data.value} criteria={criteria} />
    : <BackedStatus error={data.error} retry={data.refresh} />;
}

function StageDialog({ stage, path, onClose, onChange }: {
  stage: Stage; path: string; onClose: () => void; onChange: () => void;
}) {
  const { value: detail, error, refresh } = useRead<StageDetail>(path);
  const [mode, setMode] = useState<Mode>();
  const [retaking, setRetaking] = useState(false);
  const [history, setHistory] = useState<string>();
  const action = useAction();
  const pending = action.pending;
  const job = detail?.attempts[0]?.modelReview;
  const watching = Boolean(job && (job.active || (detail?.eligible && detail.status !== 'skipped'
    && (job.state === 'queued' || job.reason === 'daily_budget'))));
  const status = detail?.status;
  useEffect(() => {
    if (!watching) return;
    const timer = window.setInterval(refresh, 750);
    return () => window.clearInterval(timer);
  }, [watching, refresh]);
  useEffect(() => { if (status) onChange(); }, [status, onChange]);
  async function mutate(url: string, method: string, body?: object) {
    const saved = await action.run(async () => {
      await api<StageDetail>(`${path}${url}`, method, body);
      setMode(undefined);
      setRetaking(false);
      refresh();
      return true;
    });
    return saved === true;
  }
  return <Modal title={stage.phase} onClose={onClose} busy={pending}>
    {!detail ? <BackedStatus error={error} retry={refresh} /> : (() => {
      const [latest, ...older] = detail.attempts;
      const skipped = detail.status === 'skipped';
      const working = detail.eligible && !skipped;
      const starting = working && (detail.status === 'awaiting' || retaking);
      const needsReview = working && detail.status === 'awaiting_review' && latest?.kind === 'evidence';
      const canSelfReview = needsReview && !job?.active && job?.state !== 'queued';
      const modelPath = latest ? `/attempts/${latest.id}/model-review` : '';
      return <div className="stage-form">
        <div className="stage-status-line"><span className="tag">{STATUSES[detail.status]}</span>
          <span className="model-help">路线版本 {detail.routeVersion}</span>
        </div>
        {!detail.currentScope ? <p>此测评属于已替换的阶段范围。</p>
          : !detail.eligible && <p>完成本阶段课程后可以继续测评。</p>}
        {!detail.currentScope && latest?.modelReview && <p className="model-help">
          模型评审状态 · {MODEL_REVIEW_STATES[latest.modelReview.state]}
          {latest.modelReview.active && '。等待已发出的请求返回。'}
        </p>}
        {starting && <div className="stage-form">
          <ul className="stage-criteria">{detail.criteria.map(criterion => <li key={criterion.lessonId}>
            <strong>{criterion.title}</strong><span>{criterion.outcome}</span>
          </li>)}</ul>
          <div className="action-row">
            {detail.quiz.length > 0 && <button type="button" className="button soft" aria-pressed={mode === 'quiz'}
              disabled={pending} onClick={() => setMode('quiz')}>开始答题</button>}
            <button type="button" className="button soft" aria-pressed={mode === 'evidence'}
              disabled={pending} onClick={() => setMode('evidence')}>提交成果</button>
          </div>
          {mode === 'quiz' && <QuizForm key={detail.attemptCount} stage={detail} pending={pending}
            onSave={data => mutate('/attempts', 'POST', data)} />}
          {mode === 'evidence' && <EvidenceForm key={detail.attemptCount} path={path} stage={detail} pending={pending}
            onSave={data => mutate('/attempts', 'POST', data)} />}
        </div>}
        {latest && !starting && <AttemptResults attempt={latest} criteria={detail.criteria} />}
        {needsReview && <StageModelReview key={"model-" + latest.id} path={path + modelPath} evidence={latest.evidence}
          job={job} pending={pending} save={(endpoint, body) => mutate(modelPath + endpoint, 'POST', body)} />}
        {canSelfReview && <SelfReview key={"self-" + latest.id} attempt={latest} criteria={detail.criteria} pending={pending}
          onSave={findings => mutate(`/attempts/${latest.id}/review`, 'PUT', { findings })} />}
        <ErrorNote message={action.error || error} retry={error ? refresh : undefined} />
        {detail.eligible && <div className="action-row">
          {working && !starting && ['needs_work', 'insufficient'].includes(detail.status)
            && <button type="button" className="button primary" disabled={pending} onClick={() => setRetaking(true)}>重新测评</button>}
          {detail.status !== 'passed' && <button type="button" className="button secondary" disabled={pending}
            onClick={() => mutate('/skip', 'PUT', { skipped: !skipped, expectedRevision: detail.revision })}>
            {skipped ? '继续测评' : '跳过测评'}
          </button>}
        </div>}
        {older.length > 0 && <div className="stage-form">
          <h3>历史测评</h3>
          {older.map(attempt => <div className="stage-finding" key={attempt.id}>
            <div className="stage-status-line"><button type="button" className="text-action" aria-expanded={history === attempt.id}
              onClick={() => setHistory(previous => previous === attempt.id ? undefined : attempt.id)}>查看第 {attempt.number} 次测评</button>
              <span>{STATUSES[attempt.outcome]}</span></div>
            {history === attempt.id && <PastAttempt path={`${path}/attempts/${attempt.id}`} criteria={detail.criteria} />}
          </div>)}
        </div>}
      </div>;
    })()}
  </Modal>;
}

export function StageAssessments({ projectId }: { projectId: string }) {
  const path = `/projects/${projectId}/stage-assessments`;
  const { value, error, refresh } = useRead<{ items: Stage[] }>(path);
  const [query, setQuery] = useSearchParams();
  const selected = value?.items.find(item => item.id === query.get('stageAssessment'));
  const watching = !selected && value?.items.some(item => item.eligible && item.status === 'awaiting_review'
    && (item.modelReviewState === 'queued' || item.modelReviewState === 'running'));
  useEffect(() => {
    if (!watching) return;
    const timer = window.setInterval(refresh, 850);
    return () => window.clearInterval(timer);
  }, [watching, refresh]);
  function setIdentity(identity?: string) {
    setQuery(previous => {
      const next = new URLSearchParams(previous);
      if (identity) next.set('stageAssessment', identity);
      else next.delete('stageAssessment');
      return next;
    }, { replace: true });
  }
  if (!value?.items.length) return <ErrorNote message={error} retry={refresh} />;
  return <section className="panel stage-panel" aria-label="阶段综合测评">
    <h2>阶段综合测评</h2>
    <ul>{value.items.map(item => <li key={item.id}>
      <button className="stage-row" onClick={() => setIdentity(item.id)}>
        <strong>{item.phase}</strong><span className="tag">{!item.currentScope ? '历史阶段'
          : !item.eligible ? '阶段课程待完成'
            : item.status === 'awaiting_review' && item.modelReviewState
              ? MODEL_REVIEW_STATES[item.modelReviewState] : STATUSES[item.status]}</span>
      </button>
    </li>)}</ul>
    <ErrorNote message={error} retry={refresh} />
    {selected && <StageDialog key={selected.id} stage={selected} path={`${path}/${selected.id}`}
      onClose={() => { setIdentity(undefined); refresh(); }} onChange={refresh} />}
  </section>;
}
