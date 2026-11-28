import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { Crosshair, Download, EyeOff, FolderTree, History, Maximize, Minimize, ListChecks, Minus, Pause, Play, Plus, Redo2, RefreshCw, Search, Star, Undo2 } from 'lucide-react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { api, apiResponse, courseUrl, reviewUrl } from './api';
import { GraphCanvas, type GraphCanvasHandle } from './graph-canvas';
import { EdgeDialog, FavoritesDialog, GroupsDialog, HiddenDialog, HistoryDialog } from './graph-dialogs';
import { KIND_LABELS, matchesView, nodeMatches, normalizeSearch, studyLabels, type Camera, type GraphData, type GraphEdge, type GraphEdit, type GraphGroup, type GraphLinkCandidate, type GraphNode, type GraphRecommendationQueue, type GraphView, type Point } from './graph-model';
import { clampPoint, graphScene, type Glyph } from './graph-scene';
import { GraphTransfer } from './graph-transfer';
import { GRAPH_RECOMMENDATIONS, GraphReviewPanel, RecommendationDialog } from './graph-recommendations';
import { libraryFileUrl, LibraryViewSwitch } from './library-views';
import { BackedStatus, ErrorNote, useAction, useRead } from './shared';
import './graph.css';

const ENDPOINT = '/library/graph';
const NO_SUGGESTIONS: readonly GraphLinkCandidate[] = [];
type Dialog = 'hidden' | 'groups' | 'favorites' | 'history' | 'transfer' | 'recommend' | { node: GraphNode; edge?: GraphEdge };

