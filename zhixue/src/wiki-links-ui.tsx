import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import type { WikiBacklink, WikiResolution, WikiTarget } from './api';
import { BackedStatus, Empty, Loading, useRead } from './shared';
import type { WikiDraft } from './wiki-markdown';

const kinds: Record<WikiTarget['kind'], string> = {
  course: '课程', note: '笔记', import: '导入资料', supplement: '补充讲解',
};

function TargetLabel({ entry }: { entry: WikiTarget }) {
  const type = [kinds[entry.kind], entry.projectTopic, entry.status === 'missing' ? '原件缺失' : ''].filter(Boolean).join(' · ');
  return <><strong>{entry.name}</strong><small>{type}</small><small>{entry.path}</small></>;
}

export function useWikiCompletion(draft: WikiDraft | undefined, apply: (entry: WikiTarget, draft: WikiDraft) => void) {
  const [dismissed, setDismissed] = useState('');
  const [requested, setRequested] = useState<string>();
  const [selection, setSelection] = useState<{ key: string; index: number }>();
  const list = useRef<HTMLDivElement>(null);
  const key = draft ? JSON.stringify(draft) : '';
  const active = draft && key !== dismissed ? draft : undefined;
  const query = active?.query;

  useEffect(() => {
    if (query === undefined) {
      setRequested(undefined);
      return;
    }
    const timer = window.setTimeout(() => setRequested(query), 130);
    return () => window.clearTimeout(timer);
  }, [query]);

  const request = query !== undefined && query === requested ? '/files/wiki/suggestions?' + new URLSearchParams({ q: query }) : null;
  const results = useRead<WikiTarget[]>(request);
  const entries = results.value ?? [];
  const index = selection?.key === key && selection.index < entries.length ? selection.index : 0;
  useEffect(() => {
    if (index) list.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' });
  }, [index]);

  function choose(entry: WikiTarget) {
    if (!active) return;
    setDismissed(key);
    apply(entry, active);
  }

  function onKeyDown(event: KeyboardEvent) {
    if (!active || event.nativeEvent.isComposing || event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      setDismissed(key);
    } else if (entries.length && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
      event.preventDefault();
      const step = event.key === 'ArrowDown' ? 1 : -1;
      setSelection({ key, index: (index + step + entries.length) % entries.length });
    } else if (entries[index] && (event.key === 'Enter' || event.key === 'Tab' && !event.shiftKey)) {
      event.preventDefault();
      event.stopPropagation();
      choose(entries[index]);
    }
  }

  const menu = active ? <div className="wiki-completion">
    {results.value ? entries.length ? <div className="wiki-options" role="listbox" aria-label="双链补全" ref={list}>
      {entries.map((entry, number) => <button type="button" role="option" aria-selected={number === index}
        key={entry.id} onMouseDown={event => event.preventDefault()} onClick={() => choose(entry)}>
        <TargetLabel entry={entry} />
      </button>)}
    </div> : <p className="wiki-feedback" role="status">没有匹配的链接目标。</p>
      : results.error ? <p className="wiki-feedback" role="status">{results.error}</p>
        : <Loading text="正在查找链接目标" />}
  </div> : null;

  return { menu, onKeyDown, reset: () => setDismissed('') };
}

export function WikiDestination({ target, onClose }: { target: string; onClose: () => void }) {
  const navigate = useNavigate();
  const resolution = useRead<WikiResolution>('/files/wiki/resolve?' + new URLSearchParams({ target }));
  const id = resolution.value?.state === 'resolved' ? resolution.value.choices[0]?.id : undefined;
  useEffect(() => {
    if (id) navigate('/library?' + new URLSearchParams({ file: id }), { replace: true });
  }, [id, navigate]);

  return <article className="panel library-preview">
    <div className="drawer-head"><h2>双链目标</h2>
      <button type="button" className="text-action" onClick={onClose}>关闭预览</button></div>
    {resolution.value?.state === 'ambiguous' ? <>
      <p className="file-status">有多个文件匹配「{target}」。请选择要打开的文件。</p>
      <div className="wiki-options">{resolution.value.choices.map(entry => <Link key={entry.id}
        to={'/library?' + new URLSearchParams({ file: entry.id })} replace>
        <TargetLabel entry={entry} />
      </Link>)}</div>
    </> : resolution.value?.state === 'missing' ? <Empty title="找不到链接目标">
      <p className="file-status">「{target}」当前无法匹配知识库中的文件。</p>
    </Empty> : <BackedStatus error={resolution.error} retry={resolution.refresh} />}
  </article>;
}

export function WikiBacklinks({ file }: { file: string }) {
  const [open, setOpen] = useState(false);
  const incoming = useRead<WikiBacklink[]>(open ? '/files/' + encodeURIComponent(file) + '/backlinks' : null);
  return <details className="wiki-backlinks" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>反向链接</summary>
    {open && (incoming.value ? incoming.value.length ? <ul>
      {incoming.value.map(entry => <li key={entry.id + '/' + entry.at}>
        <Link to={'/library?' + new URLSearchParams({ file: entry.id, at: String(entry.at), revision: entry.revision })}>
          {entry.name} · {entry.label}
        </Link>
        <p>{entry.snippet}</p>
      </li>)}
    </ul> : <p className="file-status">暂无反向链接。</p> : <BackedStatus error={incoming.error} retry={incoming.refresh} />)}
  </details>;
}
