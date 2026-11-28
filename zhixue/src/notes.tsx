import { lazy, Suspense, useEffect, useEffectEvent, useRef, useState } from 'react';
import { BookOpen, Clock3, NotebookPen, X } from 'lucide-react';
import { Link } from 'react-router-dom';
import { api, ApiError } from './api';
import { appendCourseExcerpt, type ExcerptRequest } from './course-excerpt';
import { NoteAIDialog, type NoteAIJob, type NoteAISelection } from './note-ai';
import { NewNoteDialog, NoteDetailsDialog, type NoteSummary } from './note-catalog';
import { NoteCheckPanel, useNoteCheck, type NoteFinding } from './note-checks';
import { maxNoteLength } from './note-content';
import { BackedStatus, ErrorNote, Loading, Modal, useAction, useRead } from './shared';

type VersionEntry = { revision: string; createdAt: string; seenOnDisk: boolean; excerpt: string };
type Version = { revision: string; content: string };
type ConflictEntry = { id: string; kind: 'changed' | 'interrupted' | 'detached'; createdAt: string; resolution: string | null };
type NoteFile = { revision: string | null; content: string; missing: boolean; conflicts: ConflictEntry[]; versions: VersionEntry[] };
type NoteSaved = { revision: string | null; content: string };
type ConflictPreview = ConflictEntry & {
  base: Version | null; external: Version | null; current: Version | null; draft: Version; diff: string[];
};
type VersionPreview = Version & { current: Version | null; diff: string[] };
type Draft = { content: string; baseRevision: string | null };
type NoteSelection = { kind: 'conflict'; id: string } | { kind: 'version'; revision: string };
type AIReview = { content: string; selected?: NoteAISelection; existing?: NoteAIJob };
type Resolution = 'keep' | 'draft' | 'external' | 'merge';

const NoteEditor = lazy(() => import('./note-editor'));
const autosaveDelay = 3000;
const noFindings: readonly NoteFinding[] = [];

const conflictTitles = { changed: '文件已在别处修改', interrupted: '保存中断', detached: '外部编辑器继续写入' };
const dateTime = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' });

function draftKey(noteId: string) { return `zhixue-note-draft:${noteId}`; }

export function readNoteDraft(noteId: string): Draft | null {
  try {
    const current = sessionStorage.getItem(`${draftKey(noteId)}:v2`);
    if (current) return JSON.parse(current) as Draft;
    const legacy = sessionStorage.getItem(draftKey(noteId));
    return legacy === null ? null : { content: legacy, baseRevision: null };
  } catch { return null; }
}

function localDraft(noteId: string, draft: Draft | null) {
  const key = draftKey(noteId);
  if (draft) sessionStorage.setItem(`${key}:v2`, JSON.stringify(draft));
  else sessionStorage.removeItem(`${key}:v2`);
  sessionStorage.removeItem(key);
}

function conflictId(error: unknown) {
  if (error instanceof ApiError && error.status === 409 && typeof error.payload.conflictId === 'string') return error.payload.conflictId;
  return undefined;
}

export function NoteDiff({ lines }: { lines: string[] }) {
  return <div className="note-difference">
    <strong>版本差异</strong>
    {lines.length ? <pre aria-label="版本差异">{lines.map((line, index) =>
      <code key={index} className={line.startsWith('+') ? 'added' : line.startsWith('-') ? 'removed' : 'context'}>{line}{'\n'}</code>)}</pre>
      : <p>两份文本内容相同。</p>}
  </div>;
}

function NoteCopy({ title, version }: { title: string; version: Version | null }) {
  return <details className="note-copy"><summary>{title}</summary>
    {version ? <pre>{version.content || '（空白笔记）'}</pre> : <p>该版本没有本机文件。</p>}
  </details>;
}

