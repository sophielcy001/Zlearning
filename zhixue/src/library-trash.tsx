import { useEffect, useState } from 'react';
import { FileText, RefreshCw } from 'lucide-react';
import { ApiError, api } from './api';
import { BackedStatus, Empty, ErrorNote, Modal, useAction, useRead } from './shared';

type TrashEntry = {
  id: string; fileId: string; name: string; originalPath: string; deletedAt: string; revision: string;
  projectTopic: string | null; state: 'deleting' | 'trashed' | 'restoring' | 'purging';
  error: string | null; available: boolean;
};
type WikiImpact = {
  sourceCount: number;
  sources: { id: string; name: string; path: string; lines: number }[];
};
type DeletePreview = {
  id: string; name: string; path: string; revision: string; projectTopic: string | null; wikiImpact: WikiImpact;
};
type SelectedFile = Pick<DeletePreview, 'id' | 'name'>;
type MovePlan = { file: SelectedFile; preview?: DeletePreview; error?: string; moved?: TrashEntry };
type PreviewResult = Pick<MovePlan, 'preview' | 'error'> & { id: string };
const dateTime = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' });
const pendingLabel = { deleting: '等待移入', restoring: '等待恢复', purging: '等待清理' };

function trashEndpoint(id: string) { return `/files/${encodeURIComponent(id)}/trash`; }

export function MoveToTrash({ files, onClose, onMoved, onPartial }: {
  files: SelectedFile[]; onClose: () => void; onMoved: (entryId: string) => void; onPartial?: (ids: string[]) => void;
}) {
  const [requested, setRequested] = useState<MovePlan[]>(() => files.map(file => ({ file })));
  const [plan, setPlan] = useState<MovePlan[]>();
  const [previewError, setPreviewError] = useState('');
  const action = useAction();
  useEffect(() => {
    const controller = new AbortController();
    const ids = requested.filter(entry => !entry.moved).map(entry => entry.file.id);
    void api<{ entries: PreviewResult[] }>('/files/trash/preview', 'POST', { ids }, controller.signal)
      .then(({ entries }) => {
        if (controller.signal.aborted) return;
        const previews = new Map(entries.map(({ id, ...result }) => [id, result]));
        setPlan(requested.map(entry => entry.moved ? entry : { file: entry.file, ...previews.get(entry.file.id) }));
      }).catch(error => {
        if (!controller.signal.aborted) setPreviewError(error instanceof Error ? error.message : '暂时无法预览。');
      });
    return () => controller.abort();
  }, [requested]);

  function previewAgain(entries: MovePlan[]) {
    setPlan(undefined);
    setPreviewError('');
    setRequested([...entries]);
  }
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
        {!entry.moved && !!entry.preview?.wikiImpact.sourceCount && <WikiImpactDetails impact={entry.preview.wikiImpact} />}
        {entry.moved && <small>已移入回收站</small>}
        <ErrorNote message={entry.error} />
      </div>)}</div>
      <p className="dialog-hint">文件正文在回收期间无法打开，学习记录和引用保留。</p>
      <ErrorNote message={action.error} />
      <div className="project-dialog-actions">
        <button className="button secondary" onClick={onClose} disabled={action.pending}>{plan.some(entry => entry.moved) ? '关闭' : '取消'}</button>
        {plan.some(entry => entry.error) && <button className="button secondary" disabled={action.pending}
          onClick={() => previewAgain(plan)}>重新预览未处理文件</button>}
        <button className="button primary" onClick={move} disabled={action.pending || !available}>
          {requested.length === 1 ? '确认移入回收站' : `确认移入 ${available} 个文件`}</button>
      </div>
    </div> : <BackedStatus error={previewError} retry={() => previewAgain(requested)} />}
  </Modal>;
}

function WikiImpactDetails({ impact }: { impact: WikiImpact }) {
  const remaining = impact.sourceCount - impact.sources.length;
  return <details className="trash-wiki-impact">
    <summary>{impact.sourceCount} 份文件通过双链引用此原件</summary>
    <ul>{impact.sources.map(source => <li key={source.id}>
      <span>{source.name}</span> · <span>{source.lines} 处原文位置</span>
      <small className="file-path">{source.path}</small>
    </li>)}</ul>
    {remaining > 0 && <small>另有 {remaining} 份引用文件</small>}
  </details>;
}

function TrashAction({ entry, mode, onClose, onRestored, onCleaned }: {
  entry: TrashEntry; mode: 'restore' | 'purge'; onClose: () => void; onRestored: (id: string) => void; onCleaned: () => void;
}) {
  const [path, setPath] = useState(entry.originalPath);
  const [review, setReview] = useState<{ confirmation: string; message: string }>();
  const action = useAction();
  const restoring = mode === 'restore';
  function confirm() {
    void action.run(async () => {
      try {
        await api(`/files/trash/${encodeURIComponent(entry.id)}/${mode}`, 'POST', restoring
          ? { revision: entry.revision, path, confirmation: review?.confirmation } : { revision: entry.revision });
      } catch (error) {
        if (restoring && error instanceof ApiError && error.status === 409 && error.payload.code === 'archive_identity' &&
            typeof error.payload.confirmation === 'string') {
          setReview({ confirmation: error.payload.confirmation, message: error.message });
          return;
        }
        setReview(undefined);
        throw error;
      }
      if (restoring) onRestored(entry.fileId);
      else onCleaned();
    });
  }
  return <Modal title={restoring ? '恢复文件' : '清理回收站原件'} onClose={onClose} busy={action.pending}>
    <div className="project-dialog-form">
      <div><strong>{entry.name}</strong><p className="file-path">{entry.originalPath}</p></div>
      {restoring ? <label className="project-field">恢复位置
        <input maxLength={1400} value={path} onChange={event => {
          setPath(event.target.value);
          setReview(undefined);
        }} disabled={action.pending} />
      </label> : <p className="dialog-hint">清理后无法从回收站恢复此原件。
        { /\.(pdf|png|jpe?g)$/i.test(entry.originalPath) ? '校对文字及其版本会一并清理。学习记录和引用保留。' : '已有学习记录、引用和版本历史保留。'}
      </p>}
      {review && <p className="dialog-hint" role="status">{review.message}</p>}
      <ErrorNote message={action.error} />
      <div className="project-dialog-actions">
        <button className="button secondary" onClick={onClose} disabled={action.pending}>取消</button>
        <button className={`button ${restoring ? 'primary' : 'danger'}`} onClick={confirm}
          disabled={action.pending || restoring && !path}>{restoring
            ? review ? '按核对版本恢复' : '确认恢复' : '确认清理'}</button>
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
