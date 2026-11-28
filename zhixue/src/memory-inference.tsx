import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from './api';
import type { Consent } from './generation';
import type { AgentConfig } from './professional-agents';
import { ErrorNote, Modal, useAction, useRead } from './shared';
import type { Reviewer } from './verification';

const ENDPOINT = '/memory-inference';
const STATE = { queued: '等待整理', running: '正在整理', blocked: '等待恢复额度', ready: '整理完成', failed: '整理失败', cancelled: '已取消' };
type Job = { id: string; state: keyof typeof STATE; memoryId: string | null; error: string | null; automatic?: boolean };
type Preview = { text: string; agent: { id: string; name: string } | null; recipient: Reviewer; consent: Consent };
const active = (job: Job) => job.state === 'queued' || job.state === 'running' || job.state === 'blocked';

export function MemoryInferenceDialog({ sourceId, close, queued }: {
  sourceId: string; close: () => void; queued: () => void;
}) {
  const [id] = useState(() => crypto.randomUUID());
  const [agentId, setAgentId] = useState('');
  const [preview, setPreview] = useState<Preview>();
  const agents = useRead<{ items: AgentConfig[] }>('/agents');
  const action = useAction();
  const scope = { id, sourceMessageId: sourceId, agentId: agentId || null };

  async function inspect() {
    const result = await action.run(() => api<Preview>(ENDPOINT + '/preview', 'POST', scope));
    if (result) setPreview(result);
  }

  async function confirm() {
    if (!preview) return;
    const result = await action.run(() => api<Job>(ENDPOINT, 'POST', { ...scope, consent: preview.consent }));
    if (result) queued();
  }

  return <Modal title="AI 整理记忆" onClose={close} busy={action.pending}>
    <div className="project-dialog-form">
      {preview ? <>
        <div className="generation-destination">
          <strong>{preview.recipient.name} · {preview.recipient.model}</strong>
          <code>{preview.recipient.address}</code>
        </div>
        <dl className="generation-inputs">
          <div><dt>将发送的对话原话</dt><dd className="evidence-text">{preview.text}</dd></div>
          <div><dt>建议保存范围</dt><dd>{preview.agent ? 'Agent 专属 · ' + preview.agent.name : '共享记忆（待确认后生效）'}</dd></div>
        </dl>
        <p className="model-help">确认后调用主模型一次。生成的建议标为 AI 推断。</p>
        <div className="project-dialog-actions">
          <button type="button" className="button soft" disabled={action.pending} onClick={() => setPreview(undefined)}>返回选择范围</button>
          <button type="button" className="button primary" disabled={action.pending} onClick={() => void confirm()}>确认整理</button>
        </div>
      </> : <>
        <label className="project-field">可见范围
          <select value={agentId} disabled={action.pending} onChange={event => setAgentId(event.target.value)}>
            <option value="">共享记忆（待确认后生效）</option>
            {agents.value?.items.map(agent => <option key={agent.id} value={agent.id}>Agent 专属 · {agent.name}</option>)}
          </select>
        </label>
        <ErrorNote message={agents.error} retry={agents.refresh} />
        <div className="project-dialog-actions">
          <button type="button" className="button primary" disabled={action.pending} onClick={() => void inspect()}>预览发送范围</button>
        </div>
      </>}
      <ErrorNote message={action.error} />
    </div>
  </Modal>;
}

export function MemoryInferenceJobs({ changed }: { changed: () => void }) {
  const jobs = useRead<{ items: Job[] }>(ENDPOINT);
  const action = useAction();
  const live = jobs.value?.items.some(active);
  const saved = jobs.value?.items.filter(job => job.memoryId).map(job => job.memoryId).join(',');
  useEffect(() => {
    if (!live) return;
    const timer = window.setInterval(jobs.refresh, 1000);
    return () => window.clearInterval(timer);
  }, [live, jobs.refresh]);
  useEffect(() => {
    if (saved) changed();
  }, [saved, changed]);

  return <>
    <ErrorNote message={jobs.error} retry={jobs.refresh} />
    {!!jobs.value?.items.length && <section aria-label="AI 记忆整理记录">
      <h3>AI 记忆整理记录</h3>
      <ul className="skill-list">
        {jobs.value.items.map(job => <li key={job.id}>
          <div>
            <strong>{STATE[job.state]}</strong>
            {job.automatic && <p>Agent 自动整理</p>}
            {job.state === 'ready' && <p>{job.memoryId ? '已保存记忆建议' : '本次没有长期记忆建议'}</p>}
            <ErrorNote message={job.error ?? undefined} />
            {job.state === 'blocked' && <Link className="text-action" to="/settings/budget">查看额度恢复</Link>}
          </div>
          {active(job) && <button type="button" className="button soft" disabled={action.pending} onClick={() => void action.run(async () => {
            await api(ENDPOINT + '/' + job.id, 'DELETE');
            jobs.refresh();
          })}>取消整理</button>}
        </li>)}
      </ul>
      <ErrorNote message={action.error} />
    </section>}
  </>;
}