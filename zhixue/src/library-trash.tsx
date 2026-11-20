import { useEffect, useState } from 'react';
import { FileText, RefreshCw } from 'lucide-react';
import { api } from './api';
import { BackedStatus, Empty, ErrorNote, Modal, useAction, useRead } from './shared';

type TrashEntry = {
  id: string; fileId: string; name: string; originalPath: string; deletedAt: string; revision: string;
  projectTopic: string | null; state: 'deleting' | 'trashed' | 'restoring' | 'purging';
  error: string | null; available: boolean;
};
type DeletePreview = { id: string; name: string; path: string; revision: string; projectTopic: string | null };
type SelectedFile = Pick<DeletePreview, 'id' | 'name'>;
type MovePlan = { file: SelectedFile; preview?: DeletePreview; error?: string; moved?: TrashEntry };
const dateTime = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' });
const pendingLabel = { deleting: '等待移入', restoring: '等待恢复', purging: '等待清理' };

function trashEndpoint(id: string) { return `/files/${encodeURIComponent(id)}/trash`; }

export function MoveToTrash({ files, onClose, onMoved, onPartial }: {
  files: SelectedFile[]; onClose: () => void; onMoved: (entryId: string) => void; onPartial?: (ids: string[]) => void;
}) {
  const [requested, setRequested] = useState<MovePlan[]>(() => files.map(file => ({ file })));
  const [plan, setPlan] = useState<MovePlan[]>();
  const action = useAction();
  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      const result: MovePlan[] = [];
      for (const entry of requested) {
        if (controller.signal.aborted) return;
        if (entry.moved) { result.push(entry); continue; }
        try {
          const preview = await api<DeletePreview>(trashEndpoint(entry.file.id), 'GET', undefined, controller.signal);
          result.push({ file: entry.file, preview });
        } catch (error) {
          result.push({ file: entry.file, error: error instanceof Error ? error.message : '暂时无法预览。' });
        }
      }
      if (!controller.signal.aborted) setPlan(result);
    }
    void load();
    return () => controller.abort();
  }, [requested]);
  const available = plan?.filter(entry => entry.preview && !entry.moved && !entry.error).length ?? 0;
  function move() {
    if (!plan || !available) return;
    void action.run(async () => {
      const results: MovePlan[] = [];
      const processed: string[] = [];
      for (const entry of plan) {
        if (!entry.preview || entry.moved || entry.error) { results.push(entry); continue; }
        try {
          const moved = await api<TrashEntry>(trashEndpoint(entry.file.id), 'POST', {
            path: entry.preview.path, revision: entry.preview.revision,
          });
          processed.push(entry.file.id);
          results.push({ ...entry, moved });
        } catch (error) {
          results.push({ ...entry, error: error instanceof Error ? error.message : '暂时无法移入回收站。' });
        }
      }
      setPlan(results);
      if (results.every(entry => entry.moved)) onMoved(results.length === 1 ? results[0].moved!.id : '');
      else if (processed.length) onPartial?.(processed);
    });
  }
  return <Modal title="移入回收站" onClose={onClose} busy={action.pending}>
    {plan ? <div className="project-dialog-form">
      <div className="trash-move-plan">{plan.map(entry => <div key={entry.file.id}>
        <strong>{entry.preview?.name ?? entry.file.name}</strong>
        {entry.preview && <p className="file-path">{entry.preview.path}{entry.preview.projectTopic && ` · ${entry.preview.projectTopic}`}</p>}
        {entry.moved && <small>已移入回收站</small>}
        <ErrorNote message={entry.error} />
      </div>)}</div>
      <p className="dialog-hint">文件正文在回收期间无法打开，学习记录和引用保留。</p>
      <ErrorNote message={action.error} />
      <div className="project-dialog-actions">
        <button className="button secondary" onClick={onClose} disabled={action.pending}>{plan.some(entry => entry.moved) ? '关闭' : '取消'}</button>
        {plan.some(entry => entry.error) && <button className="button secondary" disabled={action.pending}
          onClick={() => { setRequested(plan); setPlan(undefined); }}>重新预览未处理文件</button>}
        <button className="button primary" onClick={move} disabled={action.pending || !available}>
          {requested.length === 1 ? '确认移入回收站' : `确认移入 ${available} 个文件`}</button>
      </div>
    </div> : <BackedStatus retry={() => setRequested([...requested])} />}
  </Modal>;
}

