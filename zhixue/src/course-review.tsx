import { useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from './api';
import { BackupPermission, BackupRecipient } from './model-backup';
import { BackedStatus, ErrorNote, Modal, useAction, useRead } from './shared';
import { sourceLocators, SourcePicker } from './source-picker';
import type { Reviewer, SourceExcerpt } from './verification';

type ReviewPlan = { reviewer: Reviewer | null; authorized: boolean; backup: Reviewer | null; sources: SourceExcerpt[] };
type ReviewPreview = { reviewer: Reviewer; backup: Reviewer | null; sources: SourceExcerpt[]; confirmation: string };

function SourceDialog({ endpoint, plan, close, saved }: {
  endpoint: string; plan: ReviewPlan; close: () => void; saved: () => void;
}) {
  const [sources, setSources] = useState(plan.sources);
  const [allowBackup, setAllowBackup] = useState(Boolean(plan.backup));
  const [preview, setPreview] = useState<ReviewPreview>();
  const action = useAction();

  return <Modal title="选择独立复核资料" onClose={close} busy={action.pending}>
    <div className="project-dialog-form">
      <SourcePicker sources={sources} disabled={action.pending} onChange={value => { setSources(value); setPreview(undefined); }} />
      <BackupPermission role="复核" checked={allowBackup} disabled={action.pending}
        onChange={checked => { setAllowBackup(checked); setPreview(undefined); }} />
      <button type="button" className="button secondary" disabled={action.pending || sources.length === 0}
        onClick={() => void action.run(async () => {
          setPreview(undefined);
          setPreview(await api<ReviewPreview>(`${endpoint}/preview`, 'POST', { sources: sourceLocators(sources), ...(allowBackup ? { allowBackup } : {}) }));
        })}>预览复核发送范围</button>
      {preview && <>
        <div className="generation-destination">
          <strong>{preview.reviewer.name} · {preview.reviewer.model}</strong><code>{preview.reviewer.address}</code>
        </div>
        {preview.backup && <BackupRecipient connection={preview.backup} />}
        <p className="model-help">复核会发送待核验草稿及以下资料片段。已授权的备用连接可接替独立复核，调用共用生成任务额度。</p>
        {preview.sources.map(source => <div className="file-anchor" key={source.id}>
          <strong>{source.title} · {source.label}</strong><pre>{source.excerpt}</pre>
        </div>)}
      </>}
      <ErrorNote message={action.error} />
      <div className="project-dialog-actions">
        <button type="button" className="button secondary" disabled={action.pending} onClick={close}>取消</button>
        <button type="button" className="button primary" disabled={action.pending || !preview} onClick={() => {
          if (preview) void action.run(async () => {
            await api(`${endpoint}/authorize`, 'POST', { confirmation: preview.confirmation, sources: sourceLocators(preview.sources), ...(allowBackup ? { allowBackup } : {}) });
            saved();
          });
        }}>确认并启用复核</button>
      </div>
    </div>
  </Modal>;
}

export function CourseReviewPanel({ project, onChanged }: { project: string; onChanged: () => void }) {
  const endpoint = `/projects/${project}/course-review`;
  const read = useRead<ReviewPlan>(endpoint);
  const [open, setOpen] = useState(false);
  const action = useAction();
  const plan = read.value;
  function update() {
    read.refresh();
    onChanged();
  }
  return <section className="panel generation-controls" aria-label="独立课程复核">
    <div className="generation-summary"><strong>独立课程复核</strong>
      {plan?.authorized && <span>已授权 {plan.sources.length} 处资料定位{plan.backup ? ` · 备用 ${plan.backup.model}` : ''}</span>}
    </div>
    {plan ? <div className="action-row">
      {plan.reviewer ? <button type="button" className="button secondary" onClick={() => setOpen(true)}>
        {plan.sources.length ? '更新复核资料' : '选择复核资料'}
      </button> : <Link className="text-action" to="/settings/models">配置独立复核模型</Link>}
      {plan.sources.length > 0 && <button type="button" className="text-action" disabled={action.pending}
        onClick={() => void action.run(async () => { await api(`${endpoint}/revoke`, 'POST'); update(); })}>撤销复核授权</button>}
    </div> : <BackedStatus error={read.error} retry={read.refresh} />}
    <ErrorNote message={action.error} />
    {open && plan && <SourceDialog endpoint={endpoint} plan={plan} close={() => setOpen(false)}
      saved={() => { setOpen(false); update(); }} />}
  </section>;
}
