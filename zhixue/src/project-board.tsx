import { useEffect, useRef, useState, type FormEvent } from 'react';
import { ArrowDown, ArrowDownUp, ArrowLeft, ArrowRight, ArrowUp, Bookmark, ChevronLeft, ChevronRight, Columns3,
  LayoutGrid, List, Plus, Search, SlidersHorizontal, X } from 'lucide-react';
import { Link } from 'react-router-dom';
import { api, apiResponse, courseUrl, projectUrl, type ProjectSummary } from './api';
import { conditionLabel, ProjectFilters } from './project-filters';
import { DEFAULT_VIEW, PRIORITIES, SORT_LABELS, STATUS_LABELS, type BoardResult, type ColumnSetting,
  type PageKey, type PageNumbers, type ProjectPageResult, type SavedView, type SortOrder, type ViewDefinition } from './project-view';
import { Empty, ErrorNote, Loading, Modal, useAction, useRead } from './shared';

const BROWSE_KEY = 'zhixue-project-browse-v1';
const LAYOUTS = [
  { key: 'board', name: '看板', Icon: Columns3 },
  { key: 'table', name: '列表', Icon: List },
  { key: 'cards', name: '卡片', Icon: LayoutGrid },
] as const;
const REVIEW_LABELS = {
  none: '暂无复习安排', complete: '复习周期已结束', paused: '复习安排已暂停',
  due: '待复习', upcoming: '无到期复习',
};
type Browse = { view: ViewDefinition; pages: PageNumbers; savedId: string };
type ReadBoard = { key: string; value?: BoardResult; layout?: ViewDefinition['layout']; summaryAtRequest?: string; error?: string };
type Panel = 'filters' | 'sort' | 'groups' | 'save' | 'delete';

function loadBrowse(): Browse {
  try {
    const restored = JSON.parse(sessionStorage.getItem(BROWSE_KEY) ?? 'null') as Browse | null;
    if (restored?.view?.columns && restored.pages) return restored;
  } catch { /* The board can still be used without stored browsing state. */ }
  return { view: DEFAULT_VIEW, pages: {}, savedId: '' };
}

function ProjectCard({ project, statusLabel }: { project: ProjectSummary; statusLabel: boolean }) {
  const next = project.nextLesson;
  return <article className={`project-card ${project.status}`}>
    <div className="project-card-title">
      <h3><Link to={projectUrl(project.id)}>{project.topic}</Link></h3>
      {statusLabel && <span className="project-state"><i className="status-dot" />{STATUS_LABELS[project.status]}</span>}
      {(project.tags.length > 0 || project.priority) && <div className="project-tags">
        {project.tags.slice(0, 3).map(tag => <span className="tag" key={tag}>{tag}</span>)}
        {project.tags.length > 3 && <span className="tag" title={project.tags.slice(3).join('、')}>+{project.tags.length - 3}</span>}
        {project.priority && <span className="project-priority">{PRIORITIES[project.priority]}优先级</span>}
      </div>}
    </div>
    <p className="project-goal" title={project.outcome}>{project.outcome}</p>
    <div className="project-learning">
      {project.progress === null ? <p>尚无进度</p> : <>
        <div className="progress-label"><span>已学 {project.learned} / {project.lessonCount} 节</span><strong>{project.progress}%</strong></div>
        <progress value={project.progress} max={100} aria-label="已学进度" />
      </>}
      {next ? <p className="next-lesson"><span>{next.phase}</span><strong>{next.title}</strong></p>
        : project.lessonCount > 0 ? <p>全部课程已学完</p> : <p>学习路线待创建</p>}
    </div>
    <div className="project-foot">
      <span className={`project-review ${project.reviewStatus}`}>{project.due
        ? `${project.due} 个目标待复习` : REVIEW_LABELS[project.reviewStatus]}</span>
      <Link className="text-action" to={next?.ready && project.status === 'active' ? courseUrl(next.id) : projectUrl(project.id)}>
        {next?.ready && project.status === 'active' ? '继续学习' : '查看项目'} <ArrowRight size={15} />
      </Link>
    </div>
  </article>;
}

function PageGroup({ id, data, layout, disabled, onPage }: {
  id: PageKey; data: ProjectPageResult; layout: ViewDefinition['layout']; disabled: boolean; onPage: (page: number) => void;
}) {
  const label = id === 'all' ? '匹配项目' : STATUS_LABELS[id];
  return <section className={`board-column ${id} layout-${layout}`} aria-label={label}>
    {id !== 'all' && <h2><span className="status-dot" />{label} <span className="count">{data.total}</span></h2>}
    <div className="column-cards">
      {data.items.map(project => <ProjectCard key={project.id} project={project} statusLabel={id === 'all'} />)}
      {!data.total && <p className="column-empty">没有符合条件的项目</p>}
    </div>
    <div className="pager">
      {data.total === 0 ? <span>0 项</span> : <>
        <span>{data.start}–{data.end} / {data.total}</span>
        <button type="button" className="icon-button" aria-label={`${label}上一页`} disabled={disabled || data.page <= 1} onClick={() => onPage(data.page - 1)}><ChevronLeft size={17} /></button>
        <span>第 {data.page} / {data.lastPage} 页</span>
        <button type="button" className="icon-button" aria-label={`${label}下一页`} disabled={disabled || data.page >= data.lastPage} onClick={() => onPage(data.page + 1)}><ChevronRight size={17} /></button>
      </>}
    </div>
  </section>;
}

