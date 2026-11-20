import { useEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronLeft, ChevronRight, X } from 'lucide-react';
import { Link } from 'react-router-dom';
import { projectUrl, TASK_NAMES, type TaskList, type TaskSummary } from './api';
import { GenerationControls, LocalGenerationControls, hasGenerationWork, type Study } from './generation';
import { BackedStatus, Empty, ErrorNote, useRead } from './shared';

function ModelTaskActions({ item, changed }: { item: TaskSummary; changed: () => void }) {
  const { value, error, refresh } = useRead<Study>(`/projects/${item.id}/generation`);
  const working = item.status === 'queued' || hasGenerationWork(value);
  useEffect(() => {
    refresh();
  }, [item.status, item.completed, item.paused, item.error, refresh]);
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') refresh();
    }, working ? 1000 : 3000);
    return () => window.clearInterval(timer);
  }, [working, refresh]);
  if (!value) return <BackedStatus error={error} retry={refresh} />;
  return <>
    <ErrorNote message={error} retry={refresh} />
    <GenerationControls project={item.id} value={value} changed={() => { refresh(); changed(); }} />
  </>;
}

export default function TaskDrawer({ onClose, onChanged, queuedCount, taskCount }: {
  onClose: () => void; onChanged: (project: string) => void; queuedCount: number; taskCount: number;
}) {
  const [page, setPage] = useState(1);
  const [expanded, setExpanded] = useState<string>();
  const closeButton = useRef<HTMLButtonElement>(null);
  const { value, error, refresh } = useRead<TaskList>(`/tasks?page=${page}`);
  const working = Boolean(queuedCount || value?.items.some(item => item.status === 'queued'));
  useEffect(() => { closeButton.current?.focus(); }, []);
  useEffect(() => { refresh(); }, [taskCount, refresh]);
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') refresh();
    }, working ? 1000 : 3000);
    return () => window.clearInterval(timer);
  }, [working, refresh]);
  function showPage(next: number) {
    setExpanded(undefined);
    setPage(next);
  }
  return <aside id="background-tasks" className="panel task-drawer" aria-label="后台任务列表">
    <div className="drawer-head">
      <h2>后台任务</h2>
      <button ref={closeButton} type="button" className="icon-button" aria-label="关闭任务列表" onClick={onClose}><X size={19} /></button>
    </div>
    {value ? <>
      <ErrorNote message={error} retry={refresh} />
      <div className="drawer-list">{value.items.length ? value.items.map(item => {
        const open = expanded === item.id;
        const state = open && item.generationMode === 'model' ? '' : TASK_NAMES[item.status];
        const progress = item.total > 0 ? `${item.completed} / ${item.total} 节已就绪` : '';
        const summary = [state, progress].filter(Boolean).join(' · ');
        const controls = `background-task-${item.id}`;
        function changed() { refresh(); onChanged(item.id); }
        return <article className="drawer-task" key={item.id}>
          <div className="drawer-task-head">
            <Link to={projectUrl(item.id)} onClick={onClose}><strong>{item.topic}</strong></Link>
            <button type="button" className="text-action drawer-toggle" aria-label={`管理任务 ${item.topic}`}
              aria-expanded={open} aria-controls={open ? controls : undefined} onClick={() => setExpanded(open ? undefined : item.id)}>
              管理 <ChevronDown size={15} aria-hidden="true" />
            </button>
          </div>
          {summary && <span className="drawer-task-status">{summary}</span>}
          {!open && (item.error || (item.generationMode === 'model' && item.paused)) && <p className="drawer-task-error">
            {item.generationMode === 'model' && item.paused ? '请先恢复学习项目。' : item.error}
          </p>}
          {open && <div className="drawer-actions" id={controls}>
            {item.generationMode === 'model'
              ? <ModelTaskActions item={item} changed={changed} />
              : <LocalGenerationControls project={item.id} task={item} changed={changed} />}
          </div>}
        </article>;
      }) : <Empty title="没有待处理任务" />}</div>
      {value.lastPage > 1 && <div className="pager">
        <span>共 {value.total} 项</span>
        <button type="button" className="icon-button" aria-label="任务上一页" disabled={value.page === 1} onClick={() => showPage(value.page - 1)}><ChevronLeft size={17} /></button>
        <span>第 {value.page} / {value.lastPage} 页</span>
        <button type="button" className="icon-button" aria-label="任务下一页" disabled={value.page === value.lastPage} onClick={() => showPage(value.page + 1)}><ChevronRight size={17} /></button>
      </div>}
    </> : <BackedStatus error={error} retry={refresh} />}
  </aside>;
}
