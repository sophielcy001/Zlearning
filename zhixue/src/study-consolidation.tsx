import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, courseUrl } from './api';
import { BackupDestinations, BackupOptions, backupSelection, FailoverRecord, type ModelBackups, type ModelFailover } from './model-backup';
import { BackedStatus, ErrorNote, Modal, useAction, useRead } from './shared';
import type { Citation, Reviewer } from './verification';

type Scope = {
  main: Reviewer; review: Reviewer; backups?: ModelBackups; version: number; maxCalls: number | null; confirmation: string;
  pairLimit: number; quotesPerPair: number;
  sections: { index: number; title: string; citations: Citation[] }[];
  pairs: { id: string; question: string; answer: string }[];
};
type Phase = 'draft' | 'review' | 'publish';
type Job = {
  id: string; baseVersion: number; publishedVersion: number | null; phase: Phase; questionCount: number;
  publishedSection: number | null;
  state: 'queued' | 'running' | 'paused' | 'blocked' | 'complete' | 'no_changes';
  reason: string | null; error: string | null; reviewNote: string | null;
  draft: { title: string; text: string } | null; failovers?: ModelFailover[];
};
type Settings = { granted: boolean; authorized: boolean; calls: number; maxCalls: number | null; jobs: Job[]; backups?: ModelBackups };

const PHASES: Record<Phase, string> = { draft: '整理问答', review: '独立核验', publish: '写入课程版本' };
const STATES: Record<Job['state'], string> = {
  queued: '待处理', running: '处理中', paused: '已暂停', blocked: '待重试',
  complete: '已融入课程', no_changes: '本批没有可补充内容',
};

function ConsolidationConsent({ path, initial, initialBackups, close, saved }: {
  path: string; initial: number | null; initialBackups?: ModelBackups; close: () => void; saved: () => void;
}) {
  const [limit, setLimit] = useState(initial === null ? '' : String(initial));
  const [backups, setBackups] = useState(() => backupSelection(initialBackups));
  const [scope, setScope] = useState<Scope>();
  const action = useAction();
  return <Modal title="确认学完后答疑整理" onClose={close} busy={action.pending}>
    <div className="project-dialog-form">
      <form className="assessment-consent-form" onSubmit={event => {
        event.preventDefault();
        void action.run(async () => {
          setScope(undefined);
          setScope(await api<Scope>(`${path}/preview`, 'POST', { maxCalls: limit ? Number(limit) : null, ...backups }));
        });
      }}>
        <label className="project-field">本节整理调用次数上限
          <input type="number" min={1} max={10000} value={limit} placeholder="可留空" disabled={action.pending}
            onChange={event => { setLimit(event.target.value); setScope(undefined); }} />
        </label>
        <BackupOptions selection={backups} disabled={action.pending} onChange={value => {
          setBackups(value); setScope(undefined);
        }} />
        <button type="submit" className="button secondary" disabled={action.pending}>预览整理发送范围</button>
      </form>
      {scope && <>
        <div className="generation-destination"><strong>整理 · {scope.main.name} · {scope.main.model}</strong><code>{scope.main.address}</code></div>
        <div className="generation-destination"><strong>独立核验 · {scope.review.name} · {scope.review.model}</strong><code>{scope.review.address}</code></div>
        <BackupDestinations backups={scope.backups} />
        <p className="model-help">勾选「已学完」时处理本节当日已提出且有出处的问答，每批最多 {scope.pairLimit} 轮。主模型接收每轮最多 1,200 字的问题、2,000 字的答复和 {scope.quotesPerPair} 条原始引文。独立复核模型接收草稿和相关原始引文。调用计入每日额度。</p>
        <div className="assessment-scope">
          <details open>
            <summary>第 {scope.version} 版授权依据</summary>
            {scope.sections.map(section => <div className="consolidation-source" key={section.index}>
              <strong>{section.title}</strong>
              {section.citations.map((item, index) => <blockquote key={index}>{item.quote}</blockquote>)}
            </div>)}
          </details>
          {scope.pairs.map(item => <details key={item.id}>
            <summary>{item.question}</summary><p className="evidence-text">{item.answer}</p>
          </details>)}
        </div>
      </>}
      <ErrorNote message={action.error} />
      {action.error && <Link className="text-action" to="/settings/models">打开 API 与模型设置</Link>}
      <div className="project-dialog-actions">
        <button type="button" className="button secondary" disabled={action.pending} onClick={close}>取消</button>
        <button type="button" className="button primary" disabled={!scope || action.pending} onClick={() => {
          if (scope) void action.run(async () => {
            await api(`${path}/authorize`, 'POST', { maxCalls: scope.maxCalls, confirmation: scope.confirmation, ...backups });
            saved();
          });
        }}>确认并启用答疑整理</button>
      </div>
    </div>
  </Modal>;
}

