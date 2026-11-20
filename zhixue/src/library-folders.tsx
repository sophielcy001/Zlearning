import { Folder, FolderOpen, FolderPlus, PencilLine } from 'lucide-react';
import { useState, type ReactNode } from 'react';
import { api } from './api';
import { ErrorNote, Modal, useAction } from './shared';

export type LibraryFolder = { path: string; parent: string | null; name: string };
type LocationSource = { path: string; id?: string };
type MovePreview = { path: string; fileCount: number; trackedCount: number; token: string };

export const LIBRARY_ROOT = 'library';

export function parentFolder(path: string) {
  return path.slice(0, path.lastIndexOf('/'));
}

export function LibraryFolderList({ entries, selected, onSelect, onCreate, onMove }: {
  entries: LibraryFolder[];
  selected: string;
  onSelect: (path: string) => void;
  onCreate?: () => void;
  onMove?: () => void;
}) {
  const children = new Map<string | null, LibraryFolder[]>();
  for (const entry of entries) {
    const siblings = children.get(entry.parent) ?? [];
    siblings.push(entry);
    children.set(entry.parent, siblings);
  }

  function renderEntries(parent: string | null, depth: number): ReactNode[] {
    return (children.get(parent) ?? []).flatMap(entry => {
      const active = selected === entry.path;
      const Icon = active ? FolderOpen : Folder;
      return [
        <li key={entry.path}>
          <button type="button" className={active ? 'selected' : ''} aria-pressed={active} title={entry.path}
            onClick={() => onSelect(entry.path)} style={{ paddingInlineStart: 6 + Math.min(depth, 4) * 10 }}>
            <Icon size={16} /><span>{entry.name}</span>
          </button>
        </li>,
        ...renderEntries(entry.path, depth + 1),
      ];
    });
  }

  return <div className="library-folders">
    <ul className="library-folder-tree" aria-label="实际文件夹">{renderEntries(null, 0)}</ul>
    {onCreate && <button type="button" onClick={onCreate}><FolderPlus size={16} />新建文件夹</button>}
    {onMove && <button type="button" onClick={onMove}><PencilLine size={16} />移动或重命名文件夹</button>}
  </div>;
}

export function FolderDialog({ parent, entries, source, onClose, onDone }: {
  parent: string;
  entries: LibraryFolder[];
  source?: LocationSource;
  onClose: () => void;
  onDone: (path: string) => void;
}) {
  const [destination, setDestination] = useState(parent);
  const [name, setName] = useState(source?.path.split('/').at(-1) ?? '');
  const [preview, setPreview] = useState<MovePreview>();
  const action = useAction();
  const moving = !!source;
  const title = moving ? source.id ? '移动或重命名文件' : '移动或重命名文件夹' : '新建文件夹';
  const numbers = preview && `将移动 ${preview.fileCount} 份磁盘文件，其中 ${preview.trackedCount} 份已登记在知识库。`;
  const folders = source && !source.id ? entries.filter(entry =>
    entry.path !== source.path && !entry.path.startsWith(source.path + '/')) : entries;
  const canSubmit = name.trim().length > 0 && !action.pending;

  function submit() {
    if (!canSubmit) return;
    const location = { parent: destination, name };
    void action.run(async () => {
      if (!source) {
        onDone((await api<LibraryFolder>('/files/folders', 'POST', location)).path);
        return;
      }
      const request = { ...location, source: source.path, ...(source.id && { fileId: source.id }) };
      if (!preview) {
        setPreview(await api<MovePreview>('/files/relocation/preview', 'POST', request));
        return;
      }
      try {
        onDone((await api<{ path: string }>('/files/relocation', 'PUT', { ...request, token: preview.token })).path);
      } catch (failure) {
        setPreview(undefined);
        throw failure;
      }
    });
  }

  return <Modal title={title} onClose={onClose} busy={action.pending}>
    <form className="location-dialog" onSubmit={event => { event.preventDefault(); submit(); }}>
      {source && <p className="file-path">{source.path}</p>}
      <label className="project-field">目标文件夹
        <select value={destination} disabled={action.pending} onChange={event => {
          setDestination(event.target.value);
          setPreview(undefined);
        }}>
          {folders.map(entry => <option key={entry.path} value={entry.path}>
            {entry.path === LIBRARY_ROOT ? entry.name : entry.path.slice(LIBRARY_ROOT.length + 1)}
          </option>)}
          {!folders.some(entry => entry.path === destination) && <option value={destination}>{destination}</option>}
        </select>
      </label>
      <label className="project-field">{source?.id ? '文件名' : '文件夹名称'}
        <input value={name} maxLength={255} required disabled={action.pending}
          onChange={event => { setName(event.target.value); setPreview(undefined); }} />
      </label>
      {preview && <div className="location-preview" role="status">
        <p className="file-path">目标位置 {preview.path}</p>
        <p>{numbers} 已登记文件沿用现有 ID 和引用。</p>
      </div>}
      <ErrorNote message={action.error} />
      <div className="action-row">
        <button type="submit" className="button primary" disabled={!canSubmit}>
          {moving ? preview ? '确认变更' : '预览变更' : '创建文件夹'}
        </button>
        <button type="button" className="button secondary" disabled={action.pending} onClick={onClose}>取消</button>
      </div>
    </form>
  </Modal>;
}
