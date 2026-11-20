import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react';
import { BookOpen, ChevronLeft, ChevronRight, Copy, Download, FileText, FolderInput, FolderOpen, NotebookPen, PencilLine, RefreshCw, Search, Sparkles, Square, SquareCheck, Star, Tags, Trash2, X } from 'lucide-react';
import { useSearchParams } from 'react-router-dom';
import remarkMath from 'remark-math';
import { api, apiResponse, type LibraryFile, type LibraryHit, type LibraryItem, type LibraryMatches, type ProjectOption, type SemanticIndexStatus, type WebSource } from './api';
import { BackedStatus, Empty, ErrorNote, FileUpload, Loading, Markdown, Modal, useAction, useRead } from './shared';
import { MoveToTrash, TrashPanel } from './library-trash';
import { NewNoteDialog, NoteDetailsDialog } from './note-catalog';
import { NotePanel } from './notes';
import { WebImportButton } from './web-import';
import { WikiBacklinks, WikiDestination } from './wiki-links-ui';
import { WikiText } from './wiki-text';
import { LibraryViewSwitch } from './library-views';
import { FolderDialog, LIBRARY_ROOT, LibraryFolderList, parentFolder, type LibraryFolder } from './library-folders';
import { downloadFile, FileBatchDialog, FileTagDialog, LIBRARY_BATCH_LIMIT, type BatchFileAction, type SelectedLibraryFile } from './library-organize';
import { CLASSIFICATION, CLASSIFICATION_FILE_LIMIT, ClassificationRequest, ClassificationReview, type ClassificationQueue } from './library-classification';
import { ExternalRecognition } from './recognition';

const LibraryGraph = lazy(() => import('./library-graph').then(module => ({ default: module.LibraryGraphPage })));

const TYPES = [
  { key: 'all', label: '全部文件', icon: FolderOpen },
  { key: 'course', label: '课程', icon: BookOpen },
  { key: 'supplement', label: '补充讲解', icon: BookOpen },
  { key: 'note', label: '笔记', icon: NotebookPen },
  { key: 'import', label: '导入资料', icon: FileText },
] as const;

const FORMATS = { txt: 'TXT', md: 'Markdown', pdf: 'PDF', docx: 'Word', png: 'PNG', jpg: 'JPG', jpeg: 'JPEG', html: '网页' };
const STATUSES = { ready: '', partial: '部分文字可搜索', missing: '原件缺失', failed: '无法索引正文', empty: '无可搜索文字',
  trashed: '已移入回收站', purged: '回收站原件已清理' };
type FileShelf = 'all' | 'favorites' | 'recent';
const SEMANTIC_INDEX = '/files/semantic-index';

export function LibraryPage() {
  const [params] = useSearchParams();
  return params.get('view') === 'graph' ? <Suspense fallback={<section className="page"><Loading text="正在打开图谱" /></section>}>
    <LibraryGraph />
  </Suspense> : <LibraryFiles />;
}

