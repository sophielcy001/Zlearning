import { useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { api } from './api';
import { BackupPermission, BackupRecipient } from './model-backup';
import { isMoneyHold } from './money-limit';
import { ErrorNote, useAction } from './shared';

type ModelConnection = { name: string; model: string; address: string };
type Criterion = { criterion: number; title: string; outcome: string };
type Locator = { evidenceIndex: number; firstLine: number; lastLine: number };
type Excerpt = Locator & { excerpt: number; name: string; text: string };
type ReviewScope = { connection: ModelConnection; backup?: ModelConnection; criteria: Criterion[]; excerpts: Excerpt[] };
type Preview = { scope: ReviewScope; approvalId: string; confirmation: string; maxCalls: number | null };
type State = 'queued' | 'running' | 'blocked' | 'stopped' | 'held' | 'complete';
type Body = { excerpts: Locator[]; maxCalls: number | null; allowBackup?: true };
type EvidenceText = { name: string; text: string };
type Draft = { id: string; evidenceIndex: string; firstLine: string; lastLine: string };
type SaveReview = (action: string, body?: object) => Promise<boolean>;

export type ModelReviewJob = {
  state: State; reason: string | null; message: string | null; scope: ReviewScope; maxCalls: number | null;
  calls: number; active: boolean; failoverReason: string | null; failoverMessage: string | null;
};

export const MODEL_REVIEW_STATES: Record<State, string> = {
  queued: '等待模型评审', running: '模型正在评审', blocked: '评审待处理', stopped: '已停止新增调用',
  held: '已收到评审结果', complete: '评审已完成',
};

function draft(locator?: Locator): Draft {
  return { id: crypto.randomUUID(), evidenceIndex: locator ? String(locator.evidenceIndex) : '',
    firstLine: String(locator?.firstLine ?? 1), lastLine: String(locator?.lastLine ?? 1) };
}

export function recordLines(first: number, last: number) {
  return first === last ? `第 ${first} 行` : `第 ${first}–${last} 行`;
}

function ScopeContent({ scope }: { scope: ReviewScope }) {
  return <>
    <div><strong>{scope.connection.name} · {scope.connection.model}</strong><p className="model-help">{scope.connection.address}</p></div>
    {scope.backup && <BackupRecipient connection={scope.backup} />}
    <ul className="stage-criteria">{scope.criteria.map(criterion => <li key={criterion.criterion}>
      <strong>{criterion.title}</strong><span>{criterion.outcome}</span>
    </li>)}</ul>
    {scope.excerpts.map(item => <div className="stage-evidence" key={item.excerpt}>
      <strong>{item.name} · {recordLines(item.firstLine, item.lastLine)}</strong>
      <pre tabIndex={0} aria-label={item.name}>{item.text}</pre>
    </div>)}
  </>;
}

export function AuthorizedScope({ job }: { job: ModelReviewJob }) {
  return <details className="stage-review-scope">
    <summary>查看已授权范围</summary>
    <div className="stage-form">
      <ScopeContent scope={job.scope} />
    </div>
  </details>;
}

function EvidenceRange({ item, evidence, position, removable, pending, change, remove }: {
  item: Draft; evidence: EvidenceText[]; position: number; removable: boolean; pending: boolean;
  change: (fields: Partial<Draft>) => void; remove: () => void;
}) {
  const [showLines, setShowLines] = useState(false);
  const lines = item.evidenceIndex === '' ? [] : evidence[Number(item.evidenceIndex)].text.split('\n');
  const anchor = Math.max(1, Math.min(Number(item.firstLine) || 1, lines.length) - 3);
  const visible = lines.slice(anchor - 1, anchor - 1 + 20);
  return <fieldset className="stage-fieldset stage-form" disabled={pending}>
    <legend>证据选段 {position}</legend>
    <label>证据文件<select value={item.evidenceIndex} required
      onChange={event => change({ evidenceIndex: event.target.value, firstLine: '1', lastLine: '1' })}>
      <option value="">选择本次提交的文件</option>
      {evidence.map((source, index) => <option value={index} key={index}>{index + 1}. {source.name}</option>)}
    </select></label>
    <div className="stage-line-inputs">
      <label>起始行<input type="number" min={1} max={lines.length || undefined} value={item.firstLine} required
        onChange={event => change({ firstLine: event.target.value })} /></label>
      <label>结束行<input type="number" min={Number(item.firstLine) || 1} max={lines.length || undefined}
        value={item.lastLine} required onChange={event => change({ lastLine: event.target.value })} /></label>
    </div>
    {lines.length > 0 && <div className="stage-evidence">
      <div className="action-row"><span className="model-help">保存文本共 {lines.length} 行</span>
        <button type="button" className="text-action" aria-expanded={showLines}
          onClick={() => setShowLines(value => !value)}>查看附近行号</button></div>
      {showLines && <pre aria-label="附近原文" tabIndex={0}>{visible.map((line, index) => `${anchor + index}  ${line}`).join('\n')}</pre>}
    </div>}
    {removable && <button type="button" className="text-action" onClick={remove}>移除选段 {position}</button>}
  </fieldset>;
}

function ReviewForm({ path, evidence, job, pending, save, onClose }: {
  path: string; evidence: EvidenceText[]; job?: ModelReviewJob | null; pending: boolean;
  save: SaveReview; onClose: () => void;
}) {
  const action = useAction();
  const [items, setItems] = useState<Draft[]>(() => job?.scope.excerpts.map(item => draft(item)) ?? [draft()]);
  const [limit, setLimit] = useState(() => job?.maxCalls == null ? '' : String(job.maxCalls));
  const [allowBackup, setAllowBackup] = useState(Boolean(job?.scope.backup));
  const [preview, setPreview] = useState<Preview>();
  const locked = pending || action.pending;
  const selections: Body = {
    ...(allowBackup ? { allowBackup: true } : {}), maxCalls: limit === '' ? null : Number(limit),
    excerpts: items.map(({ evidenceIndex, firstLine, lastLine }) => ({
      evidenceIndex: Number(evidenceIndex), firstLine: Number(firstLine), lastLine: Number(lastLine),
    })),
  };

  async function inspect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (items.some(item => item.evidenceIndex === '')) return;
    setPreview(await action.run(() => api<Preview>(path + '/preview', 'POST', selections)));
  }

  if (preview) {
    return <section aria-label="本次发送范围" className="stage-form stage-review-scope">
      <ScopeContent scope={preview.scope} />
      <p className="model-help">{preview.maxCalls === null ? '沿用全应用每日额度' : `本次评审最多 ${preview.maxCalls} 次调用，计入全应用额度`}</p>
      <div className="action-row">
        <button type="button" className="button secondary" disabled={locked} onClick={() => setPreview(undefined)}>修改选段</button>
        <button type="button" className="button primary" disabled={locked} onClick={() => {
          void save('', { ...selections, approvalId: preview.approvalId, confirmation: preview.confirmation }).then(saved => {
            if (saved) onClose();
          });
        }}>确认发送所选证据</button>
      </div>
    </section>;
  }
  return <form className="stage-form stage-inputs" onSubmit={event => void inspect(event)}>
    <p className="model-help">选段以本次提交保存的文字行号为准。可添加同一文件的多个选段。</p>
    {items.map((item, index) => <EvidenceRange key={item.id} item={item} evidence={evidence}
      position={index + 1} pending={locked} removable={items.length > 1}
      change={fields => setItems(current => current.map(row => row.id === item.id ? { ...row, ...fields } : row))}
      remove={() => setItems(current => current.filter(row => row.id !== item.id))} />)}
    <div className="action-row"><button type="button" className="button soft" disabled={locked || items.length >= 8}
      onClick={() => setItems(current => [...current, draft()])}>添加证据选段</button></div>
    <label>本次评审调用上限<input type="number" min={1} max={10000} value={limit} placeholder="留空则使用全应用额度"
      disabled={locked} onChange={event => setLimit(event.target.value)} /></label>
    <BackupPermission role="复核" checked={allowBackup} disabled={locked} onChange={setAllowBackup} />
    <ErrorNote message={action.error} />
    {action.error && <Link to="/settings/models" className="text-action">查看模型分工</Link>}
    <div className="action-row">
      <button type="button" className="button secondary" disabled={locked} onClick={onClose}>收起</button>
      <button className="button primary" disabled={locked || items.some(item => item.evidenceIndex === '')}>预览发送范围</button>
    </div>
  </form>;
}