function ResolveNote({ endpoint, id, onDone, onClose, onRefresh }: {
  endpoint: string; id: string; onDone: (file: NoteSaved) => void; onClose: () => void; onRefresh: () => void;
}) {
  const path = `${endpoint}/conflicts/${id}`;
  const read = useRead<ConflictPreview>(path);
  const preview = read.value;
  const action = useAction();
  const [merge, setMerge] = useState<string>();
  const [updating, setUpdating] = useState(false);
  useEffect(() => { setUpdating(false); }, [preview]);
  const disabled = action.pending || updating || Boolean(read.error || preview?.resolution);

  function resolve(choice: Resolution) {
    if (!preview) return;
    void action.run(async () => {
      try {
        const file = await api<NoteSaved>(path, 'POST', {
          action: choice, currentRevision: preview.current?.revision ?? null, draftRevision: preview.draft.revision,
          externalRevision: preview.external?.revision ?? null, ...(choice === 'merge' ? { content: merge } : {}),
        });
        onDone(file);
      } catch (error) {
        if (!conflictId(error)) throw error;
        setUpdating(true);
        read.refresh();
        onRefresh();
        throw new Error('比较后出现了新的版本。请查看最新内容后重新确认。');
      }
    });
  }

  return <Modal title="处理笔记冲突" busy={action.pending} onClose={onClose}>
    {!preview ? <BackedStatus error={read.error} retry={read.refresh} /> : <div className="project-dialog-form note-comparison">
      <p>{preview.resolution ? '该冲突已处理。' : `${conflictTitles[preview.kind]}。${preview.current
        ? '请选择要保留的内容，或合并两份文本。' : '原路径当前没有文件。可保留缺失状态，或选择版本恢复。'}`}</p>
      <NoteDiff lines={preview.diff} />
      <div className="note-copy-group">
        <NoteCopy title="当前本机文件" version={preview.current} />
        {preview.external && preview.external.revision !== preview.current?.revision &&
          <NoteCopy title={preview.kind === 'interrupted' ? '保存前的版本' : '已保留的外部版本'} version={preview.external} />}
        <NoteCopy title={preview.kind === 'detached' ? '应用保存时的版本' : '应用草稿'} version={preview.draft} />
        {preview.base && <NoteCopy title="编辑前的版本" version={preview.base} />}
      </div>
      <ErrorNote message={read.error} retry={read.refresh} />
      <ErrorNote message={action.error} />
      {merge !== undefined && <label className="project-field">合并笔记<textarea value={merge} maxLength={50_000}
        onChange={event => setMerge(event.target.value)} disabled={action.pending} /></label>}
      <div className="project-dialog-actions">
        {merge === undefined ? <>
          <button type="button" className="button secondary" disabled={disabled} onClick={() => resolve('keep')}>
            {preview.current ? '保留本机文件' : '保留缺失状态'}</button>
          {preview.external && preview.external.revision !== preview.current?.revision &&
            <button type="button" className="button secondary" disabled={disabled} onClick={() => resolve('external')}>
              {preview.kind === 'interrupted' ? '恢复保存前的版本' : '使用外部版本'}</button>}
          {preview.draft.revision !== preview.current?.revision &&
            <button type="button" className="button secondary" disabled={disabled} onClick={() => resolve('draft')}>使用应用草稿</button>}
          <button type="button" className="button primary" disabled={disabled} onClick={() => setMerge(preview.draft.content)}>手动合并</button>
        </> : <>
          <button type="button" className="button secondary" disabled={action.pending} onClick={() => setMerge(undefined)}>返回版本选择</button>
          <button type="button" className="button primary" disabled={disabled} onClick={() => resolve('merge')}>保存合并结果</button>
        </>}
      </div>
    </div>}
  </Modal>;
}