export function LessonConsolidationPanel({ lesson, learned, version, onPublished }: {
  lesson: string; learned: boolean; version: number; onPublished: () => void;
}) {
  const path = `/lessons/${lesson}/consolidation`;
  const { value, error, refresh } = useRead<Settings>(path);
  const [open, setOpen] = useState(false);
  const applied = useRef(version);
  const action = useAction();
  const polling = learned && value?.jobs.some(job => job.state === 'queued' || job.state === 'running');
  const newest = Math.max(0, ...(value?.jobs.map(job => job.publishedVersion ?? 0) ?? []));
  useEffect(() => { refresh(); }, [learned, refresh]);
  useEffect(() => {
    if (!polling) return;
    const interval = window.setInterval(refresh, 850);
    return () => window.clearInterval(interval);
  }, [polling, refresh]);
  useEffect(() => {
    if (newest <= version || newest <= applied.current) return;
    applied.current = newest;
    onPublished();
  }, [newest, version, onPublished]);
  return <section className="consolidation-panel" aria-label="学完后的答疑整理">
    {value ? <>
      <div className="action-row">
        <button type="button" className="button secondary" onClick={() => setOpen(true)}>
          {value.authorized ? '调整整理范围与额度' : '启用学完后答疑整理'}
        </button>
        {value.granted && <button type="button" className="text-action" disabled={action.pending}
          onClick={() => void action.run(async () => { await api(`${path}/revoke`, 'POST'); refresh(); })}>撤销整理授权</button>}
      </div>
      {value.authorized && <p className="model-help">
        答疑整理已启用 · 已计入 {value.calls} 次调用{value.maxCalls === null ? '' : ` / 上限 ${value.maxCalls}`}
      </p>}
      <ErrorNote message={action.error || error} retry={error && !action.error ? refresh : undefined} />
      {value.jobs.map(job => <div className="consolidation-job panel" key={job.id}>
        <div className="action-row">
          <strong role="status">{STATES[job.state]}{job.state !== 'complete' && job.state !== 'no_changes' && ` · ${PHASES[job.phase]}`}</strong>
          <span className="model-help">{job.questionCount} 轮问答 · 来自课程第 {job.baseVersion} 版</span>
        </div>
        <FailoverRecord items={job.failovers} />
        <ErrorNote message={job.error ?? undefined} />
        {job.reviewNote && <p className="evidence-text">{job.reviewNote}</p>}
        {job.draft && <details className="section-verification"><summary>查看待处理草稿</summary>
          <strong>{job.draft.title}</strong><p className="evidence-text">{job.draft.text}</p>
        </details>}
        {job.state === 'blocked' && value.authorized && <button type="button" className="text-action" disabled={action.pending}
          onClick={() => void action.run(async () => { await api(`${path}/jobs/${job.id}/retry`, 'POST'); refresh(); })}>重试此阶段</button>}
        {job.publishedVersion !== null && <Link className="text-action" to={job.publishedSection === null
          ? `${courseUrl(lesson)}?readingVersion=${job.publishedVersion}`
          : `${courseUrl(lesson)}?readingVersion=${Math.max(version, newest)}&section=${job.publishedSection}`}>
          {job.publishedSection === null ? `查看课程第 ${job.publishedVersion} 版` : '定位新增段落'}
        </Link>}
      </div>)}
      {open && <ConsolidationConsent path={path} initial={value.maxCalls} initialBackups={value.backups} close={() => setOpen(false)}
        saved={() => { setOpen(false); refresh(); }} />}
    </> : <BackedStatus error={error} retry={refresh} />}
  </section>;
}
