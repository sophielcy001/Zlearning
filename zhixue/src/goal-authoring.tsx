import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { Sparkles } from 'lucide-react';
import { Link } from 'react-router-dom';
import { api } from './api';
import { GOAL_ORIGINS, type Evidence, type GoalDraft } from './conversation';
import type { Consent } from './generation';
import { ErrorNote, Modal, useAction, useRead } from './shared';

export type GoalChanges = { goalId: string; revision: number; values: Record<string, string>; removeEvidence: string[] };
type ApprovedEdits = Pick<GoalChanges, 'values' | 'removeEvidence'>;
type GoalJob = {
  id: string;
  state: 'queued' | 'running' | 'blocked' | 'ready' | 'applied' | 'failed' | 'cancelled';
  error: string | null;
  suggestion: Pick<GoalDraft, 'fields' | 'question'> | null;
  approvedEdits: ApprovedEdits | null;
};
type GoalDisclosure = {
  recipient: { name: string; model: string; address: string };
  consent: Consent;
  material: {
    fields: { key: string; label: string; value: string }[];
    sources: { key: string; label: string; text: string; answer?: string }[];
  };
  unavailable: string[];
};

function GoalScopeDialog({ endpoint, changes, hasFiles, close, queued }: {
  endpoint: string; changes: GoalChanges; hasFiles: boolean; close: () => void; queued: (job: GoalJob) => void;
}) {
  const [id] = useState(() => crypto.randomUUID());
  const [withExcerpts, setExcerpts] = useState(false);
  const [disclosure, setDisclosure] = useState<GoalDisclosure>();
  const action = useAction();
  const request = { ...changes, id, withExcerpts };

  async function preview() {
    setDisclosure(undefined);
    const found = await action.run(() => api<GoalDisclosure>(`${endpoint}/preview`, 'POST', request));
    if (found) setDisclosure(found);
  }

  async function confirm() {
    if (!disclosure) return;
    const job = await action.run(() => api<GoalJob>(endpoint, 'POST', { ...request, consent: disclosure.consent }));
    if (job) queued(job);
  }

  return <Modal title="确认目标整理范围" onClose={close} busy={action.pending}>
    <div className="project-dialog-form">
      {hasFiles && <label className="project-check"><input type="checkbox" checked={withExcerpts} disabled={action.pending}
        onChange={event => { setExcerpts(event.target.checked); setDisclosure(undefined); }} />附带资料片段</label>}
      <div className="action-row">
        <button type="button" className="button secondary" disabled={action.pending} onClick={() => void preview()}>查看整理范围</button>
      </div>
      {disclosure && <>
        <div className="generation-destination">
          <strong>{disclosure.recipient.name} · {disclosure.recipient.model}</strong>
          <code>{disclosure.recipient.address}</code>
        </div>
        {disclosure.material.fields.length > 0 && <dl className="generation-inputs" aria-label="将发送的目标字段">
          {disclosure.material.fields.map(field => <div key={field.key}>
            <dt>{field.label}</dt><dd className="evidence-text">{field.value}</dd>
          </div>)}
        </dl>}
        {disclosure.material.sources.length > 0 && <dl className="generation-inputs" aria-label="将发送的学习依据">
          {disclosure.material.sources.map(source => <div key={source.key}>
            <dt className="evidence-text">{source.label}</dt><dd className="evidence-text">
              {source.text}{source.answer && <p><strong>智学回复</strong><br /><span>{source.answer}</span></p>}
            </dd>
          </div>)}
        </dl>}
        {disclosure.unavailable.length > 0 && <p className="model-help">暂无可用片段的资料包括{disclosure.unavailable.join('、')}。</p>}
        <p className="model-help">确认后调用主模型一次，返回可编辑草稿。</p>
      </>}
      <ErrorNote message={action.error} />
      {action.error.includes('管理中心配置') && <Link className="text-action" to="/settings/models">打开 API 与模型设置</Link>}
      <div className="project-dialog-actions">
        <button type="button" className="button secondary" disabled={action.pending} onClick={close}>取消</button>
        <button type="button" className="button primary" disabled={!disclosure || action.pending}
          onClick={() => void confirm()}>确认整理目标</button>
      </div>
    </div>
  </Modal>;
}