function RestoreNote({ endpoint, revision, onDone, onClose, onConflict }: {
  endpoint: string; revision: string; onDone: (file: NoteSaved) => void; onClose: () => void; onConflict: (id: string) => void;
}) {
  const read = useRead<VersionPreview>(`${endpoint}/versions/${revision}`);
  const preview = read.value;
  const action = useAction();
  function restore() {
    if (!preview) return;
    void action.run(async () => {
      try {
        onDone(await api<NoteSaved>(endpoint, 'PUT', { baseRevision: preview.current?.revision ?? null, content: preview.content }));
      } catch (error) {
        const id = conflictId(error);
        if (id) onConflict(id);
        else throw error;
      }
    });
  }
  return <Modal title="查看笔记版本" onClose={onClose} busy={action.pending}>
    {!preview ? <BackedStatus error={read.error} retry={read.refresh} /> : <div className="project-dialog-form note-comparison">
      <NoteDiff lines={preview.diff} />
      <NoteCopy title="查看版本全文" version={preview} />
      <ErrorNote message={action.error} />
      <div className="project-dialog-actions">
        <button type="button" className="button secondary" disabled={action.pending} onClick={onClose}>关闭预览</button>
        <button type="button" className="button primary" disabled={action.pending || preview.current?.revision === revision} onClick={restore}>恢复此版本</button>
      </div>
    </div>}
  </Modal>;
}