function LibraryFiles() {
  const [params, setParams] = useSearchParams();
  const [filter, setFilter] = useState<string>('all');
  const [query, setQuery] = useState('');
  const [searchTerm, setSearchTerm] = useState('');
  const [searchMode, setSearchMode] = useState<'keyword' | 'hybrid'>('keyword');
  const [scope, setScope] = useState('current');
  const [format, setFormat] = useState('');
  const [project, setProject] = useState('');
  const [tag, setTag] = useState('');
  const [shelf, setShelf] = useState<FileShelf>('all');
  const [rebuilt, setRebuilt] = useState(false);
  const [creatingNote, setCreatingNote] = useState(false);
  const [selecting, setSelecting] = useState(false);
  const [picked, setPicked] = useState<Record<string, string>>({});
  const [batch, setBatch] = useState<{ action: BatchFileAction | 'trash' | 'tag'; files: SelectedLibraryFile[] }>();
  const [classifying, setClassifying] = useState<Pick<LibraryItem, 'id' | 'name'>[]>();
  const [classificationRevision, setClassificationRevision] = useState(0);
  const appliedHistory = useRef<string | undefined>(undefined);
  const [folderAction, setFolderAction] = useState<'create' | 'move'>();
  const action = useAction();
  const semanticAction = useAction();
  const inTrash = params.has('trash');
  const viewingClassification = params.has('classify');
  const list = useRead<LibraryItem[]>(inTrash ? null : '/files');
  const classifications = useRead<ClassificationQueue>(inTrash ? null : CLASSIFICATION);
  const directories = useRead<LibraryFolder[]>('/files/folders');
  const projectOptions = useRead<ProjectOption[]>(inTrash ? null : '/projects/options');
  const directory = params.get('folder') ?? '';
  const actualFolders = directories.value ?? [];
  const currentFolder = actualFolders.find(entry => entry.path === directory);
  const wikiTarget = params.get('wiki') || null;
  const selected = wikiTarget ? null : params.get('file');
  const fullFile = !!selected && params.has('open');
  const at = params.get('at');
  const revision = params.get('revision');
  const browsing = !query.trim();
  const request = new URLSearchParams({ q: searchTerm });
  const hybrid = searchMode === 'hybrid' && !inTrash;
  if (hybrid) request.set('mode', 'hybrid');
  const semantic = useRead<SemanticIndexStatus>(hybrid ? SEMANTIC_INDEX : null);
  if (scope === 'current') {
    if (filter !== 'all') request.set('kind', filter);
    if (directory) request.set('directory', directory);
  }
  if (format) request.set('format', format);
  if (project) request.set('project', project);
  if (tag) request.set('tag', tag);
  if (shelf !== 'all') request.set('shelf', shelf);
  const matches = useRead<LibraryMatches>(inTrash || browsing || !searchTerm ? null : `/files/search?${request}`);
  const items = list.value?.filter(item => (filter === 'all' || item.kind === filter) && (!directory || parentFolder(item.path) === directory)
    && (!format || item.format === format) && (!project || (item.projectIds ?? [item.projectId]).includes(project))
    && (!tag || item.tags?.some(entry => entry.key === tag))
    && (shelf === 'all' || (shelf === 'favorites' ? item.favorite : item.lastViewedAt)));
  if (shelf === 'recent') items?.sort((left, right) => (right.lastViewedAt ?? '').localeCompare(left.lastViewedAt ?? ''));
  const tags = new Map(list.value?.flatMap(item => (item.tags ?? []).map(entry => [entry.key, entry.name] as const)));
  const selectedCount = Object.keys(picked).length;
  const { refresh: refreshList } = list;
  const { refresh: refreshMatches } = matches;
  const { refresh: refreshSemantic } = semantic;
  const matchIndex = matches.value?.semantic;
  const indexState = semantic.value?.state;
  const indexedPassages = semantic.value?.completed;
  const semanticStatus = indexState === 'ready' && matchIndex?.state === 'failed'
    ? matchIndex : semantic.value ?? matchIndex;
  const indexing = indexState === 'preparing' || indexState === 'indexing' || matchIndex?.state === 'indexing';
  const { refresh: refreshDirectories } = directories;
  const { refresh: refreshClassifications } = classifications;
  const refreshFiles = useCallback(() => {
    refreshList(); refreshMatches(); refreshDirectories(); refreshSemantic();
  }, [refreshList, refreshMatches, refreshDirectories, refreshSemantic]);
  const refreshRecent = useCallback(() => {
    if (shelf === 'recent') { refreshList(); refreshMatches(); }
  }, [refreshList, refreshMatches, shelf]);
  const classificationCount = classifications.value?.pending.length ?? 0;
  const polling = !!classifications.value?.jobs.some(job => job.state === 'queued' || job.state === 'running'
    || viewingClassification && job.state === 'blocked')
    || !!classifications.value?.pending.some(item => item.state === 'approved');
  const applied = classifications.value?.history.filter(item => item.result?.path || item.result?.tags.length)
    .map(item => item.id).join();

  useEffect(() => {
    if (!polling) return;
    const timer = window.setInterval(refreshClassifications, 1400);
    return () => window.clearInterval(timer);
  }, [polling, refreshClassifications]);

  useEffect(() => {
    if (inTrash) return;
    window.addEventListener('focus', refreshClassifications);
    return () => window.removeEventListener('focus', refreshClassifications);
  }, [inTrash, refreshClassifications]);

  useEffect(() => {
    if (applied === undefined || appliedHistory.current === applied) return;
    if (appliedHistory.current !== undefined) {
      refreshFiles();
      setClassificationRevision(current => current + 1);
    }
    appliedHistory.current = applied;
  }, [applied, refreshFiles]);

  useEffect(() => {
    const timeout = window.setTimeout(() => setSearchTerm(query.trim()), 220);
    return () => window.clearTimeout(timeout);
  }, [query]);

  useEffect(() => {
    if (!hybrid || !indexing) return;
    const timer = window.setInterval(refreshSemantic, 1400);
    return () => window.clearInterval(timer);
  }, [hybrid, indexing, refreshSemantic]);

  useEffect(() => {
    if (hybrid && indexState) refreshMatches();
  }, [hybrid, indexState, indexedPassages, refreshMatches]);

  function enableSemantic() {
    void semanticAction.run(async () => {
      await api<SemanticIndexStatus>(SEMANTIC_INDEX, 'POST');
      refreshSemantic();
      refreshMatches();
    });
  }

  function setFileView(target: Record<string, string> = {}) {
    setParams({ ...(directory && { folder: directory }), ...target });
  }

  function showClassification(show: boolean) {
    const next = new URLSearchParams(params);
    if (show) {
      refreshClassifications();
      next.set('classify', '1');
    } else next.delete('classify');
    setParams(next);
  }

  function selectFile(item: LibraryItem | LibraryHit, open = false) {
    const target: Record<string, string> = { file: item.id };
    if (open) target.open = '1';
    if ('location' in item && item.location && item.revision) {
      target.at = String(item.location.number);
      target.revision = item.location.textRevision ?? item.revision;
    }
    setFileView(target);
  }

  function keepFileLayout(target: Record<string, string>) {
    setFileView({ ...(fullFile && { open: '1' }), ...target });
  }

  function imported(id: string) {
    setFilter('all'); setFormat(''); setProject(''); setTag(''); setShelf('all'); setQuery(''); setSearchTerm('');
    refreshFiles();
    setParams({ file: id });
  }

  function rebuild() {
    setRebuilt(false);
    void action.run(async () => {
      await api('/files/reindex', 'POST');
      refreshFiles();
      setRebuilt(true);
    });
  }

  function fileButton(item: LibraryItem | LibraryHit) {
    const hit = 'location' in item ? item : null;
    const pressed = selecting ? item.id in picked : selected === item.id && (hit?.location ? at === String(hit.location.number) : at === null);
    const Icon = selecting ? pressed ? SquareCheck : Square : FileText;
    return <button className={`library-file ${pressed ? 'selected' : ''}`} aria-pressed={pressed} key={`${item.id}/${hit?.location?.number ?? 0}`}
      disabled={selecting && !pressed && (item.status === 'missing' || selectedCount >= LIBRARY_BATCH_LIMIT)}
      onDoubleClick={() => { if (!selecting && item.status !== 'missing') selectFile(item, true); }}
      onClick={() => {
        if (!selecting) selectFile(item);
        else setPicked(current => {
          if (!(item.id in current)) return { ...current, [item.id]: item.name };
          const next = { ...current }; delete next[item.id]; return next;
        });
      }}>
      <Icon size={18} /><span><strong>{item.name}{item.favorite && <Star className="library-favorite" size={13} aria-label="已收藏" />}</strong>
        <small>{[FORMATS[item.format], item.projectTopic, STATUSES[item.status]].filter(Boolean).join(' · ')}</small>
        {!!item.tags?.length && <small className="library-file-tags">{item.tags.map(entry => entry.name).join(' · ')}</small>}
        {hit && <><small>{hit.location?.label ?? '文件信息'}
          {hit.match === 'semantic' && ' · 语义相关'}{hit.match === 'both' && ' · 关键词及语义'}</small><span className="hit-excerpt">{hit.snippet}</span><small>{hit.path}</small></>}
      </span>
    </button>;
  }

  function openTrash(id: string) {
    setBatch(undefined); setSelecting(false); setPicked({}); setParams({ trash: id });
  }

  function prepareBatch(type: NonNullable<typeof batch>['action']) {
    const indexed = new Map(list.value?.map(file => [file.id, file]));
    setBatch({ action: type, files: Object.entries(picked).map(([id, name]) => ({ id, name, tags: indexed.get(id)?.tags })) });
  }

  function removeProcessed(ids: string[]) {
    const processed = new Set(ids);
    setPicked(current => Object.fromEntries(Object.entries(current).filter(([id]) => !processed.has(id))));
    refreshFiles();
  }

  const folders = <div className="panel library-types"><h2>文件夹</h2>
    <LibraryFolderList entries={actualFolders} selected={inTrash ? '' : directory} onSelect={folder => {
      setFilter('all'); refreshFiles(); setParams({ folder });
    }} onCreate={!inTrash && directories.value ? () => setFolderAction('create') : undefined}
      onMove={!inTrash && currentFolder?.parent ? () => setFolderAction('move') : undefined} />
    <ErrorNote message={directories.error} retry={directories.refresh} />
    <div className="library-types-list" aria-label="文件类别">
      {TYPES.map(({ key, label, icon: Icon }) => <button type="button" key={key}
        className={!inTrash && !directory && filter === key ? 'selected' : ''}
        aria-pressed={!inTrash && !directory && filter === key}
        onClick={() => { setFilter(key); refreshFiles(); setParams({}); }}><Icon size={17} />{label}</button>)}
    </div>
    <button className={inTrash ? 'selected' : ''} aria-pressed={inTrash} onClick={() => openTrash('')}><Trash2 size={17} />回收站</button>
  </div>;

  if (inTrash) return <section className="page library-page">
    <div className="page-head"><div><h1>知识库</h1><p>查看和恢复已移入回收站的原件。</p></div></div>
    <LibraryViewSwitch />
    <div className="library-grid">{folders}<TrashPanel selected={params.get('trash') ?? ''}
      onRestored={id => { setFilter('all'); setTag(''); setShelf('all'); setQuery(''); refreshFiles(); setParams({ file: id }); }} /></div>
  </section>;

  return <section className="page library-page">
    <div className="page-head">
      <div><h1>知识库</h1><p>搜索课程、补充讲解、笔记和资料正文。</p></div>
      <div className="action-row">
        <button type="button" className="button secondary" onClick={() => setCreatingNote(true)}><NotebookPen size={16} />新建笔记</button>
        <FileUpload onImport={imported} /><WebImportButton onImport={imported} />
        <button type="button" className="button secondary" onClick={() => showClassification(true)}>
          <Sparkles size={16} />AI 整理记录{classificationCount ? ` · ${classificationCount} 项` : ''}
        </button>
      </div>
    </div>
    <LibraryViewSwitch />
    {!fullFile && <><div className="library-toolbar">
      <label className="search"><Search size={17} /><input type="search" aria-label="搜索知识库" placeholder="搜索文件名或正文" maxLength={120} value={query} onChange={event => setQuery(event.target.value)} /></label>
      <select aria-label="搜索方式" value={searchMode} onChange={event => setSearchMode(event.target.value as typeof searchMode)}>
        <option value="keyword">关键词</option><option value="hybrid">关键词＋语义</option>
      </select>
      <select aria-label="搜索范围" value={scope} onChange={event => setScope(event.target.value)}><option value="current">当前文件夹</option><option value="all">全部知识库</option></select>
      <select aria-label="文件类型" value={format} onChange={event => setFormat(event.target.value)}><option value="">全部类型</option>{Object.entries(FORMATS).map(([value, name]) => <option key={value} value={value}>{name}</option>)}</select>
      <select aria-label="学习项目" value={project} onChange={event => setProject(event.target.value)}><option value="">全部项目</option>{projectOptions.value?.map(item => <option key={item.id} value={item.id}>{item.topic}</option>)}</select>
      <select aria-label="文件标签" value={tag} onChange={event => setTag(event.target.value)}>
        <option value="">全部标签</option>
        {Array.from(tags.entries()).sort((a, b) => a[1].localeCompare(b[1], 'zh-CN')).map(([key, name]) => <option key={key} value={key}>{name}</option>)}
        {tag && !tags.has(tag) && <option value={tag}>{tag}</option>}
      </select>
      <select aria-label="文件视图" value={shelf} onChange={event => {
        setShelf(event.target.value as FileShelf); refreshList(); refreshMatches();
      }}>
        <option value="all">所有文件</option><option value="favorites">收藏文件</option><option value="recent">最近访问</option>
      </select>
      <button className="button secondary" disabled={action.pending} onClick={rebuild}><RefreshCw size={16} />{action.pending ? '正在重建' : '重建索引'}</button>
      <button className="button secondary" aria-pressed={selecting} onClick={() => {
        setSelecting(!selecting); setPicked({}); setFileView();
      }}>{selecting ? '结束多选' : '多选文件'}</button>
    </div>
    {hybrid && semanticStatus?.state === 'disabled' && <div className="action-row library-semantic-status">
      <span>从 Hugging Face 下载约 {Math.round(semanticStatus.modelBytes / 1_000_000)} MB，之后在本机检索。</span>
      <button type="button" className="button secondary" disabled={semanticAction.pending} onClick={enableSemantic}>
        {semanticAction.pending ? '正在启用' : '启用语义搜索'}
      </button>
    </div>}
    {hybrid && semanticStatus?.state === 'preparing' && <p className="library-semantic-status" role="status">
      正在准备本地模型，可继续使用关键词搜索。
    </p>}
    {hybrid && semanticStatus?.state === 'indexing' && <p className="library-semantic-status" role="status">
      正在建立语义索引 · {semanticStatus.completed} / {semanticStatus.total} 个搜索条目
    </p>}
    {hybrid && <><ErrorNote message={semanticStatus?.message ?? semanticAction.error} retry={enableSemantic} />
      <ErrorNote message={semantic.error} retry={refreshSemantic} /></>}
    {selecting && <div className="action-row library-selection" aria-label="文件批量操作">
      <span role="status">已选 {selectedCount} 个文件{selectedCount >= LIBRARY_BATCH_LIMIT && ' · 达到单批上限'}</span>
      {selectedCount > CLASSIFICATION_FILE_LIMIT && <small>每批 AI 整理最多 {CLASSIFICATION_FILE_LIMIT} 份文件。</small>}
      <button type="button" className="button secondary" disabled={!selectedCount} onClick={() => prepareBatch('tag')}><Tags size={16} />批量标签</button>
      <button type="button" className="button secondary" disabled={!selectedCount || selectedCount > CLASSIFICATION_FILE_LIMIT}
        onClick={() => setClassifying(Object.entries(picked).map(([id, name]) => ({ id, name })))}>
        <Sparkles size={16} />AI 整理所选
      </button>
      <button type="button" className="button secondary" disabled={!selectedCount || !actualFolders.length} onClick={() => prepareBatch('move')}><FolderInput size={16} />批量移动</button>
      <button type="button" className="button secondary" disabled={!selectedCount || !actualFolders.length} onClick={() => prepareBatch('copy')}><Copy size={16} />批量复制</button>
      <button type="button" className="button secondary" disabled={!selectedCount} onClick={() => prepareBatch('export')}><Download size={16} />批量导出</button>
      <button type="button" className="button secondary" disabled={!selectedCount} onClick={() => prepareBatch('trash')}>
        <Trash2 size={16} />{selectedCount ? `移入回收站 · ${selectedCount} 项` : '移入回收站'}
      </button>
    </div>}
    <ErrorNote message={action.error} retry={rebuild} />
    <ErrorNote message={projectOptions.error} retry={projectOptions.refresh} />
    {rebuilt && <p className="library-index-status" role="status">索引已重建</p>}</>}
    <div className={`library-grid ${fullFile ? 'library-full-file' : selected || wikiTarget ? 'has-preview' : ''}`}>
      {!fullFile && <>{folders}
      <div className="panel library-list">
        <h2>{browsing ? currentFolder?.name ?? '文件' : '搜索结果'}</h2>
        {!!directory && directories.value && !currentFolder && <ErrorNote message="该文件夹已移动或不存在，请在左侧重新选择。" />}
        {browsing ? items ? items.length ? items.map(fileButton) : <Empty title="没有匹配的文件" /> : <BackedStatus error={list.error} retry={list.refresh} />
          : searchTerm !== query.trim() ? <Loading text="正在准备搜索" /> : matches.value ? <>
            {matches.value.hits.length ? matches.value.hits.map(fileButton) : <Empty title="没有匹配的内容" />}
            {matches.value.hasMore && <p className="result-count">可缩小搜索范围以查看其余命中。</p>}
          </> : <BackedStatus error={matches.error} retry={matches.refresh} />}
      </div></>}
      {wikiTarget && <WikiDestination target={wikiTarget} onClose={() => setFileView()} />}
      {selected && <FilePreview key={JSON.stringify([selected, at, revision])} id={selected} at={at} revision={revision}
        full={fullFile} onOpen={() => setFileView({ file: selected, open: '1', ...(at && { at }), ...(revision && { revision }) })}
        classificationRevision={classificationRevision} onClassify={file => setClassifying([file])}
        editing={params.has('edit')} onEdit={() => keepFileLayout({ file: selected, edit: '1' })}
        onPreview={() => keepFileLayout({ file: selected })}
        onChanged={refreshFiles} onViewed={refreshRecent} folders={actualFolders}
        onRelocated={path => {
          refreshFiles();
          keepFileLayout({ file: selected, ...(directory && { folder: parentFolder(path) }) });
        }}
        onPage={(number, originalRevision) => {
          keepFileLayout({ file: selected, ...(number === null ? {} : { at: String(number) }),
            ...(originalRevision ? { revision: originalRevision } : {}) });
        }}
        onReviewed={(number, textRevision) => {
          refreshFiles();
          keepFileLayout({ file: selected, at: String(number), revision: textRevision });
        }}
        onClose={() => setFileView()} onLatest={() => {
          refreshFiles();
          keepFileLayout({ file: selected });
        }} onTrash={openTrash} />}
    </div>
    {creatingNote && <NewNoteDialog onClose={() => setCreatingNote(false)} onDone={note => {
      setCreatingNote(false);
      setFilter('note'); setQuery(''); setSearchTerm(''); setFormat(''); setProject(''); setTag(''); setShelf('all');
      refreshFiles();
      setParams({ file: 'note-' + note.id, edit: '1' });
    }} />}
    {batch?.action === 'trash' && <MoveToTrash files={batch.files} onClose={() => setBatch(undefined)} onMoved={openTrash}
      onPartial={removeProcessed} />}
    {batch?.action === 'tag' && <FileTagDialog files={batch.files} onClose={() => setBatch(undefined)}
      onDone={() => {
        setBatch(undefined); refreshFiles();
      }} />}
    {batch && batch.action !== 'tag' && batch.action !== 'trash' && <FileBatchDialog mode={batch.action}
      files={batch.files} folders={actualFolders} onClose={() => setBatch(undefined)} onProcessed={removeProcessed} />}
    {folderAction && <FolderDialog entries={actualFolders}
      parent={(folderAction === 'move' ? currentFolder?.parent : currentFolder?.path) ?? LIBRARY_ROOT}
      source={folderAction === 'move' && currentFolder ? currentFolder : undefined}
      onClose={() => setFolderAction(undefined)}
      onDone={path => {
        setFolderAction(undefined); refreshFiles(); setParams({ folder: path });
      }} />}
    {classifying && <ClassificationRequest files={classifying} folders={actualFolders}
      close={() => setClassifying(undefined)} queued={() => { setClassifying(undefined); showClassification(true); }} />}
    {viewingClassification && <ClassificationReview queue={classifications.value} error={classifications.error}
      refresh={refreshClassifications} close={() => showClassification(false)} />}
  </section>;
}