export function StageModelReview({ path, evidence, job, pending, save }: {
  path: string; evidence: EvidenceText[]; job?: ModelReviewJob | null; pending: boolean; save: SaveReview;
}) {
  const [editing, setEditing] = useState(false);
  const canEdit = !job || !job.active && (job.state === 'stopped' || job.state === 'blocked');
  const recipient = job?.failoverReason && job.scope.backup ? job.scope.backup : job?.scope.connection;
  const stoppable = job && ['queued', 'running', 'blocked'].includes(job.state);
  return <section className="stage-review-box stage-form" aria-label="复核模型评审">
    {job && <>
      <div className="stage-status-line"><strong>{job.reason === 'restored' ? '评审待恢复' : MODEL_REVIEW_STATES[job.state]}</strong>
        <span className="model-help">{job.failoverReason && '备用 · '}{recipient?.name} · {recipient?.model}</span>
        <span className="model-help">已计入 {job.calls}{job.maxCalls !== null && ` / ${job.maxCalls}`} 次调用</span>
      </div>
      {job.failoverMessage && <p className="model-help">{job.failoverMessage}</p>}
      {job.state === 'blocked' && <ErrorNote message={job.message ?? '模型评审暂时需要处理。'} />}
      {(job.reason === 'daily_budget' || isMoneyHold(job.reason)) && <Link className="text-action" to="/settings/budget">查看额度恢复</Link>}
      {job.reason === 'configuration_changed' && <Link className="text-action" to="/settings/models">查看模型分工</Link>}
      {job.state === 'stopped' && job.active && <p className="model-help">等待已发出的请求返回。</p>}
      <AuthorizedScope job={job} />
      <div className="action-row">
        {stoppable && <button type="button" className="button secondary" disabled={pending}
          onClick={() => void save('/stop')}>停止模型评审</button>}
        {job.state === 'held' && <button type="button" className="button primary" disabled={pending}
          onClick={() => void save('/apply')}>采用已返回的评审</button>}
      </div>
    </>}
    {canEdit && (editing
      ? <ReviewForm path={path} evidence={evidence} job={job} pending={pending} save={save} onClose={() => setEditing(false)} />
      : <div className="action-row"><button type="button" className="button soft" disabled={pending}
        onClick={() => setEditing(true)}>{job ? '重新选择评审范围' : '用复核模型评审'}</button></div>)}
  </section>;
}
