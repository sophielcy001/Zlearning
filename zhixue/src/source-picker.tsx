import { useState } from 'react';
import type { LibraryFile, LibraryItem } from './api';
import { BackedStatus, ErrorNote, FileUpload, useRead } from './shared';
import type { SourceExcerpt, SourceLocator } from './verification';

export function sourceLocators(sources: SourceExcerpt[]): SourceLocator[] {
  return sources.map(({ fileId, at, revision, offset }) => ({ fileId, at, revision, offset }));
}

export function SourcePicker({ sources, onChange, disabled, allowImport = false }: {
  sources: SourceExcerpt[]; onChange: (sources: SourceExcerpt[]) => void; disabled: boolean; allowImport?: boolean;
}) {
  const files = useRead<LibraryItem[]>('/files');
  const [file, setFile] = useState('');
  const [point, setPoint] = useState('1');
  const [offset, setOffset] = useState('0');
  const at = Number(point);
  const start = Number(offset);
  const candidate = useRead<LibraryFile>(file && Number.isSafeInteger(at) && at >= 1 ? `/files/${file}?at=${at}` : null);
  const item = candidate.value;
  const anchor = item?.anchor;
  const excerpt = anchor && Number.isSafeInteger(start) && start >= 0
    ? Array.from(anchor.text.slice(0, start * 2 + 4400)).slice(start, start + 2200).join('') : '';
  const duplicate = sources.some(source => source.fileId === file && source.at === at && source.offset === start);

  function choose(identity: string) {
    setFile(identity);
    setPoint('1');
    setOffset('0');
  }
  function add() {
    const revision = anchor?.textRevision ?? item?.revision;
    if (!item || !revision || !anchor || anchor.number !== at || !excerpt.trim() || duplicate || sources.length >= 6) return;
    onChange([...sources, { id: '', fileId: item.id, title: item.name, at, revision, offset: start, label: anchor.label, excerpt }]);
  }

  return <>
    {allowImport && <FileUpload label="导入核验资料" disabled={disabled} onImport={identity => { files.refresh(); choose(identity); }} />}
    {files.value ? <>
      <label className="project-field">知识库资料
        <select value={file} disabled={disabled} onChange={event => choose(event.target.value)}>
          <option value="">选择一份已导入资料</option>
          {files.value.filter(entry => entry.kind === 'import' && entry.revision && ['ready', 'partial'].includes(entry.status)).map(entry => (
            <option key={entry.id} value={entry.id}>{entry.name}</option>
          ))}
        </select>
      </label>
      {file && <>
        <div className="generation-preview-form">
          <label className="project-field">页、行或段落编号
            <input type="number" min={1} value={point} max={item?.pageCount ?? undefined} disabled={disabled}
              onChange={event => { setPoint(event.target.value); setOffset('0'); }} />
          </label>
          <label className="project-field">截取起点（从 0 算起）
            <input type="number" min={0} value={offset} disabled={disabled} onChange={event => setOffset(event.target.value)} />
          </label>
        </div>
        <ErrorNote message={candidate.error} retry={candidate.refresh} />
        {item && <div className="file-anchor"><strong>{anchor?.label ?? '这一位置无可用文字'}</strong>
          {excerpt && <pre>{excerpt}</pre>}
        </div>}
        <button type="button" className="button secondary" disabled={disabled || !excerpt.trim() || duplicate || sources.length >= 6}
          onClick={add}>加入此定位</button>
      </>}
    </> : <BackedStatus error={files.error} retry={files.refresh} />}
    {!!sources.length && <ul className="scope-items" aria-label="已选资料">
      {sources.map((source, index) => <li key={`${source.fileId}/${source.at}/${source.offset}`}>
        <span>{source.title} · {source.label}</span>
        <button type="button" className="text-action" aria-label={`移除资料 ${source.title} ${source.label}`} disabled={disabled}
          onClick={() => onChange(sources.filter((_, position) => position !== index))}>移除</button>
      </li>)}
    </ul>}
  </>;
}
