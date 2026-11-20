import { useId, useState } from 'react';
import { api, apiResponse, type LibraryTag } from './api';
import { ErrorNote, Modal, useAction } from './shared';
import { LIBRARY_ROOT, type LibraryFolder } from './library-folders';

export const LIBRARY_BATCH_LIMIT = 100;
export type SelectedLibraryFile = { id: string; name: string; tags?: LibraryTag[] };
export type BatchFileAction = 'move' | 'copy' | 'export';
type Signature = { id: string; source: string; target: string; token: string; copyId?: string };
type PlannedFile = (Signature | { id: string; error: string }) & { name: string | null };
type FileOutcome = { id: string; path?: string; error?: string; pending?: boolean };
const ACTION_LABELS = { move: '移动', copy: '复制', export: '导出' };
const ACTION_HINTS = {
  move: '预览列出每份文件的目标位置。移动后沿用文件引用。',
  copy: '每份副本以独立资料保存，沿用标签。原有课程引用继续指向原件。',
  export: '文件原件将打包为 ZIP。预览列出压缩包中的文件名。',
};

export function downloadFile(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 2000);
}

export function FileTagDialog({ files, onClose, onDone }: {
  files: SelectedLibraryFile[];
  onClose: () => void;
  onDone: () => void;
}) {
  const [mode, setMode] = useState<'add' | 'remove'>('add');
  const [name, setName] = useState('');
  const choices = useId();
  const action = useAction();
  const tags = Array.from(new Map(files.flatMap(file => (file.tags ?? []).map(tag => [tag.key, tag]))).values());

  function save() {
    if (!name.trim()) return;
    void action.run(async () => {
      await api('/files/tags', 'POST', { ids: files.map(file => file.id), tag: name, action: mode });
      onDone();
    });
  }

  return <Modal title={files.length > 1 ? '批量编辑标签' : '编辑文件标签'} onClose={onClose} busy={action.pending}>
    <form className="project-dialog-form" onSubmit={event => { event.preventDefault(); save(); }}>
      <p className="dialog-hint">{files.length > 1 ? `将处理 ${files.length} 个选中的文件。` : files[0]?.name}</p>
      {tags.length > 0 && <ul className="file-tags" aria-label="选中文件的标签">
        {tags.map(tag => <li key={tag.key}>{tag.name}</li>)}
      </ul>}
      <label className="project-field">标签操作
        <select value={mode} disabled={action.pending} onChange={event => setMode(event.target.value === 'add' ? 'add' : 'remove')}>
          <option value="add">添加标签</option>
          <option value="remove">移除标签</option>
        </select>
      </label>
      <label className="project-field">标签名称
        <input list={choices} value={name} maxLength={80} disabled={action.pending}
          onChange={event => setName(event.target.value)} />
      </label>
      <datalist id={choices}>{tags.map(tag => <option key={tag.key} value={tag.name} />)}</datalist>
      <ErrorNote message={action.error} />
      <div className="project-dialog-actions">
        <button type="button" className="button secondary" onClick={onClose} disabled={action.pending}>取消</button>
        <button type="submit" className="button primary" disabled={action.pending || !name.trim()}>保存标签</button>
      </div>
    </form>
  </Modal>;
}

export function FileBatchDialog({ mode, files, folders, onClose, onProcessed }: {
  mode: BatchFileAction;
  files: SelectedLibraryFile[];
  folders: LibraryFolder[];
  onClose: () => void;
  onProcessed: (ids: string[]) => void;
}) {
  const [parent, setParent] = useState(LIBRARY_ROOT);
  const [plan, setPlan] = useState<PlannedFile[]>();
  const [outcomes, setOutcomes] = useState<Record<string, FileOutcome>>({});
  const [downloaded, setDownloaded] = useState(false);
  const action = useAction();
  const writesFiles = mode !== 'export';
  const verb = ACTION_LABELS[mode];
  const remainder = files.filter(file => !outcomes[file.id]?.path);
  const ready = plan?.filter((entry): entry is PlannedFile & Signature => {
    const result = outcomes[entry.id];
    return 'token' in entry && !result?.path && (!result?.error || !!result.pending);
  }) ?? [];
  const hasPending = ready.some(entry => outcomes[entry.id]?.pending);
  const plansByFile = new Map(plan?.map(entry => [entry.id, entry]));
  const request = { action: mode, parent: writesFiles ? parent : undefined };

  function inspect() {
    void action.run(async () => {
      const next = await api<{ entries: PlannedFile[] }>('/files/batch/preview', 'POST', {
        ...request, ids: remainder.map(file => file.id),
      });
      setPlan(next.entries);
      setOutcomes(current => Object.fromEntries(Object.entries(current).filter(([, item]) => item.path)));
    });
  }

  function confirm() {
    void action.run(async () => {
      const data = { ...request, files: ready.map(({ id, source, target, token, copyId }) => ({ id, source, target, token, copyId })) };
      if (writesFiles) {
        const result = await api<{ entries: FileOutcome[] }>('/files/batch/confirm', 'POST', data);
        setOutcomes(current => ({ ...current, ...Object.fromEntries(result.entries.map(item => [item.id, item])) }));
        onProcessed(result.entries.filter(item => item.path).map(item => item.id));
      } else {
        try {
          const response = await apiResponse('/files/batch/confirm', 'POST', JSON.stringify(data), 'application/json');
          downloadFile(await response.blob(), '智学知识库文件.zip');
          setDownloaded(true);
        } catch (error) {
          setPlan(undefined);
          throw error;
        }
      }
    });
  }

  const canConfirm = ready.length > 0 && !downloaded;
  return <Modal title={`批量${verb}文件`} onClose={onClose} busy={action.pending}>
    <div className="project-dialog-form">
      {writesFiles && <label className="project-field">目标文件夹
        <select value={parent} onChange={event => { setParent(event.target.value); setPlan(undefined); }}
          disabled={action.pending || !remainder.length || hasPending}>
          {folders.map(folder => <option key={folder.path} value={folder.path}>{folder.path === LIBRARY_ROOT
            ? folder.name : folder.path.slice(LIBRARY_ROOT.length + 1)}</option>)}
        </select>
      </label>}
      <ul className="file-batch-plan" aria-label={`${verb}文件清单`}>{files.map(file => {
        const entry = plansByFile.get(file.id);
        const result = outcomes[file.id];
        const error = result?.error || (entry && 'error' in entry ? entry.error : null);
        const route = result?.path || (entry && 'target' in entry ? entry.target : null);
        return <li key={file.id}>
          <strong>{entry?.name || file.name}</strong>
          {route && <p className="file-path">{route}</p>}
          {result?.path && <small>已{verb}</small>}
          {error && <p className="file-status" role="status">{error}</p>}
        </li>;
      })}</ul>
      <p className="dialog-hint">{ACTION_HINTS[mode]}</p>
      {downloaded && <p className="notice success" role="status">ZIP 下载已开始。</p>}
      <ErrorNote message={action.error} />
      <div className="project-dialog-actions">
        <button type="button" className="button secondary" onClick={onClose} disabled={action.pending}>
          {files.length > remainder.length || downloaded ? '关闭' : '取消'}
        </button>
        {!downloaded && remainder.length > 0 && <button type="button" className="button secondary" onClick={inspect}
          disabled={action.pending || hasPending || writesFiles && !folders.length}>{plan ? '重新预览' : '预览'}</button>}
        {canConfirm && <button type="button" className="button primary" onClick={confirm} disabled={action.pending}>
          {hasPending ? '继续处理' : `确认${verb} ${ready.length} 个文件`}
        </button>}
      </div>
    </div>
  </Modal>;
}