export function NotePanel({ noteId, lessonId, legacyLessonId, visible, onDirty, onChange, excerpt, onExcerptComplete,
  label = '本节笔记', showLibraryLink = true }: {
  noteId: string; lessonId?: string; legacyLessonId?: string; visible: boolean; onDirty?: (dirty: boolean) => void; onChange: () => void;
  excerpt?: ExcerptRequest; onExcerptComplete?: (id: number) => void; label?: string; showLibraryLink?: boolean;
}) {
  const endpoint = legacyLessonId ? '/lessons/' + legacyLessonId + '/note' : '/notes/' + noteId;
  const read = useRead<NoteFile>(endpoint);
  const [draft, setDraft] = useState(() => readNoteDraft(noteId));
  const latestDraft = useRef(draft);
  const lastAttempt = useRef<Draft | null>(null);
  const inFlight = useRef(false);
  const [applied, setApplied] = useState<NoteSaved>();
  const [saved, setSaved] = useState('');
  const [draftError, setDraftError] = useState('');
  const completedExcerpt = useRef(0);
  const [excerptError, setExcerptError] = useState('');
  const [excerptStatus, setExcerptStatus] = useState('');
  const [selection, setSelection] = useState<NoteSelection>();
  const [aiReview, setAIReview] = useState<AIReview>();
  const [aiStatus, setAIStatus] = useState('');
  const [findingLocation, setFindingLocation] = useState<{ id: string }>();
  const action = useAction();
  const current = applied ?? read.value;
  const aiEndpoint = '/notes/' + noteId + '/assistant';
  const ai = useRead<NoteAIJob | null>(visible && current && !read.error ? aiEndpoint : null);
  const checkEndpoint = '/notes/' + noteId + '/checks';
  const checks = useNoteCheck(checkEndpoint, visible && !!current && !read.error);
  const checkingEnabled = Boolean(checks.value?.id);
  const checkRevision = checks.value?.revision;
  const noteRevision = current?.revision;
  const publishedFindings = !draft && checkRevision === noteRevision ? checks.value?.findings ?? noFindings : noFindings;
  const refreshNote = read.refresh;
  const dirty = draft !== null;
  useEffect(() => {
    if (visible && checkingEnabled && !dirty && noteRevision !== checkRevision) refreshNote();
  }, [visible, checkingEnabled, dirty, checkRevision, noteRevision, refreshNote]);
  const reportDirty = useEffectEvent((dirty: boolean) => onDirty?.(dirty));
  useEffect(() => { reportDirty(draft !== null); }, [draft]);
  useEffect(() => { setApplied(undefined); }, [read.value]);

  function rememberDraft(update: Draft | null) {
    latestDraft.current = update;
    setDraft(update);
    try { localDraft(noteId, update); setDraftError(''); }
    catch { setDraftError('此标签页无法保存草稿。请先将编辑内容保存。'); }
  }
  function edit(content: string) {
    if (!current) return;
    const previous = latestDraft.current;
    const baseRevision = previous ? previous.baseRevision : current.revision;
    setSaved('');
    rememberDraft(!inFlight.current && content === current.content && baseRevision === current.revision
      ? null : { content, baseRevision });
  }
  function openAI(selected?: NoteAISelection, existing?: NoteAIJob) {
    if (!current || selection || read.error) return;
    setAIStatus('');
    setAIReview({ content: latestDraft.current?.content ?? current.content, selected, existing });
  }
  function closeAI() {
    setAIReview(undefined);
    ai.refresh();
  }
  async function applyAI(markdown: string, replace: boolean, signal: AbortSignal) {
    if (!aiReview || !current || !visible || selection) throw new Error('请回到该笔记后再确认建议。');
    const starting = latestDraft.current;
    const text = starting?.content ?? current.content;
    const revision = starting ? starting.baseRevision : current.revision;
    if (aiReview.content !== text) throw new Error('笔记正文已变化。可返回笔记后重新查看建议。');
    if (inFlight.current) throw new Error('正在保存笔记。保存完成后可继续确认建议。');
    const live = await api<NoteFile>(endpoint, 'GET', undefined, signal);
    if (signal.aborted) return;
    if (inFlight.current || starting !== latestDraft.current) throw new Error('草稿正在更新，请重新确认建议。');
    if (live.revision !== revision) {
      read.refresh();
      throw new Error('本机笔记已在别处修改。请比较当前版本后再采用建议。');
    }
    const proposal = markdown.trim();
    let next: string;
    if (replace) {
      if (!aiReview.selected) throw new Error('请在笔记中重新选段。');
      next = aiReview.selected.replace(proposal);
    } else {
      const boundary = !text || /(?:\r?\n){2}$/.test(text) ? '' : text.endsWith('\n') ? '\n' : '\n\n';
      next = text + boundary + proposal + '\n';
    }
    if (next.length > maxNoteLength) throw new Error('笔记最多 50,000 个字符，可缩短建议后再确认。');
    edit(next);
    setAIStatus('AI 建议已加入笔记');
  }
  function finishDraft(submitted: Draft, savedFile?: NoteSaved) {
    const latest = latestDraft.current;
    if (!latest) return;
    const stored = readNoteDraft(noteId);
    if (stored && (stored.content !== latest.content || stored.baseRevision !== latest.baseRevision)) return;
    if (savedFile) {
      // Edits made while the file was being saved continue from its confirmed revision.
      rememberDraft(latest.content === savedFile.content ? null : { content: latest.content, baseRevision: savedFile.revision });
    } else if (latest.content === submitted.content && latest.baseRevision === submitted.baseRevision) {
      rememberDraft(null);
    } else {
      // The conflict already contains the submitted text. Keep subsequent edits available for review.
      lastAttempt.current = latest;
    }
  }
  function done(file: NoteSaved, message = '笔记已保存') {
    setApplied(file);
    setSaved(message);
    setSelection(undefined);
    read.refresh();
    onChange();
  }
  function showConflict(id: string) {
    setSelection({ kind: 'conflict', id });
    setSaved('');
    read.refresh();
  }
  function save() {
    const submitted = latestDraft.current;
    if (!submitted || selection) return;
    void action.run(async () => {
      lastAttempt.current = submitted;
      inFlight.current = true;
      try {
        const file = await api<NoteSaved>(endpoint, 'PUT', submitted);
        finishDraft(submitted, file);
        done(file);
      } catch (error) {
        const id = conflictId(error);
        if (!id) throw error;
        finishDraft(submitted);
        showConflict(id);
      } finally {
        inFlight.current = false;
      }
    });
  }
  const autoSave = useEffectEvent(save);
  const editable = !!current && !read.error;
  const insertExcerpt = useEffectEvent((request: ExcerptRequest) => {
    if (!current || request.id <= completedExcerpt.current) return;
    const next = appendCourseExcerpt(latestDraft.current?.content ?? current.content, request);
    if (next.content === undefined) {
      setExcerptError(next.error);
      return;
    }
    completedExcerpt.current = request.id;
    setExcerptError('');
    setExcerptStatus('摘录已加入笔记');
    edit(next.content);
    onExcerptComplete?.(request.id);
  });
  useEffect(() => {
    if (excerpt && editable && !selection) insertExcerpt(excerpt);
  }, [current, draft, editable, excerpt, selection]);
  useEffect(() => {
    if (!editable || !draft || draft === lastAttempt.current || selection || action.pending) return;
    const timer = window.setTimeout(() => autoSave(), autosaveDelay);
    return () => window.clearTimeout(timer);
  }, [editable, draft, selection, action.pending]);

  return <>
    {!read.value || !current ? <><BackedStatus error={read.error} retry={read.refresh} />
      {read.error && <Link className="text-action" to={`/library?file=note-${noteId}`}>在知识库查看文件位置</Link>}</> : <>
      <ErrorNote message={read.error} retry={read.refresh} />
      {read.value.missing && current.revision === null && <p className="notice">原笔记文件已缺失。可从版本历史查看或恢复。</p>}
      {read.value.conflicts.map(conflict => <div key={conflict.id} className="notice note-warning">
        <span>{conflictTitles[conflict.kind]}</span>
        <button type="button" className="text-action" disabled={action.pending} onClick={() => setSelection({ kind: 'conflict', id: conflict.id })}>
          查看冲突 · {dateTime.format(new Date(conflict.createdAt))}
        </button>
      </div>)}
      <Suspense fallback={<Loading text="正在加载笔记编辑器" />}>
        <NoteEditor label={label} content={draft?.content ?? current.content} disabled={!!selection}
          onChange={edit} onAI={visible && !read.error ? openAI : undefined}
          findings={publishedFindings} location={findingLocation} />
      </Suspense>
      <div className="action-row">
        <button type="button" className="button primary" disabled={action.pending || !!selection || !draft} onClick={save}>保存笔记</button>
        <span role="status" className={saved && !draft ? 'saved' : 'note-save-status'}>
          {action.pending ? '正在保存笔记' : draft ? (action.error ? '草稿保存失败' : '草稿待保存') : saved}
        </span>
        {ai.value && <button type="button" className="text-action" disabled={!!selection} aria-haspopup="dialog"
          onClick={() => openAI(undefined, ai.value ?? undefined)}>查看最近 AI 建议</button>}
      </div>
      {aiStatus && <p className="note-excerpt-status" role="status">{aiStatus}</p>}
      <ErrorNote message={action.error || draftError} />
      {visible && !selection && <NoteCheckPanel endpoint={checkEndpoint} lessonId={lessonId} read={checks}
        findings={publishedFindings} dirty={dirty}
        syncing={checkingEnabled && checkRevision !== noteRevision}
        onLocate={finding => setFindingLocation({ id: finding.id })} />}
      {excerpt && excerptError && <div className="note-excerpt-error">
        <ErrorNote message={excerptError} />
        <button type="button" className="text-action" onClick={() => {
          setExcerptError('');
          onExcerptComplete?.(excerpt.id);
        }}>取消这条摘录</button>
      </div>}
      {excerptStatus && !excerpt && <p role="status" className="note-excerpt-status">{excerptStatus}</p>}
      {showLibraryLink && current.revision && <Link className="text-action note-library-link" to={'/library?file=note-' + noteId}>
        在知识库打开笔记
      </Link>}
      {read.value.versions.length > 0 && <details className="note-history"><summary><Clock3 size={15} />版本历史</summary>
        {read.value.versions.map(version => <button type="button" key={version.revision} className="note-history-item" disabled={action.pending || draft !== null}
          onClick={() => setSelection({ kind: 'version', revision: version.revision })}>
          <BookOpen size={15} />
          <span className="note-history-excerpt"><strong>{version.excerpt.trim() || '空白笔记'}</strong>
            <small>{dateTime.format(new Date(version.createdAt))}</small></span>
          <small>{version.revision === current.revision ? '当前版本' : version.seenOnDisk ? '文件存档' : '草稿存档'}</small>
        </button>)}
        {draft && <p>保存当前草稿后可恢复历史版本。</p>}
      </details>}
    </>}
    {visible && selection?.kind === 'conflict' && <ResolveNote key={selection.id} endpoint={endpoint} id={selection.id}
      onDone={file => done(file, '冲突已处理')} onRefresh={read.refresh} onClose={() => setSelection(undefined)} />}
    {visible && selection?.kind === 'version' && <RestoreNote endpoint={endpoint} revision={selection.revision}
      onDone={file => done(file, '版本已恢复')} onConflict={showConflict} onClose={() => setSelection(undefined)} />}
    {visible && aiReview && !selection && <NoteAIDialog endpoint={aiEndpoint} lessonId={lessonId}
      selected={aiReview.selected} existing={aiReview.existing} onClose={closeAI} onApply={applyAI} />}
  </>;
}