function TrashAction({ entry, mode, onClose, onRestored, onCleaned }: {
  entry: TrashEntry; mode: 'restore' | 'purge'; onClose: () => void; onRestored: (id: string) => void; onCleaned: () => void;
}) {
  const [path, setPath] = useState(entry.originalPath);
  const action = useAction();
  const restoring = mode === 'restore';
  function confirm() {
    void action.run(async () => {
      await api(`/files/trash/${encodeURIComponent(entry.id)}/${mode}`, 'POST', restoring
        ? { revision: entry.revision, path } : { revision: entry.revision });
      if (restoring) onRestored(entry.fileId);
      else onCleaned();
    });
  }
  return <Modal title={restoring ? '恢复文件' : '清理回收站原件'} onClose={onClose} busy={action.pending}>
    <div className="project-dialog-form">
      <div><strong>{entry.name}</strong><p className="file-path">{entry.originalPath}</p></div>
      {restoring ? <label className="project-field">恢复位置
        <input maxLength={1400} value={path} onChange={event => setPath(event.target.value)} disabled={action.pending} />
      </label> : <p className="dialog-hint">清理后无法从回收站恢复此原件。已有学习记录、引用和版本历史保留。</p>}
      <ErrorNote message={action.error} />
      <div className="project-dialog-actions">
        <button className="button secondary" onClick={onClose} disabled={action.pending}>取消</button>
        <button className={`button ${restoring ? 'primary' : 'danger'}`} onClick={confirm}
          disabled={action.pending || restoring && !path}>{restoring ? '确认恢复' : '确认清理'}</button>
      </div>
    </div>
  </Modal>;
}

export function TrashPanel({ selected, onRestored }: { selected: string; onRestored: (id: string) => void }) {
  const read = useRead<TrashEntry[]>('/files/trash');
  const [command, setCommand] = useState<{ entry: TrashEntry; mode: 'restore' | 'purge' }>();
  const [cleaned, setCleaned] = useState(false);
  const items = read.value;
  return <section className="panel library-list trash-panel">
    <div className="drawer-head">
      <h2>回收站</h2>
      <button className="icon-button" aria-label="刷新回收站" onClick={read.refresh}><RefreshCw size={16} /></button>
    </div>
    {cleaned && <p className="notice success" role="status">已清理回收站记录。</p>}
    {!items ? <BackedStatus error={read.error} retry={read.refresh} /> : items.length === 0
      ? <Empty title="回收站为空" /> : <div className="trash-entries">
        {items.map(entry => <article key={entry.id} className={`trash-entry ${entry.id === selected ? 'highlighted' : ''}`}>
          <div className="trash-details"><FileText size={18} /><div>
            <strong>{entry.name}</strong>
            <p className="file-path">{entry.originalPath}</p>
            <small>删除于 {dateTime.format(new Date(entry.deletedAt))}{entry.projectTopic && ` · ${entry.projectTopic}`}</small>
            {entry.state !== 'trashed' && <p className="file-status">{pendingLabel[entry.state]}</p>}
            {!entry.available && entry.state === 'trashed' && <p className="file-status">回收站原件缺失</p>}
            {entry.error && <p className="file-status" role="status">{entry.error}</p>}
          </div></div>
          <div className="action-row">
            <button className="button secondary" disabled={!entry.available || entry.state !== 'trashed'}
              onClick={() => setCommand({ entry, mode: 'restore' })}>恢复文件</button>
            <button className="button secondary" disabled={entry.state !== 'trashed'}
              onClick={() => setCommand({ entry, mode: 'purge' })}>手动清理</button>
          </div>
        </article>)}
      </div>}
    {items && <ErrorNote message={read.error} retry={read.refresh} />}
    {command && <TrashAction entry={command.entry} mode={command.mode} onClose={() => setCommand(undefined)}
      onRestored={onRestored} onCleaned={() => { setCommand(undefined); setCleaned(true); read.refresh(); }} />}
  </section>;
}