function SortDialog({ sort, onClose, onApply }: { sort: SortOrder; onClose: () => void; onApply: (order: SortOrder) => void }) {
  const [value, setValue] = useState(sort);
  return <Modal title="项目排序" onClose={onClose}>
    <form className="project-dialog-form" onSubmit={event => { event.preventDefault(); onApply(value); }}>
      <label className="project-field">排序字段<select value={value.key} onChange={event => setValue({ ...value, key: event.target.value as SortOrder['key'] })}>
        {Object.entries(SORT_LABELS).map(([key, text]) => <option key={key} value={key}>{text}</option>)}
      </select></label>
      <label className="project-field">排序方向<select value={value.direction} onChange={event => setValue({ ...value, direction: event.target.value as SortOrder['direction'] })}>
        <option value="asc">升序</option><option value="desc">降序</option>
      </select></label>
      <div className="project-dialog-actions"><button type="button" className="button secondary" onClick={onClose}>取消</button>
        <button className="button primary">应用排序</button></div>
    </form>
  </Modal>;
}

function GroupDialog({ columns, onClose, onApply }: {
  columns: ColumnSetting[]; onClose: () => void; onApply: (columns: ColumnSetting[]) => void;
}) {
  const [value, setValue] = useState(columns);
  const visibleCount = value.filter(column => column.visible).length;
  function move(index: number, to: number) {
    const changed = [...value];
    [changed[index], changed[to]] = [changed[to], changed[index]];
    setValue(changed);
  }
  return <Modal title="分组设置" onClose={onClose}>
    <form className="project-dialog-form" onSubmit={event => { event.preventDefault(); onApply(value); }}>
      <p className="dialog-hint">看板按项目状态分组。至少显示一列。</p>
      <div className="project-group-list">{value.map((column, index) => {
        const name = STATUS_LABELS[column.key];
        return <div className="project-group-row" key={column.key}>
          <label><input type="checkbox" checked={column.visible} disabled={column.visible && visibleCount === 1}
            onChange={event => setValue(current => current.map(item => item.key === column.key ? { ...item, visible: event.target.checked } : item))} />{name}</label>
          <button type="button" className="icon-button" disabled={index === 0} aria-label={`${name}上移`} onClick={() => move(index, index - 1)}><ArrowUp size={16} /></button>
          <button type="button" className="icon-button" disabled={index === value.length - 1} aria-label={`${name}下移`} onClick={() => move(index, index + 1)}><ArrowDown size={16} /></button>
        </div>;
      })}</div>
      <div className="project-dialog-actions"><button type="button" className="button secondary" onClick={onClose}>取消</button>
        <button className="button primary">应用分组</button></div>
    </form>
  </Modal>;
}

function SaveDialog({ view, selected, onClose, onSaved }: {
  view: ViewDefinition; selected?: SavedView; onClose: () => void; onSaved: (saved: SavedView) => void;
}) {
  const [editing, setEditing] = useState(Boolean(selected));
  const [name, setName] = useState(selected?.name ?? '');
  const action = useAction();
  function save(event: FormEvent) {
    event.preventDefault();
    if (!name.trim()) return;
    void action.run(async () => {
      const path = editing && selected ? `/project-views/${selected.id}` : '/project-views';
      const saved = await api<SavedView>(path, editing ? 'PUT' : 'POST', { name, definition: view });
      onSaved(saved);
    });
  }
  return <Modal title={editing ? '更新视图' : '保存视图'} onClose={onClose} busy={action.pending}>
    <form className="project-dialog-form" onSubmit={save}>
      <label className="project-field">视图名称<input value={name} maxLength={80} required disabled={action.pending} onChange={event => setName(event.target.value)} /></label>
      <ErrorNote message={action.error} />
      <div className="project-dialog-actions">
        {editing && <button type="button" className="text-action clear-draft" disabled={action.pending} onClick={() => { setEditing(false); setName(''); }}>另存为新视图</button>}
        <button type="button" className="button secondary" disabled={action.pending} onClick={onClose}>取消</button>
        <button className="button primary" disabled={action.pending || !name.trim()}>{editing ? '更新视图' : '保存视图'}</button>
      </div>
    </form>
  </Modal>;
}