export function LessonNotes({ lessonId, visible, excerptQueue, onExcerptComplete, onNoteTarget, onDirty, onChange, close }: {
  lessonId: string; visible: boolean; excerptQueue: ExcerptRequest[];
  onExcerptComplete: (id: number) => void; onNoteTarget: (id: string | undefined) => void;
  onDirty: (id: string, dirty: boolean) => void; onChange: () => void; close: () => void;
}) {
  const catalog = useRead<NoteSummary[]>('/lessons/' + lessonId + '/notes');
  const [selected, setSelected] = useState<string>();
  const [visited, setVisited] = useState<string[]>([]);
  const [creating, setCreating] = useState(false);
  const [settings, setSettings] = useState<string>();
  const [justCreated, setJustCreated] = useState<NoteSummary>();
  const options = catalog.value ?? [];
  const shown = justCreated && !options.some(note => note.id === justCreated.id) ? [...options, justCreated] : options;
  const active = shown.find(note => note.id === selected) ?? shown[0];
  const activeId = active?.id;

  useEffect(() => {
    if (justCreated && catalog.value?.some(note => note.id === justCreated.id)) setJustCreated(undefined);
  }, [catalog.value, justCreated]);
  useEffect(() => {
    if (activeId) setVisited(ids => ids.includes(activeId) ? ids : [...ids, activeId]);
    onNoteTarget(activeId);
  }, [activeId, onNoteTarget]);

  const panels = activeId && !visited.includes(activeId) ? [...visited, activeId] : visited;

  return <aside className="panel note-panel">
    <div className="drawer-head"><h2><NotebookPen size={20} />本节笔记</h2>
      <button type="button" className="icon-button" aria-label="关闭笔记" onClick={close}><X size={19} /></button>
    </div>
    <div className="note-picker">
      {!!shown.length && <select aria-label="选择笔记" value={activeId} onChange={event => setSelected(event.target.value)}>
        {shown.map(note => <option key={note.id} value={note.id}>{note.title}</option>)}
      </select>}
      <button type="button" className="button secondary" onClick={() => setCreating(true)}>新建笔记</button>
      {active && <button type="button" className="text-action" onClick={() => setSettings(active.id)}>标题与课程关联</button>}
    </div>
    {catalog.value ? !active && <p className="notice">本节暂无笔记。</p> : <BackedStatus error={catalog.error} retry={catalog.refresh} />}
    {panels.map(id => <div key={id} hidden={id !== activeId}>
      <NotePanel noteId={id} lessonId={lessonId} legacyLessonId={id === lessonId ? lessonId : undefined}
        visible={visible && id === activeId} onDirty={dirty => onDirty(id, dirty)} onChange={onChange}
        excerpt={excerptQueue.find(item => item.noteId === id || (!item.noteId && id === activeId))}
        onExcerptComplete={onExcerptComplete} />
    </div>)}
    {visible && creating && <NewNoteDialog lessonId={lessonId} onClose={() => setCreating(false)} onDone={note => {
      setCreating(false);
      setJustCreated(note);
      setSelected(note.id);
      catalog.refresh();
      onChange();
    }} />}
    {visible && settings && <NoteDetailsDialog noteId={settings} onClose={() => setSettings(undefined)} onDone={() => {
      setSettings(undefined);
      catalog.refresh();
      onChange();
    }} />}
  </aside>;
}
