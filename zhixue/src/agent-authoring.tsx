import { useEffect } from 'react';
import { Link } from 'react-router-dom';
import { api } from './api';
import { ErrorNote, useAction, useRead } from './shared';

export const AUTHORING = '/agent-authoring';
export type AgentSuggestion = { name: string; responsibility: string; purpose: string };
export type AuthoringJob = {
  id: string; purpose: string | null; state: 'queued' | 'running' | 'blocked' | 'ready' | 'failed' | 'cancelled';
  suggestion: AgentSuggestion | null; error: string | null;
};

export function useAgentAuthoring() {
  const resource = useRead<{ items: AuthoringJob[] }>(AUTHORING);
  const active = resource.value?.items.some(job => job.state === 'queued' || job.state === 'running' || job.state === 'blocked');
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(resource.refresh, 1200);
    return () => window.clearInterval(timer);
  }, [active, resource.refresh]);
  return resource;
}

const STATUS = { queued: '等待生成', running: '正在起草', blocked: '等待恢复额度', ready: '可编辑', failed: '生成失败', cancelled: '已取消' };

export function AgentDrafts({ drafts, adopt }: {
  drafts: ReturnType<typeof useAgentAuthoring>; adopt: (suggestion: AgentSuggestion) => void;
}) {
  const action = useAction();
  return <>
    <ErrorNote message={drafts.error} retry={drafts.refresh} />
    {!!drafts.value?.items.length && <section aria-label="AI 起草记录">
      <h3>AI 起草记录</h3>
      <ul className="skill-list">{drafts.value.items.map(job => <li key={job.id}>
        <div>
          <strong>{job.suggestion?.name ?? '专业 Agent 草稿'}</strong>
          {job.purpose && <p>{job.purpose}</p>}
          <p aria-live="polite">{STATUS[job.state]}</p>
          <ErrorNote message={job.error ?? undefined} />
          {job.state === 'blocked' && <Link className="text-action" to="/settings/budget">查看额度恢复</Link>}
        </div>
        <div className="action-row">
          {job.suggestion && <button type="button" className="button soft"
            onClick={() => { if (job.suggestion) adopt(job.suggestion); }}>使用草稿</button>}
          <button type="button" className="button soft" disabled={action.pending} onClick={() => void action.run(async () => {
            await api<AuthoringJob>(AUTHORING + '/' + job.id, 'DELETE');
            drafts.refresh();
          })}>{job.state === 'ready' || job.state === 'failed' ? '移除记录' : '取消起草'}</button>
        </div>
      </li>)}</ul>
      <ErrorNote message={action.error} />
    </section>}
  </>;
}