function DeleteDialog({ view, onClose, onDeleted }: { view: SavedView; onClose: () => void; onDeleted: () => void }) {
  const action = useAction();
  return <Modal title="删除视图" onClose={onClose} busy={action.pending}>
    <div className="project-dialog-form">
      <p className="dialog-hint">删除「{view.name}」后，它将从项目视图菜单移除。</p>
      <ErrorNote message={action.error} />
      <div className="project-dialog-actions">
        <button type="button" className="button secondary" disabled={action.pending} onClick={onClose}>取消</button>
        <button type="button" className="button danger" disabled={action.pending} onClick={() => {
          void action.run(async () => { await apiResponse(`/project-views/${view.id}`, 'DELETE'); onDeleted(); });
        }}>删除视图</button>
      </div>
    </div>
  </Modal>;
}

export function ProjectBoard({ boardStamp, onRefresh }: { boardStamp: string; onRefresh: () => void }) {
  const [browse, setBrowse] = useState(loadBrowse);
  const [panel, setPanel] = useState<Panel>();
  const [read, setRead] = useState<ReadBoard>();
  const [reload, setReload] = useState(0);
  const saves = useRead<SavedView[]>('/project-views');
  const tagOptions = useRead<string[]>(panel === 'filters' ? '/project-tags' : null);
  const summaryStamp = useRef(boardStamp);
  const { view, pages, savedId } = browse;
  const selected = saves.value?.find(item => item.id === savedId);
  const request = JSON.stringify({ view, pages });
  const loadKey = `${reload}:${request}`;
  const loading = read?.key !== loadKey;
  const result = read?.value;
  const stale = result && boardStamp !== result.stamp && boardStamp !== read.summaryAtRequest;
  const filtered = Boolean(view.search.trim() || view.filters.length);
  const dirty = selected && JSON.stringify(selected.definition) !== JSON.stringify(view);

  useEffect(() => {
    try { sessionStorage.setItem(BROWSE_KEY, JSON.stringify(browse)); }
    catch { /* Keep browsing state in memory if storage is unavailable. */ }
  }, [browse]);
  useEffect(() => { summaryStamp.current = boardStamp; }, [boardStamp]);
  useEffect(() => {
    const controller = new AbortController();
    const payload = JSON.parse(request) as Pick<Browse, 'pages' | 'view'>;
    const timeout = window.setTimeout(() => {
      const summaryAtRequest = summaryStamp.current;
      api<BoardResult>('/project-board/query', 'POST', payload, controller.signal).then(value => {
        if (controller.signal.aborted) return;
        setRead({ key: loadKey, value, layout: payload.view.layout, summaryAtRequest });
        const clamped = Object.entries(value.pages).filter(([key, data]) => data && data.page !== (payload.pages[key as PageKey] ?? 1));
        if (clamped.length) {
          setBrowse(current => JSON.stringify({ view: current.view, pages: current.pages }) === request
            ? { ...current, pages: { ...current.pages, ...Object.fromEntries(clamped.map(([key, data]) => [key, data?.page])) } } : current);
        }
      }).catch(error => {
        if (!controller.signal.aborted) setRead(previous => ({ ...previous, key: loadKey, error: error.message }));
      });
    }, 130);
    return () => { window.clearTimeout(timeout); controller.abort(); };
  }, [loadKey, request]);

  function editView(update: Partial<ViewDefinition>) {
    setBrowse(current => ({ ...current, view: { ...current.view, ...update }, pages: {} }));
    setPanel(undefined);
  }
  function refresh() {
    setReload(value => value + 1);
    onRefresh();
  }
  const close = () => setPanel(undefined);
  const groups = result && Object.entries(result.pages).map(([key, data]) => data && <PageGroup key={key} id={key as PageKey}
    data={data} layout={read?.layout ?? 'board'} disabled={loading || Boolean(read?.error)}
    onPage={page => setBrowse(current => ({ ...current, pages: { ...current.pages, [key]: page } }))} />);
  const hiddenMatches = result && read?.layout === 'board'
    ? result.total - Object.values(result.pages).reduce((count, page) => count + (page?.total ?? 0), 0) : 0;

  return <section className="page project-board-page">
    <div className="page-head"><div><h1>学习项目</h1><p>按状态查看进度和待复习目标。</p></div>
      <Link className="button primary" to="/"><Plus size={17} />新建项目</Link>
    </div>
    <div className="board-controls board-views">
      <select aria-label="项目视图" value={selected?.id ?? ''} onChange={event => {
        const chosen = saves.value?.find(item => item.id === event.target.value);
        setBrowse({ savedId: chosen?.id ?? '', view: chosen?.definition ?? DEFAULT_VIEW, pages: {} });
      }}>
        <option value="">全部项目</option>{saves.value?.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select>
      <div className="segmented" aria-label="项目显示方式">{LAYOUTS.map(({ key, name, Icon }) =>
        <button key={key} type="button" aria-pressed={view.layout === key} onClick={() => {
          if (view.layout !== key) editView({ layout: key });
        }}><Icon size={17} />{name}</button>)}</div>
      <div className="board-view-actions">
        {dirty && <small>有未保存更改</small>}
        <button type="button" className="button secondary" onClick={() => setPanel('save')}><Bookmark size={16} />{selected ? '更新视图' : '保存视图'}</button>
        {selected && <button type="button" className="text-action" onClick={() => setPanel('delete')}>删除视图</button>}
      </div>
    </div>
    <ErrorNote message={saves.error} retry={saves.refresh} />
    <div className="board-controls">
      <label className="search"><Search size={17} /><input type="search" aria-label="搜索项目名称或目标" placeholder="搜索项目名称或目标"
        value={view.search} maxLength={500} onChange={event => editView({ search: event.target.value })} /></label>
      <button type="button" className={`button ${view.filters.length ? 'primary' : 'secondary'}`} onClick={() => setPanel('filters')}><SlidersHorizontal size={17} />筛选{view.filters.length > 0 && <span>{view.filters.length}</span>}</button>
      <button type="button" className="button secondary" aria-label="排序" onClick={() => setPanel('sort')}>
        <ArrowDownUp size={16} />{SORT_LABELS[view.sort.key]} {view.sort.direction === 'asc' ? '↑' : '↓'}
      </button>
      {view.layout === 'board' && <button type="button" className="button secondary" onClick={() => setPanel('groups')}><Columns3 size={16} />分组：状态</button>}
      {view.layout === 'board'
        ? <select aria-label="每列项目数" value={view.boardSize} onChange={event => editView({ boardSize: Number(event.target.value) as ViewDefinition['boardSize'] })}>
          {[3, 5, 10].map(size => <option key={size} value={size}>每列 {size} 个</option>)}</select>
        : <select aria-label="每页项目数" value={view.pageSize} onChange={event => editView({ pageSize: Number(event.target.value) as ViewDefinition['pageSize'] })}>
          {[12, 24, 48].map(size => <option key={size} value={size}>每页 {size} 个</option>)}</select>}
    </div>
    <div className="board-result-line">
      {view.filters.length > 1 && <span className="board-combinator">{view.combinator === 'and' ? '全部满足' : '任一满足'}</span>}
      {view.filters.map((filter, index) => <button type="button" className="chip condition-chip" key={index}
        aria-label={`移除条件：${conditionLabel(filter)}`} onClick={() => editView({ filters: view.filters.filter((_, i) => i !== index) })}>
        {conditionLabel(filter)}<X size={14} />
      </button>)}
      {view.filters.length > 0 && <button type="button" className="text-action" onClick={() => editView({ filters: [] })}>清除筛选</button>}
      {result && <p className="result-count" aria-live="polite">共 {result.total} 个匹配项目{hiddenMatches > 0 ? ` · ${hiddenMatches} 个在隐藏列` : ''}</p>}
    </div>
    {loading && <Loading text="正在更新项目…" />}
    {!loading && <ErrorNote message={read?.error} retry={refresh} />}
    {!loading && stale && <div className="notice" role="status">项目已更新。<button type="button" className="text-action" onClick={refresh}>刷新查看</button></div>}
    {result && !result.hasProjects && <Empty title="还没有学习项目"><Link to="/">创建学习项目</Link></Empty>}
    {result?.hasProjects && result.total === 0 && filtered && <Empty title="没有符合条件的学习项目">
      <button type="button" className="text-action" onClick={() => editView({ search: '', filters: [] })}>清除搜索和筛选</button>
    </Empty>}
    {result?.hasProjects && (read?.layout === 'board'
      ? <div className="board" role="region" aria-label="项目状态看板" tabIndex={0}>{groups}</div> : groups)}
    {panel === 'filters' && <ProjectFilters view={view} tags={tagOptions.value} tagError={tagOptions.error} retryTags={tagOptions.refresh}
      onApply={editView} onClose={close} />}
    {panel === 'sort' && <SortDialog sort={view.sort} onClose={close} onApply={sort => editView({ sort })} />}
    {panel === 'groups' && <GroupDialog columns={view.columns} onClose={close} onApply={columns => editView({ columns })} />}
    {panel === 'save' && <SaveDialog selected={selected} view={view} onClose={close} onSaved={saved => {
      setBrowse(current => ({ ...current, view: saved.definition, savedId: saved.id }));
      saves.refresh();
      close();
    }} />}
    {panel === 'delete' && selected && <DeleteDialog view={selected} onClose={close} onDeleted={() => {
      setBrowse(current => ({ ...current, savedId: '' }));
      saves.refresh();
      close();
    }} />}
  </section>;
}
