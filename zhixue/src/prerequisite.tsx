import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from './api';
import { EvidenceLinks } from './course-reading';
import { BackupDestinations, BackupOptions, backupSelection, FailoverRecord, type ModelBackups, type ModelFailover } from './model-backup';
import { BackedStatus, ErrorNote, Markdown, Modal, useAction, useRead } from './shared';
import { sourceLocators, SourcePicker } from './source-picker';
import type { Citation, Reviewer, SourceExcerpt } from './verification';

export type PrerequisiteSelection = { topic?: string; targetLessonId?: string };
type Section = { index: number; title: string; text: string; citations: Citation[] };
type Resource = {
  fileId: string; revision: string; body: string | null; sections: Section[];
  unverified?: number; reviewer?: Reviewer;
};
type Job = {
  id: string; state: 'queued' | 'running' | 'paused' | 'blocked' | 'stopped';
  phase: 'draft' | 'review' | 'publish'; error: string | null; inFlight: boolean;
  calls: number; maxCalls: number | null; sources: SourceExcerpt[];
  draft: Pick<Section, 'title' | 'text'>[] | null; backups?: ModelBackups; failovers?: ModelFailover[];
};
type Lookup = { topic: string } & (
  { kind: 'course' | 'explanation'; resource: Resource }
  | { kind: 'task'; job: Job }
  | { kind: 'missing'; suggestedSources: SourceExcerpt[]; previousUnavailable: boolean }
);
type Preview = { main: Reviewer; review: Reviewer; backups?: ModelBackups; topic: string; confirmation: string; sources: SourceExcerpt[] };

const PHASES = { draft: '起草讲解', review: '独立核验', publish: '写入知识库' };
const STATES: Record<Job['state'], string> = {
  queued: '等待中', running: '处理中', paused: '项目已暂停', blocked: '需要处理', stopped: '已停止',
};

function PrerequisiteScope({ path, selection, initial, job, saved }: {
  path: string; selection: PrerequisiteSelection; initial: SourceExcerpt[]; job?: Job; saved: () => void;
}) {
  const [sources, setSources] = useState(initial);
  const [limit, setLimit] = useState(job?.maxCalls == null ? '' : String(job.maxCalls));
  const [backups, setBackups] = useState(() => backupSelection(job?.backups));
  const [preview, setPreview] = useState<Preview>();
  const action = useAction();

  function payload(items: SourceExcerpt[]) {
    return { ...selection, sources: sourceLocators(items), maxCalls: limit ? Number(limit) : null, resumeJobId: job?.id, ...backups };
  }
  return <div className="project-dialog-form">
    <SourcePicker sources={sources} disabled={action.pending} allowImport
      onChange={items => { setSources(items); setPreview(undefined); }} />
    <form className="generation-preview-form" onSubmit={event => {
      event.preventDefault();
      void action.run(async () => {
        setPreview(undefined);
        setPreview(await api<Preview>(`${path}/preview`, 'POST', payload(sources)));
      });
    }}>
      <label className="project-field">本次讲解调用次数上限
        <input type="number" min={1} max={10000} value={limit} placeholder="可留空" disabled={action.pending}
          onChange={event => { setLimit(event.target.value); setPreview(undefined); }} />
      </label>
      <BackupOptions selection={backups} disabled={action.pending} onChange={value => {
        setBackups(value); setPreview(undefined);
      }} />
      <button type="submit" className="button secondary" disabled={action.pending || !sources.length}>预览前置讲解发送范围</button>
    </form>
    {preview && <>
      <div className="generation-destination"><strong>起草 · {preview.main.name} · {preview.main.model}</strong><code>{preview.main.address}</code></div>
      <div className="generation-destination"><strong>独立核验 · {preview.review.name} · {preview.review.model}</strong><code>{preview.review.address}</code></div>
      <BackupDestinations backups={preview.backups} />
      <p>主题 · {preview.topic}</p>
      <p className="model-help">主模型接收主题和下列资料片段。独立复核模型接收草稿及这些片段。调用计入每日额度，通过核验的讲解保存到知识库。</p>
      {preview.sources.map(source => <div className="file-anchor" key={source.id}>
        <strong>{source.title} · {source.label}</strong><pre>{source.excerpt}</pre>
      </div>)}
      <button type="button" className="button primary" disabled={action.pending} onClick={() => void action.run(async () => {
        await api(`${path}/generate`, 'POST', { ...payload(preview.sources), confirmation: preview.confirmation });
        saved();
      })}>{job ? '确认并继续讲解任务' : '确认并生成前置讲解'}</button>
    </>}
    <ErrorNote message={action.error} />
    {action.error && <Link className="text-action" to="/settings/models">打开 API 与模型设置</Link>}
  </div>;
}