function useSavedView(initial: GraphView) {
  const [view, display] = useState(initial);
  const [error, setError] = useState('');
  const desired = useRef(initial);
  const queued = useRef<GraphView | null>(null);
  const active = useRef(false);
  const mounted = useRef(false);
  const timer = useRef<number | null>(null);

  const flush = useCallback(async () => {
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = null;
    if (active.current || !queued.current) return;
    active.current = true;
    try {
      while (queued.current) {
        const current = queued.current;
        queued.current = null;
        try {
          const response = await apiResponse(ENDPOINT + '/view', 'PUT', JSON.stringify(current), 'application/json', undefined, true);
          await response.json();
          if (mounted.current) setError('');
        } catch (failure) {
          if (queued.current) continue;
          queued.current = current;
          if (mounted.current) setError(failure instanceof Error ? failure.message : '图谱视角未保存。');
          break;
        }
      }
    } finally {
      active.current = false;
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    const save = () => { void flush(); };
    window.addEventListener('pagehide', save);
    return () => {
      mounted.current = false;
      window.removeEventListener('pagehide', save);
      save();
    };
  }, [flush]);

  function update(changes: Partial<GraphView>, delay = 0) {
    const next = { ...desired.current, ...changes };
    desired.current = queued.current = next;
    display(next);
    if (timer.current !== null) window.clearTimeout(timer.current);
    if (delay) timer.current = window.setTimeout(() => void flush(), delay);
    else void flush();
  }

  return { view, update, error, flush };
}

function openUrl(node: GraphNode, params: URLSearchParams) {
  if (node.lessonId) return courseUrl(node.lessonId);
  return libraryFileUrl(params, { file: node.id, ...(node.kind === 'note' && node.fileStatus !== 'missing' && { edit: '1' }) });
}

function NodeSummary({ node, linked, choose, hide, edit, pending }: {
  node: GraphNode; linked: { node: GraphNode; edge: GraphEdge }[]; choose: (node: GraphNode) => void;
  hide: () => void; edit: (edge?: GraphEdge) => void; pending: boolean;
}) {
  const [params] = useSearchParams();
  const status = studyLabels(node);
  return <>
    <div className="graph-inspector-heading">
      <small>{KIND_LABELS[node.kind]}{node.projectTopic ? ' · ' + node.projectTopic : ''}</small>
      <h2>{node.title}</h2>
    </div>
    {(status.length > 0 || node.fileStatus === 'missing') && <ul className="graph-states" aria-label="节点状态">
      {status.map(text => <li key={text}>{text}</li>)}
      {node.fileStatus === 'missing' && <li>原件缺失</li>}
    </ul>}
    {node.excerpt && <p className="graph-excerpt">{node.excerpt}</p>}
    {node.path && <p className="graph-file-path">{node.path}</p>}
    <div className="action-row graph-node-open">
      <Link className="button primary" to={openUrl(node, params)}>{node.lessonId ? '打开课程' : node.kind === 'note' ? '打开笔记' : '打开文件'}</Link>
      {node.review && <Link className="button secondary" to={reviewUrl(node.review)}>去复习</Link>}
    </div>
    <div className="graph-neighbor-heading"><h3>关联节点</h3>
      <button className="text-action" disabled={pending} onClick={() => edit()}><Plus size={15} />添加连线</button>
    </div>
    {linked.length ? <ul className="graph-connections">
      {linked.map(({ node: neighbor, edge }) => <li key={edge.id}>
        <button type="button" className="graph-connection" onClick={() => choose(neighbor)}>
          <strong>{neighbor.title}</strong><small>{edge.both ? '双向' : edge.source === node.id ? '指向' : '来自'} · {KIND_LABELS[neighbor.kind]}</small>
        </button>
        <button type="button" className="text-action" aria-label={'编辑连线 ' + neighbor.title} disabled={pending} onClick={() => edit(edge)}>编辑</button>
      </li>)}
    </ul> : <p className="muted">还没有关联节点</p>}
    <button type="button" className="text-action graph-hide" disabled={pending} onClick={hide}><EyeOff size={15} />隐藏此节点</button>
  </>;
}

function useReducedMotion() {
  const [reduced, setReduced] = useState(() => window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  useEffect(() => {
    const query = window.matchMedia('(prefers-reduced-motion: reduce)');
    const adjust = () => setReduced(query.matches);
    query.addEventListener('change', adjust);
    adjust();
    return () => query.removeEventListener('change', adjust);
  }, []);
  return reduced;
}

function GraphWorkspace({ initial, openReview }: { initial: GraphData; openReview: boolean }) {
  const [params] = useSearchParams();
  const [data, setData] = useState(initial);
  const [query, setQuery] = useState('');
  const [dialog, setDialog] = useState<Dialog>();
  const [selectedGroup, setSelectedGroup] = useState<string>();
  const [pending, setPending] = useState(false);
  const [editError, setEditError] = useState('');
  const applying = useRef(false);
  const saved = useSavedView(initial.view);
  const { view, update } = saved;
  const refresh = useAction();
  const navigate = useNavigate();
  const reduced = useReducedMotion();
  const motion = view.motion ?? !reduced;
  const stage = useRef<GraphCanvasHandle>(null);
  const workbench = useRef<HTMLDivElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [full, setFull] = useState(false);
  const [reviewOpen, setReviewOpen] = useState(openReview);
  const recommendations = useRead<GraphRecommendationQueue>(GRAPH_RECOMMENDATIONS);
  const hasJobs = !!recommendations.value?.jobs.some(job => job.state === 'queued' || job.state === 'running');
  const budgetWaiting = !!recommendations.value?.jobs.some(job => job.state === 'blocked');
  const candidates = recommendations.value?.candidates;
  const candidateCount = candidates?.length ?? 0;
  const proposed = useMemo(() => {
    if (!candidates?.length) return NO_SUGGESTIONS;
    const links = candidates.filter(item => item.kind === 'link');
    return links.length ? links : NO_SUGGESTIONS;
  }, [candidates]);

  useEffect(() => {
    if (recommendations.error || (!hasJobs && !budgetWaiting)) return;
    const handle = window.setTimeout(recommendations.refresh, hasJobs ? 650 : 5500);
    return () => window.clearTimeout(handle);
  }, [recommendations.value, recommendations.error, recommendations.refresh, hasJobs, budgetWaiting]);

  useEffect(() => {
    window.addEventListener('focus', recommendations.refresh);
    return () => window.removeEventListener('focus', recommendations.refresh);
  }, [recommendations.refresh]);

  const activeNodes = useMemo(() => new Map(data.nodes.filter(node => !node.hidden).map(node => [node.id, node])), [data.nodes]);
  const scene = useMemo(() => graphScene(data, view, proposed), [data, view.folder, view.project, view.due, view.local, view.focus, view.collapsed, proposed]);
  const drawnCandidates = useMemo(() => scene.edges.filter(edge => edge.candidate).length, [scene]);
  const focus = view.focus ? activeNodes.get(view.focus) : undefined;
  const group = data.groups.find(item => item.id === selectedGroup);
  const selection = group ? undefined : focus;
  const key = normalizeSearch(query);
  const found = useMemo(() => key ? [...activeNodes.values()].filter(node => nodeMatches(node, key)).slice(0, 12) : [], [activeNodes, key]);
  const linked = useMemo(() => {
    if (!selection) return [];
    return data.edges.flatMap(edge => {
      const peer = edge.source === selection.id ? edge.target : edge.target === selection.id ? edge.source : null;
      const node = peer ? activeNodes.get(peer) : undefined;
      return node ? [{ node, edge }] : [];
    }).sort((left, right) => left.node.title.localeCompare(right.node.title, 'zh-CN'));
  }, [selection, activeNodes, data.edges]);

  useEffect(() => {
    function changed() { setFull(document.fullscreenElement === workbench.current); }
    document.addEventListener('fullscreenchange', changed);
    function escape(event: KeyboardEvent) {
      if (event.key === 'Escape' && !dialog) setExpanded(false);
    }
    window.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('fullscreenchange', changed);
      window.removeEventListener('keydown', escape);
    };
  }, [dialog]);

  async function fullscreen() {
    if (full) {
      await document.exitFullscreen().catch(() => undefined);
    } else if (expanded) {
      setExpanded(false);
    } else {
      try {
        if (!workbench.current?.requestFullscreen) setExpanded(true);
        else await workbench.current.requestFullscreen();
      } catch {
        setExpanded(true);
      }
    }
  }

  function openDialog(target: Dialog) {
    if (target === 'transfer') void saved.flush();
    setEditError('');
    setDialog(target);
  }

  function closeDialog() {
    setDialog(undefined);
    setEditError('');
  }

  async function commit(action: GraphEdit) {
    if (applying.current) return false;
    applying.current = true;
    setPending(true);
    setEditError('');
    try {
      setData(await api<GraphData>(ENDPOINT + '/edit', 'POST', { revision: data.revision, action }));
      return true;
    } catch (failure) {
      setEditError(failure instanceof Error ? failure.message : '图谱变更未保存。');
      return false;
    } finally {
      applying.current = false;
      setPending(false);
    }
  }

  function selectNode(node: GraphNode) {
    const parent = data.groups.find(item => item.members.includes(node.id));
    setSelectedGroup(undefined);
    setQuery('');
    update({
      ...(!matchesView(node, view) ? { folder: '', project: null, due: false } : {}),
      focus: node.id, center: node.position, zoom: Math.min(1.8, Math.max(.92, view.zoom)),
      collapsed: parent ? view.collapsed.filter(id => id !== parent.id) : view.collapsed,
    });
  }

  function selectGlyph(glyph: Glyph) {
    if (glyph.node) selectNode(glyph.node);
    else if (glyph.groupId) {
      setSelectedGroup(glyph.groupId);
      update({ center: glyph.point, zoom: Math.min(1.8, Math.max(.92, view.zoom)) });
    }
  }

  function collapse(item: GraphGroup) {
    const wasCollapsed = view.collapsed.includes(item.id);
    setSelectedGroup(wasCollapsed ? undefined : item.id);
    update({ collapsed: wasCollapsed ? view.collapsed.filter(id => id !== item.id) : [...view.collapsed, item.id] });
  }

  function openGlyph(glyph: Glyph) {
    if (glyph.node) navigate(openUrl(glyph.node, params));
    if (glyph.groupId) {
      const current = data.groups.find(item => item.id === glyph.groupId);
      if (current && view.collapsed.includes(current.id)) collapse(current);
    }
  }

  async function moveGlyph(glyph: Glyph, delta: Point) {
    if (glyph.groupId) await commit({ kind: 'moveGroup', id: glyph.groupId, delta });
    else if (glyph.node) {
      const position = clampPoint({ x: glyph.point.x + delta.x, y: glyph.point.y + delta.y });
      await commit({ kind: 'move', id: glyph.node.id, position });
    }
  }

  function chooseOnly(event: FormEvent) {
    event.preventDefault();
    if (found.length) selectNode(found[0]);
  }

  function filters(changes: Partial<GraphView>) {
    setSelectedGroup(undefined);
    update({ ...changes, center: null });
  }

  function changeCamera(camera: Camera) { update(camera, 220); }

  const dialogActions = { commit, pending, error: editError, close: closeDialog };
  return <div ref={workbench} className={'graph-workspace panel' + (expanded ? ' graph-expanded' : '')}>
    <div className="graph-tools">
      <form className="graph-search-form" onSubmit={chooseOnly}>
        <label className="search"><Search size={17} /><input type="search" aria-label="搜索图谱节点" maxLength={180}
          placeholder="搜索节点名称" value={query} onChange={event => setQuery(event.target.value)} /></label>
        {key && <div className="graph-search-results" aria-label="图谱搜索结果">
          {found.length ? <ul className="graph-choices">{found.map(node => <li key={node.id}>
            <button type="button" onClick={() => selectNode(node)}><strong>{node.title}</strong>
              <small>{[KIND_LABELS[node.kind], node.projectTopic ?? node.folder].filter(Boolean).join(' · ')}</small></button>
          </li>)}</ul> : <p>没有匹配的节点</p>}
        </div>}
      </form>
      <select aria-label="图谱文件夹" value={view.folder} onChange={event => filters({ folder: event.target.value })}>
        <option value="">全部文件夹</option>
        {view.folder && !data.folders.some(item => item.path === view.folder) && <option value={view.folder}>{view.folder}</option>}
        {data.folders.map(item => <option key={item.path} value={item.path}>{item.name}</option>)}
      </select>
      <select aria-label="图谱学习项目" value={view.project ?? ''} onChange={event => filters({ project: event.target.value || null })}>
        <option value="">全部项目</option>
        {view.project && !data.projects.some(item => item.id === view.project) && <option value={view.project}>已移除的项目</option>}
        {data.projects.map(item => <option key={item.id} value={item.id}>{item.topic}</option>)}
      </select>
      <button type="button" className="button secondary graph-toggle" aria-pressed={view.due}
        onClick={() => filters({ due: !view.due })}>待复习</button>
      <div className="graph-scope" role="group" aria-label="图谱范围">
        <button type="button" aria-pressed={!view.local} onClick={() => filters({ local: false })}>全局</button>
        <button type="button" aria-pressed={view.local} disabled={!focus} onClick={() => {
          if (focus) update({ local: true, center: focus.position });
        }}>当前关联</button>
      </div>
    </div>
    <div className="graph-actions">
      <div className="graph-toolbar-group" role="group" aria-label="图谱视角控制">
        <button className="icon-button" aria-label="缩小图谱" title="缩小" onClick={() => stage.current?.zoom(.8)}><Minus size={17} /></button>
        <button className="icon-button" aria-label="放大图谱" title="放大" onClick={() => stage.current?.zoom(1.25)}><Plus size={17} /></button>
        <button className="icon-button" aria-label="适配图谱" title="显示当前范围" onClick={() => stage.current?.fit()}><Crosshair size={17} /></button>
        <button className="icon-button" aria-label={motion ? '暂停动效' : '继续动效'} title={motion ? '暂停动效' : '继续动效'}
          onClick={() => update({ motion: !motion })}>{motion ? <Pause size={16} /> : <Play size={16} />}</button>
        <button className="icon-button" aria-label={full || expanded ? '退出全屏' : '全屏图谱'} title={full || expanded ? '退出全屏' : '全屏'}
          onClick={() => void fullscreen()}>{full || expanded ? <Minimize size={16} /> : <Maximize size={16} />}</button>
      </div>
      <div className="graph-toolbar-group" role="group" aria-label="图谱编辑">
        <button className="icon-button" aria-label="撤销图谱操作" title="撤销" disabled={pending || !data.canUndo}
          onClick={() => void commit({ kind: 'undo' })}><Undo2 size={17} /></button>
        <button className="icon-button" aria-label="重做图谱操作" title="重做" disabled={pending || !data.canRedo}
          onClick={() => void commit({ kind: 'redo' })}><Redo2 size={17} /></button>
        <button className="icon-button" aria-label="刷新图谱" title="刷新知识库" disabled={pending || refresh.pending}
          onClick={() => {
            recommendations.refresh();
            void refresh.run(async () => setData(await api<GraphData>(ENDPOINT)));
          }}><RefreshCw size={16} /></button>
        <button className="graph-tool" onClick={() => openDialog('groups')}><FolderTree size={16} />分组</button>
        <button className="graph-tool" onClick={() => openDialog('hidden')}><EyeOff size={16} />隐藏节点</button>
        <button className="graph-tool" onClick={() => openDialog('favorites')}><Star size={16} />收藏视角</button>
        <button className="graph-tool" onClick={() => openDialog('history')}><History size={16} />图谱历史</button>
        <button className="graph-tool" disabled={pending} onClick={() => openDialog('transfer')}><Download size={16} />导入导出</button>
        <button type="button" className="graph-tool graph-review-toggle" aria-expanded={reviewOpen} aria-controls="graph-side-panel"
          onClick={() => { setReviewOpen(open => !open); recommendations.refresh(); }}>
          <ListChecks size={16} />图谱待审核{candidateCount > 0 && <span>{candidateCount}</span>}
        </button>
      </div>
    </div>
    <ErrorNote message={refresh.error} />
    <ErrorNote message={saved.error} retry={() => void saved.flush()} />
    {!dialog && <ErrorNote message={editError} />}
    <div className="graph-layout">
      <div className="graph-stage">
        <GraphCanvas ref={stage} scene={scene} center={view.center} zoom={view.zoom}
          focus={group ? 'group:' + group.id : view.focus} motion={motion} reduced={reduced}
          onCamera={changeCamera} onSelect={selectGlyph} onOpen={openGlyph} onMove={moveGlyph} />
        {!scene.nodes.length && <div className="graph-empty">
          <strong>{data.nodes.length ? '这个范围没有节点' : '知识库还没有节点'}</strong>
          {data.nodes.length ? <button type="button" className="text-action" onClick={() => filters({ folder: '', project: null, due: false, local: false })}>清除筛选</button>
            : <Link className="text-action" to="/library">打开文件视图</Link>}
        </div>}
      </div>
      <aside id="graph-side-panel" className="graph-inspector" aria-label={reviewOpen ? '图谱待审核' : '图谱节点详情'}>
        {reviewOpen ? <GraphReviewPanel queue={recommendations.value} loadError={recommendations.error} refresh={recommendations.refresh}
          graph={data} onChanged={setData} close={() => setReviewOpen(false)} openRequest={() => openDialog('recommend')} />
          : group ? <>
          <div className="graph-inspector-heading"><small>节点分组</small><h2>{group.name}</h2></div>
          <button className="button secondary" onClick={() => collapse(group)}>{view.collapsed.includes(group.id) ? '展开分组' : '收起分组'}</button>
          <ul className="graph-choices">{group.members.slice(0, 20).map(id => {
            const member = activeNodes.get(id);
            return member ? <li key={id}><button onClick={() => selectNode(member)}>{member.title}</button></li> : null;
          })}</ul>
        </> : selection ? <NodeSummary node={selection} linked={linked} choose={selectNode} pending={pending}
          hide={() => void (async () => {
            if (await commit({ kind: 'visibility', ids: [selection.id], hidden: true })) update({ local: false, focus: null, center: null });
          })()} edit={edge => openDialog({ node: selection, edge })} />
          : <>
            <div className="graph-inspector-heading"><h2>节点</h2></div>
            {scene.nodes.length > 0 && <ul className="graph-choices">{scene.nodes.slice(0, 8).map(node => <li key={node.id}>
              <button type="button" onClick={() => selectNode(node)}><strong>{node.title}</strong><small>{KIND_LABELS[node.kind]}</small></button>
            </li>)}</ul>}
          </>}
      </aside>
    </div>
    <footer className="graph-footer">
      <span role="status">{scene.nodes.length} 个节点 · {scene.edges.length - drawnCandidates} 条连线
        {drawnCandidates ? ' · ' + drawnCandidates + ' 条候选连线' : ''}</span>
      <ul aria-label="图例"><li><i className="graph-key course" />知识点</li><li><i className="graph-key note" />笔记</li>
        <li><i className="graph-key import" />资料</li><li><i className="graph-key learned" />已学完</li>
        <li><i className="graph-key supplement" />补充讲解</li><li><i className="graph-key due" />待复习</li><li><i className="graph-key weak" />薄弱</li>
        {drawnCandidates > 0 && <li><i className="graph-key candidate" />候选连线</li>}</ul>
    </footer>
    {dialog === 'hidden' && <HiddenDialog {...dialogActions} nodes={data.nodes} />}
    {dialog === 'groups' && <GroupsDialog {...dialogActions} data={data} view={view} focus={selection?.id ?? null} collapse={collapse} />}
    {dialog === 'favorites' && <FavoritesDialog {...dialogActions} favorites={data.favorites} view={view}
      apply={favorite => { setSelectedGroup(undefined); update(favorite); }} />}
    {dialog === 'history' && <HistoryDialog {...dialogActions} head={data.head} />}
    {dialog === 'recommend' && <RecommendationDialog nodes={data.nodes} recommended={scene.nodes} focus={selection}
      close={closeDialog} queued={() => { recommendations.refresh(); closeDialog(); }} />}
    {dialog === 'transfer' && <GraphTransfer revision={data.revision} close={closeDialog} capture={async format => {
      if (!stage.current) throw new Error('图谱画布仍在准备，请稍后重试。');
      return stage.current.capture(format);
    }} imported={value => { setData(value); setSelectedGroup(undefined); update(value.view); }} />}
    {dialog && typeof dialog === 'object' && <EdgeDialog {...dialogActions} nodes={data.nodes} origin={dialog.node} edge={dialog.edge} />}
  </div>;
}

export function LibraryGraphPage() {
  const [params] = useSearchParams();
  const graph = useRead<GraphData>(ENDPOINT);
  return <section className="page library-page graph-page">
    <div className="page-head"><div><h1>知识库</h1><p>查看知识点、笔记和资料之间的关联。</p></div></div>
    <LibraryViewSwitch graph />
    {graph.value ? <GraphWorkspace initial={graph.value} openReview={params.get('review') === '1'} /> : <BackedStatus error={graph.error} retry={graph.refresh} />}
  </section>;
}