export function useGoalAuthoring(endpoint: string) {
  const { value, error, refresh } = useRead<{ job: GoalJob | null }>(endpoint);
  const [confirmed, setConfirmed] = useState<GoalJob>();
  const remote = value?.job;
  const job = confirmed && confirmed.id !== remote?.id ? confirmed : remote;
  const active = job?.state === 'queued' || job?.state === 'running' || job?.state === 'blocked';

  useEffect(() => {
    if (confirmed?.id && confirmed.id === remote?.id) setConfirmed(undefined);
  }, [confirmed?.id, remote?.id]);
  useEffect(() => {
    window.addEventListener('focus', refresh);
    return () => window.removeEventListener('focus', refresh);
  }, [refresh]);
  useEffect(() => {
    if (!active) return;
    const poll = window.setInterval(refresh, job?.state === 'blocked' ? 2500 : 900);
    return () => window.clearInterval(poll);
  }, [active, job?.state, refresh]);

  return { endpoint, job, active, error, refresh, markQueued: (job: GoalJob) => { setConfirmed(job); refresh(); } };
}

export function GoalAuthoring({ authoring, changes, evidence, busy, adopt }: {
  authoring: ReturnType<typeof useGoalAuthoring>; changes: GoalChanges; evidence: Evidence[];
  busy: boolean; adopt: (id: string, approved: ApprovedEdits | null) => Promise<boolean>;
}) {
  const { endpoint, job, active, error, refresh, markQueued } = authoring;
  const [open, setOpen] = useState(false);
  const action = useAction();
  const suggestion = job?.state === 'ready' ? job.suggestion : null;
  const status = job?.state === 'queued' ? '目标整理已排队' : job?.state === 'running' ? '正在整理学习目标' : null;
  const hasFiles = evidence.some(source => source.kind === 'file' && !changes.removeEvidence.includes(source.id));
  const removed = evidence.filter(source => job?.approvedEdits?.removeEvidence.includes(source.id));
  const disabled = action.pending || busy;

  async function discard(id: string) {
    const result = await action.run(() => api<GoalJob>(`${endpoint}/${id}`, 'DELETE'));
    if (result) refresh();
  }

  async function apply(job: GoalJob) {
    if (await adopt(job.id, job.approvedEdits)) refresh();
  }

  return <section className="goal-authoring" aria-label="模型目标整理">
    <ErrorNote message={error} retry={refresh} />
    <ErrorNote message={action.error || job?.error || undefined} />
    {job?.state === 'blocked' && <Link className="text-action" to="/settings/budget">打开额度恢复</Link>}
    {status && <p className="model-help" role="status">{status}</p>}
    {active && <p className="model-help">移除依据会撤回本次整理。取消整理后可改写目标字段。</p>}
    {suggestion && <>
      <h3>主模型草稿</h3>
      <dl className="generation-inputs" aria-label="模型草稿字段">
        {suggestion.fields.filter(field => field.value).map(field => <div key={field.key}>
          <dt>{field.label}<small>{GOAL_ORIGINS[field.origin]}</small></dt>
          <dd className="evidence-text">{field.value}</dd>
        </div>)}
      </dl>
      {removed.length > 0 && <p className="model-help">采用草稿时会移除{removed.map(source => `「${source.label}」`).join('、')}。</p>}
      {suggestion.question && <p className="goal-question">{suggestion.question}</p>}
    </>}
    <div className="action-row">
      {!active && !suggestion && <button type="button" className="button soft" disabled={disabled} onClick={() => setOpen(true)}>
        <Sparkles size={16} />用主模型整理目标
      </button>}
      {job && suggestion && <button type="button" className="button primary" disabled={disabled}
        onClick={() => void apply(job)}>采用目标草稿</button>}
      {job && (active || suggestion) && <button type="button" className="button secondary" disabled={disabled}
        onClick={() => void discard(job.id)}>{suggestion ? '忽略草稿' : '取消整理'}</button>}
    </div>
    {open && createPortal(<GoalScopeDialog endpoint={endpoint} changes={changes} hasFiles={hasFiles}
      close={() => setOpen(false)} queued={queued => { markQueued(queued); setOpen(false); }} />, document.body)}
  </section>;
}