function OriginalPageImage({ id, page, revision, isPdf }: { id: string; page: number; revision: string; isPdf: boolean }) {
  const [result, setResult] = useState<{ url?: string; error?: string }>({});
  useEffect(() => {
    const controller = new AbortController();
    let objectUrl: string | undefined;
    const query = new URLSearchParams({ revision });
    apiResponse(`/files/${encodeURIComponent(id)}/pages/${page}/image?${query}`, 'GET', undefined, undefined, controller.signal)
      .then(response => response.blob()).then(image => {
        if (controller.signal.aborted) return;
        objectUrl = URL.createObjectURL(image);
        setResult({ url: objectUrl });
      }).catch((error: Error) => {
        if (!controller.signal.aborted) setResult({ error: error.message });
      });
    return () => { controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [id, page, revision]);
  if (result.error) return <ErrorNote message={result.error} />;
  return result.url
    ? <img className="pdf-page" src={result.url} alt={isPdf ? `PDF 原件第 ${page} 页` : '导入的原图'} />
    : <Loading text={isPdf ? '正在渲染原页' : '正在打开原图'} />;
}

type OcrText = { text: string; truncated: boolean };

function PageRecognition({ id, page, revision, isPdf, anchor, onReviewed }: {
  id: string; page: number; revision: string; isPdf: boolean; anchor: LibraryFile['anchor'];
  onReviewed: (page: number, textRevision: string) => void;
}) {
  const action = useAction();
  const [draft, setDraft] = useState<OcrText & { baseTextRevision: string | null }>();
  const [externalPending, setExternalPending] = useState(false);
  const busy = action.pending || externalPending;
  const endpoint = `/files/${encodeURIComponent(id)}/pages/${page}/ocr`;
  const baseTextRevision = anchor?.textRevision ?? null;

  function recognize() {
    void action.run(async () => {
      const result = await api<OcrText>(endpoint, 'POST', { revision });
      setDraft({ ...result, baseTextRevision });
    });
  }

  async function recognizeExternally(authorizationId: string, signal: AbortSignal) {
    setExternalPending(true);
    try {
      const result = await api<OcrText>(endpoint + '/external', 'POST', { revision, authorizationId }, signal);
      signal.throwIfAborted();
      setDraft({ ...result, baseTextRevision });
    } finally {
      setExternalPending(false);
    }
  }

  function save() {
    if (busy || !draft?.text.trim()) return;
    void action.run(async () => {
      const saved = await api<{ textRevision: string }>(endpoint, 'PUT', {
        revision, textRevision: draft.baseTextRevision, text: draft.text,
      });
      setDraft(undefined);
      onReviewed(page, saved.textRevision);
    });
  }

  return <section className="ocr-editor" aria-label="识别与校对">
    {draft ? <form onSubmit={event => { event.preventDefault(); save(); }}>
      <label className="project-field">校对文字
        <textarea rows={6} maxLength={30_000} value={draft.text} disabled={busy}
          onChange={event => setDraft({ ...draft, text: event.target.value })} />
      </label>
      <p className="file-status">请对照原件检查识别文字和公式。保存后可按本页文字搜索。</p>
      {draft.truncated && <p className="file-status" role="status">识别结果可能只包含本页部分文字，请对照原件补全。</p>}
      <div className="action-row">
        <button type="submit" className="button primary" disabled={busy || !draft.text.trim()}>
          {action.pending ? '正在保存' : '保存校对文字'}
        </button>
        <button type="button" className="button secondary" disabled={busy} onClick={() => setDraft(undefined)}>取消</button>
      </div>
    </form> : <div className="action-row">
      <button type="button" className="button secondary" disabled={busy} onClick={recognize}>
        {action.pending ? '正在本机识别' : isPdf ? '识别本页' : '识别图片'}
      </button>
      <button type="button" className="text-action" disabled={busy}
        onClick={() => setDraft({ text: anchor?.text ?? '', truncated: false, baseTextRevision })}>
        {anchor?.textRevision ? '编辑校对文字' : '手动录入'}
      </button>
    </div>}
    <ErrorNote message={action.error} />
    <ExternalRecognition kind="ocr" description={isPdf ? `本次选择 PDF 第 ${page} 页` : '本次选择当前图片'}
      disabled={action.pending} replacesDraft={!!draft?.text.trim()} recognize={recognizeExternally} />
  </section>;
}

function WebSourceInfo({ source, hasOriginal }: { source: WebSource; hasOriginal: boolean }) {
  const allowed = /^https?:\/\//i.test(source.url);
  const saved = new Date(source.fetchedAt);
  return <dl className="web-source">
    <div><dt>网页来源</dt><dd>
      {allowed ? <a href={source.url} target="_blank" rel="noreferrer">{source.url}</a> : source.url}
    </dd></div>
    {source.requestedUrl !== source.url && <div><dt>导入地址</dt><dd>{source.requestedUrl}</dd></div>}
    <div><dt>保存时间</dt><dd><time dateTime={source.fetchedAt}>{saved.toLocaleString('zh-CN')}</time></dd></div>
    {!source.matchesSnapshot && hasOriginal && <div><dt>快照状态</dt><dd>本地文件已编辑，来源地址对应最初保存的网页。</dd></div>}
  </dl>;
}

function FilePreview({ id, at, revision, full, onOpen, editing, onEdit, onPreview, onChanged, onViewed, onPage, onReviewed, onClose, onLatest, onTrash,
  folders, onRelocated, classificationRevision, onClassify }: {
  id: string; at: string | null; revision: string | null;
  full: boolean; onOpen: () => void;
  editing: boolean; onEdit: () => void; onPreview: () => void; onChanged: () => void; onViewed: () => void;
  onPage: (page: number | null, originalRevision: string | null) => void;
  onReviewed: (page: number, textRevision: string) => void; onClose: () => void;
  onLatest: () => void; onTrash: (id: string) => void;
  folders: LibraryFolder[]; onRelocated: (path: string) => void;
  classificationRevision: number; onClassify: (file: Pick<LibraryItem, 'id' | 'name'>) => void;
}) {
  const query = new URLSearchParams();
  if (at) query.set('at', at);
  if (revision) query.set('revision', revision);
  const file = useRead<LibraryFile>(`/files/${encodeURIComponent(id)}?${query}`);
  const { refresh: refreshFile } = file;
  const download = useAction();
  const favorite = useAction();
  const [repairing, setRepairing] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [openedEditor, setOpenedEditor] = useState(false);
  const [noteSettings, setNoteSettings] = useState(false);
  const [moving, setMoving] = useState(false);
  const [tagging, setTagging] = useState(false);
  const [visitError, setVisitError] = useState(false);
  const anchor = useRef<HTMLDivElement>(null);
  const lastClassification = useRef(classificationRevision);
  const item = file.value;
  const canOpen = !!item && !['missing', 'trashed', 'purged'].includes(item.status);
  const noteId = item?.kind === 'note' && !['trashed', 'purged'].includes(item.status) ? id.slice('note-'.length) : undefined;
  const showEditor = editing && !!noteId;

  useEffect(() => {
    if (showEditor) setOpenedEditor(true);
  }, [showEditor]);

  useEffect(() => {
    if (lastClassification.current === classificationRevision) return;
    lastClassification.current = classificationRevision;
    refreshFile();
  }, [classificationRevision, refreshFile]);

  useEffect(() => {
    if (!canOpen) return;
    const controller = new AbortController();
    void api(`/files/${encodeURIComponent(id)}/opened`, 'POST', undefined, controller.signal).then(() => {
      setVisitError(false);
      onViewed();
    }).catch(() => {
      if (!controller.signal.aborted) setVisitError(true);
    });
    return () => controller.abort();
  }, [canOpen, id, onViewed]);

  function editedNote() {
    file.refresh();
    onChanged();
  }
  const anchorNumber = item?.anchor?.number;

  useEffect(() => {
    if (at && anchorNumber) anchor.current?.scrollIntoView({ block: 'nearest' });
  }, [at, anchorNumber]);

  function saveOriginal(name: string) {
    void download.run(async () => {
      const response = await apiResponse(`/files/${encodeURIComponent(id)}/original`);
      downloadFile(await response.blob(), name);
    });
  }

  function toggleFavorite() {
    if (!item) return;
    void favorite.run(async () => {
      await api(`/files/${encodeURIComponent(id)}/favorite`, 'PUT', { favorite: !item.favorite });
      file.refresh();
      onChanged();
    });
  }

  const isPdf = item?.format === 'pdf';
  const isWeb = item?.format === 'html';
  const currentPage = item?.pageCount ? item.anchor?.number : undefined;
  const showAnchor = item?.anchor && (at || item.anchor.textRevision);
  return <article className="panel library-preview">
    <div className="drawer-head"><h2>{item?.name ?? '文件预览'}</h2>
      {full && !showEditor ? <button type="button" className="button secondary" onClick={onClose}>
        <ChevronLeft size={16} />返回文件列表
      </button> : <button className="icon-button" aria-label={showEditor ? '关闭编辑' : '关闭预览'}
        onClick={showEditor ? onPreview : onClose}><X size={19} /></button>}</div>
    {noteId && (showEditor || openedEditor) && <div className="note-panel library-note-panel" hidden={!showEditor}>
      <div className="file-preview-actions">
        {canOpen && !full && <button type="button" className="button secondary" onClick={onOpen}>
          <FileText size={16} />打开文件
        </button>}
        <button type="button" className="button secondary" onClick={onPreview}>返回预览</button>
        <button type="button" className="text-action" onClick={() => setNoteSettings(true)}>标题与课程关联</button>
      </div>
      <NotePanel noteId={noteId} visible={showEditor} label="笔记正文" showLibraryLink={false} onChange={editedNote} />
    </div>}
    {showEditor && noteId && noteSettings && <NoteDetailsDialog noteId={noteId} onClose={() => setNoteSettings(false)}
      onDone={() => { setNoteSettings(false); editedNote(); }} />}
    {!showEditor && (item ? <>
      <p className="file-path" title={item.path}>{item.path}</p>
      {!!item.tags?.length && <ul className="file-tags" aria-label="文件标签">
        {item.tags.map(entry => <li key={entry.key}>{entry.name}</li>)}
      </ul>}
      <div className="file-preview-actions">
        {noteId && <button type="button" className="button secondary" onClick={onEdit}><NotebookPen size={16} />编辑笔记</button>}
        {canOpen && <button className="button secondary" disabled={download.pending} onClick={() => saveOriginal(item.name)}>
          <Download size={16} />{isWeb ? '下载网页快照' : '下载原件'}
        </button>}
        {canOpen && <button type="button" className="button secondary" onClick={() => onClassify(item)}>
          <Sparkles size={16} />AI 整理此文件
        </button>}
        {canOpen && !!folders.length && <button type="button" className="button secondary" onClick={() => setMoving(true)}>
          <PencilLine size={16} />移动或重命名文件
        </button>}
        {canOpen && <button className="button secondary" onClick={() => setDeleting(true)}><Trash2 size={16} />移入回收站</button>}
        {!['trashed', 'purged'].includes(item.status) && <>
          <button type="button" className="button secondary" aria-pressed={!!item.favorite} disabled={favorite.pending}
            onClick={toggleFavorite}><Star size={16} />{item.favorite ? '取消收藏' : '收藏文件'}</button>
          <button type="button" className="button secondary" onClick={() => setTagging(true)}><Tags size={16} />编辑标签</button>
        </>}
        {item.status === 'trashed' && item.trashId && <button className="button secondary" onClick={() => onTrash(item.trashId!)}>查看回收站</button>}
        {item.status === 'missing' && <button className="button secondary" onClick={() => setRepairing(true)}>修复文件位置</button>}
        {isPdf && currentPage && item.pageCount && <div className="file-pagination">
          <button className="icon-button" aria-label="上一页" disabled={currentPage === 1}
            onClick={() => onPage(currentPage - 1, item.revision)}><ChevronLeft size={18} /></button>
          <span>第 {currentPage} / {item.pageCount} 页</span>
          <button className="icon-button" aria-label="下一页" disabled={currentPage === item.pageCount}
            onClick={() => onPage(currentPage + 1, item.revision)}><ChevronRight size={18} /></button>
          {item.pageCount > 5 && <form className="file-pagination" onSubmit={event => {
            event.preventDefault();
            const page = Number(new FormData(event.currentTarget).get('page'));
            if (Number.isInteger(page) && page >= 1 && page <= item.pageCount!) onPage(page, item.revision);
          }}>
            <input type="number" name="page" min={1} max={item.pageCount} required aria-label="跳到页码" />
            <button type="submit" className="button secondary">跳转</button>
          </form>}
        </div>}
      </div>
      <ErrorNote message={download.error} />
      <ErrorNote message={favorite.error} />
      {visitError && <p className="file-status" role="status">最近访问时间暂时无法保存。</p>}
      {item.webSource && <WebSourceInfo source={item.webSource} hasOriginal={!!item.revision} />}
      {!!item.sections?.length && <label className="project-field web-sections">网页分节
        <select aria-label="网页分节" value={at ?? ''} onChange={event => onPage(event.target.value ? Number(event.target.value) : null, item.revision)}>
          <option value="">全部已提取正文</option>
          {item.sections.map(entry => <option key={entry.number} value={entry.number}>{entry.label}</option>)}
        </select>
      </label>}
      {item.message && <p className="file-status" role="status">{item.message}</p>}
      {!['trashed', 'purged'].includes(item.status) && <WikiBacklinks file={item.id} />}
      {isPdf && item.unreadablePages.length > 0 && <p className="file-status">第 {item.unreadablePages.join('、')} 页无可提取文字</p>}
      {showAnchor && item.anchor && <div className="file-anchor" ref={anchor}>
        <strong>{item.anchor.label}</strong>{item.anchor.text && <pre>{item.anchor.text}</pre>}
      </div>}
      {currentPage && item.revision ? <>
        <OriginalPageImage id={id} page={currentPage} revision={item.revision} isPdf={isPdf} />
        {!showAnchor && item.content && <pre className="plain-file">{item.content}</pre>}
        <PageRecognition key={`${id}:${item.revision}:${currentPage}`} id={id} page={currentPage} revision={item.revision}
          isPdf={isPdf} anchor={item.anchor} onReviewed={onReviewed} />
      </> : item.content && !(isWeb && showAnchor)
        ? item.markdown ? <Markdown remarkPlugins={[remarkMath]}>{item.content}</Markdown>
          : <pre className="plain-file">{item.format === 'txt' ? <WikiText text={item.content} /> : item.content}</pre> : null}
    </> : file.error && (revision || at) ? <><ErrorNote message={file.error} /><button className="button secondary" onClick={onLatest}>查看当前版本</button></> : <BackedStatus error={file.error} retry={file.refresh} />)}
    {item && repairing && <RepairFile item={item} onClose={() => setRepairing(false)} onRepaired={() => {
      setRepairing(false);
      file.refresh();
      onLatest();
    }} />}
    {item && deleting && <MoveToTrash files={[item]} onClose={() => setDeleting(false)} onMoved={onTrash} />}
    {item && tagging && <FileTagDialog files={[item]} onClose={() => setTagging(false)} onDone={() => {
      setTagging(false); file.refresh(); onChanged();
    }} />}
    {item && moving && <FolderDialog source={item} parent={parentFolder(item.path)} entries={folders}
      onClose={() => setMoving(false)} onDone={path => {
        setMoving(false); file.refresh(); onRelocated(path);
      }} />}
  </article>;
}

type RepairOptions = { candidates: string[]; hasMore: boolean };
type RepairPreview = { path: string; revision: string; sameContent: boolean | null; size: number; excerpt: string | null };

function RepairFile({ item, onClose, onRepaired }: { item: LibraryItem; onClose: () => void; onRepaired: () => void }) {
  const endpoint = `/files/${encodeURIComponent(item.id)}/repair`;
  const [path, setPath] = useState('');
  const [preview, setPreview] = useState<RepairPreview>();
  const options = useRead<RepairOptions>(endpoint);
  const action = useAction();
  function selectPath(path: string) {
    setPath(path);
    setPreview(undefined);
  }
  function inspect() {
    setPreview(undefined);
    void action.run(async () => setPreview(await api<RepairPreview>(`${endpoint}/preview`, 'POST', { path })));
  }
  function confirm() {
    if (!preview) return;
    void action.run(async () => {
      try {
        await api(endpoint, 'PUT', { path: preview.path, revision: preview.revision });
        onRepaired();
      } catch (error) {
        setPreview(undefined);
        throw error;
      }
    });
  }
  return <Modal title="修复文件位置" onClose={onClose} busy={action.pending}>
    <div className="project-dialog-form location-dialog">
      <div><strong>原位置</strong><p className="file-path">{item.path}</p></div>
      <label className="project-field">从知识库选择同类型文件
        <select value={options.value?.candidates.includes(path) ? path : ''} disabled={action.pending || !options.value?.candidates.length}
          onChange={event => selectPath(event.target.value)}>
          <option value="">{options.value?.candidates.length ? '选择候选文件' : options.value ? '暂无候选文件' : '正在查找候选文件'}</option>
          {options.value?.candidates.map(candidate => <option key={candidate} value={candidate}>{candidate}</option>)}
        </select>
      </label>
      {options.value?.hasMore && <p className="file-status">候选较多，输入相对路径可定位其他文件。</p>}
      <label className="project-field">候选文件路径
        <input maxLength={1400} value={path} placeholder="library/资料/参考.md" disabled={action.pending}
          onChange={event => selectPath(event.target.value)} /></label>
      <ErrorNote message={options.error} retry={options.refresh} />
      <ErrorNote message={action.error} />
      {preview && <div className={`notice location-preview ${preview.sameContent === true ? 'success' : ''}`}>
        <strong>{preview.sameContent === true ? '内容与已知版本一致' : preview.sameContent === false ? '内容与已知版本不同' : '没有可比较的旧版本'}</strong>
        <span>{preview.path} · {preview.size.toLocaleString()} 字节</span>
        {preview.excerpt && <pre>{preview.excerpt}</pre>}
      </div>}
      <div className="project-dialog-actions">
        <button className="button secondary" disabled={action.pending} onClick={onClose}>取消</button>
        <button className="button secondary" disabled={action.pending || !path} onClick={inspect}>预览候选</button>
        <button className="button primary" disabled={action.pending || !preview} onClick={confirm}>确认关联</button>
      </div>
    </div>
  </Modal>;
}