function PrerequisiteContent({ lesson, selection }: { lesson: string; selection: PrerequisiteSelection }) {
  const endpoint = `/lessons/${lesson}/prerequisites`;
  const params = new URLSearchParams(selection.targetLessonId ? { targetLessonId: selection.targetLessonId } : { topic: selection.topic ?? '' });
  const { value, error, refresh } = useRead<Lookup>(`${endpoint}?${params}`);
  const action = useAction();
  const [edit, setEdit] = useState(false);
  const job = value?.kind === 'task' ? value.job : undefined;
  const pollMs = job?.state === 'paused' ? 2500 : job && (job.inFlight || job.state === 'queued' || job.state === 'running') ? 800 : 0;
  useEffect(() => {
    if (!pollMs) return;
    const timer = window.setInterval(refresh, pollMs);
    return () => window.clearInterval(timer);
  }, [pollMs, refresh]);
  function change(suffix: 'retry' | 'stop') {
    if (job) void action.run(async () => {
      await api(`${endpoint}/jobs/${job.id}/${suffix}`, 'POST');
      refresh();
    });
  }
  if (!value) return <BackedStatus error={error} retry={refresh} />;
  return <div className="prerequisite-content">
    <h3>{value.topic}</h3>
    <ErrorNote message={error || action.error} retry={error ? refresh : undefined} />
    {(value.kind === 'course' || value.kind === 'explanation') && <>
      <p className="model-help">{value.kind === 'course' ? '来自已有课程' : '已保存到知识库'}
        {value.resource.reviewer && ` · 独立复核 ${value.resource.reviewer.name}`}</p>
      {!!value.resource.unverified && <p className="model-help">{value.resource.unverified} 个小节尚缺可用核验依据。</p>}
      {value.resource.body && <Markdown>{value.resource.body.replace(/^# [^\n]*\n+/, '')}</Markdown>}
      {value.resource.sections.map(section => <section className="prerequisite-section" key={section.index}>
        <h4>{section.title}</h4><p className="evidence-text">{section.text}</p>
        <details className="section-verification"><summary>核验依据</summary><EvidenceLinks entries={section.citations} /></details>
      </section>)}
      <Link className="text-action" to={`/library?${new URLSearchParams({ file: value.resource.fileId, revision: value.resource.revision })}`}>在知识库打开</Link>
    </>}
    {job && <div className="consolidation-job panel">
      <strong role="status">{STATES[job.state]} · {PHASES[job.phase]}</strong>
      <p className="model-help">已计入 {job.calls} 次调用{job.maxCalls === null ? '' : ` / 上限 ${job.maxCalls}`}</p>
      <FailoverRecord items={job.failovers} />
      <ErrorNote message={job.error ?? undefined} />
      {job.draft && <details className="section-verification"><summary>查看待处理草稿</summary>
        {job.draft.map((section, index) => <section className="prerequisite-section" key={index}>
          <h4>{section.title}</h4><p className="evidence-text">{section.text}</p>
        </section>)}
      </details>}
      <div className="action-row">
        {(job.state === 'queued' || job.state === 'running' || job.state === 'paused' || job.state === 'blocked') && <button type="button" className="text-action"
          disabled={action.pending} onClick={() => change('stop')}>停止讲解任务</button>}
        {(job.state === 'blocked' || job.state === 'stopped') && !job.inFlight && <>
          <button type="button" className="button secondary" disabled={action.pending} onClick={() => change('retry')}>继续此阶段</button>
          <button type="button" className="text-action" disabled={action.pending} onClick={() => setEdit(!edit)}>修改资料与额度</button>
        </>}
      </div>
    </div>}
    {value.kind === 'missing' && <>
      <p className="model-help">{value.previousUnavailable ? '已保存的讲解需要重新核验。请选择要用于本次讲解的资料。'
        : '请选择适用的资料片段以生成临时讲解。'}</p>
      <PrerequisiteScope path={endpoint} selection={selection} initial={value.suggestedSources} saved={refresh} />
    </>}
    {edit && job && (job.state === 'blocked' || job.state === 'stopped') && <PrerequisiteScope
      path={endpoint} selection={selection} initial={job.sources} job={job} saved={() => { setEdit(false); refresh(); }} />}
  </div>;
}

export function PrerequisiteDialog({ lesson, request, close }: { lesson: string; request: PrerequisiteSelection; close: () => void }) {
  const [text, setText] = useState(request.topic ?? '');
  const [selection, setSelection] = useState<PrerequisiteSelection | undefined>(request.topic || request.targetLessonId ? request : undefined);
  return <Modal title="前置知识" onClose={close}>
    <div className="project-dialog-form">
      {!request.targetLessonId && <form className="generation-preview-form" onSubmit={event => {
        event.preventDefault();
        if (text.trim()) setSelection({ topic: text.trim() });
      }}>
        <label className="project-field">前置知识主题
          <input value={text} maxLength={160} onChange={event => setText(event.target.value)} />
        </label>
        <button type="submit" className="button secondary" disabled={!text.trim()}>查找讲解</button>
      </form>}
      {selection && <PrerequisiteContent key={selection.targetLessonId ?? selection.topic} lesson={lesson} selection={selection} />}
      <div className="project-dialog-actions"><button type="button" className="button secondary" onClick={close}>返回原阅读处</button></div>
    </div>
  </Modal>;
}
