import { useState, type FormEvent } from 'react';
import { ChevronDown, ChevronRight, Plus, X } from 'lucide-react';
import { BackedStatus, Empty, ErrorNote, Modal, useRead } from './shared';
import { KIND_LABELS, nodeMatches, normalizeSearch, type GraphData, type GraphEdge, type GraphEdit, type GraphFavorite, type GraphGroup, type GraphNode, type GraphView, type LinkDirection } from './graph-model';

type Commit = (action: GraphEdit) => Promise<boolean>;
type DialogProps = { commit: Commit; close: () => void; pending: boolean; error: string };

export function NodePicker({ nodes, label, exclude, pick, disabled = false }: {
  nodes: GraphNode[]; label: string; exclude: Set<string>; pick: (node: GraphNode) => void; disabled?: boolean;
}) {
  const [query, setQuery] = useState('');
  const key = normalizeSearch(query);
  const items = nodes.filter(node => !node.hidden && !exclude.has(node.id) && (!key || nodeMatches(node, key))).slice(0, 8);
  return <div className="graph-node-picker">
    <label>{label}<input type="search" disabled={disabled} value={query} maxLength={180} onChange={event => setQuery(event.target.value)} placeholder="输入节点名称" /></label>
    {items.length ? <ul className="graph-choices">{items.map(node => <li key={node.id}>
      <button type="button" disabled={disabled} onClick={() => { pick(node); setQuery(''); }}>
        <strong>{node.title}</strong><small>{[KIND_LABELS[node.kind], node.projectTopic ?? node.folder].filter(Boolean).join(' · ')}</small>
      </button>
    </li>)}</ul> : <p className="muted">没有匹配的节点</p>}
  </div>;
}

export function EdgeDialog({ origin, edge, nodes, close, commit, pending, error }: DialogProps & {
  origin: GraphNode; edge?: GraphEdge; nodes: GraphNode[];
}) {
  const [target, setTarget] = useState(edge ? edge.source === origin.id ? edge.target : edge.source : '');
  const [direction, setDirection] = useState<'out' | 'in' | 'both'>(edge?.both ? 'both' : edge && edge.source !== origin.id ? 'in' : 'out');
  const chosen = nodes.find(node => node.id === target);

  async function save(choice: LinkDirection) {
    if (!target) return;
    const source = direction === 'in' ? target : origin.id;
    const end = direction === 'in' ? origin.id : target;
    if (await commit({ kind: 'link', source, target: end, direction: choice })) close();
  }

  return <Modal title={edge ? '编辑连线' : '添加连线'} onClose={close} busy={pending}>
    <form className="graph-dialog-form" onSubmit={event => { event.preventDefault(); void save(direction === 'both' ? 'both' : 'one-way'); }}>
      <p>当前节点 <strong>{origin.title}</strong></p>
      {chosen && <p className="graph-selected-node">关联节点 <strong>{chosen.title}</strong></p>}
      {!edge && <NodePicker nodes={nodes} exclude={new Set([origin.id])} label="选择关联节点" pick={node => setTarget(node.id)} />}
      <label>连线方向<select value={direction} onChange={event => setDirection(event.target.value as typeof direction)}>
        <option value="out">当前节点 → 关联节点</option>
        <option value="in">关联节点 → 当前节点</option>
        <option value="both">双向</option>
      </select></label>
      <ErrorNote message={error} />
      <div className="action-row">
        <button className="button primary" disabled={!chosen || pending}>保存连线</button>
        {edge && <button type="button" className="button secondary" disabled={pending} onClick={() => void save('remove')}>删除连线</button>}
        {edge?.automatic && edge.manual && <button type="button" className="text-action" disabled={pending} onClick={() => void save('default')}>恢复原有关系</button>}
      </div>
    </form>
  </Modal>;
}

export function HiddenDialog({ nodes, close, commit, pending, error }: DialogProps & { nodes: GraphNode[] }) {
  const hidden = nodes.filter(node => node.hidden);
  const [picked, setPicked] = useState<string[]>([]);
  const ids = hidden.filter(node => picked.includes(node.id)).map(node => node.id);

  return <Modal title="隐藏节点" onClose={close} busy={pending}>
    {hidden.length ? <>
      <p className="muted">恢复后，所选节点会回到图谱中的原位置。</p>
      <div className="graph-dialog-row">
        <button className="text-action" type="button" onClick={() => setPicked(ids.length === hidden.length ? [] : hidden.map(node => node.id))}>
          {ids.length === hidden.length ? '取消全选' : '全选'}
        </button>
        <button type="button" className="button primary" disabled={pending || !ids.length} onClick={async () => {
          if (await commit({ kind: 'visibility', ids, hidden: false })) setPicked([]);
        }}>恢复所选节点{ids.length ? ' · ' + ids.length : ''}</button>
      </div>
      <ul className="graph-dialog-list">
        {hidden.map(node => <li key={node.id}><label className="graph-check">
          <input type="checkbox" checked={ids.includes(node.id)} onChange={event => setPicked(current => event.target.checked
            ? [...current, node.id] : current.filter(id => id !== node.id))} />
          <span>{node.title}<small>{KIND_LABELS[node.kind]}</small></span>
        </label></li>)}
      </ul>
    </> : <Empty title="没有隐藏节点" />}
    <ErrorNote message={error} />
  </Modal>;
}

function GroupFields({ group, nodes, groups, cancel, save, pending }: {
  group: GraphGroup; nodes: GraphNode[]; groups: GraphGroup[]; cancel: () => void;
  save: (group: GraphGroup) => Promise<void>; pending: boolean;
}) {
  const [name, setName] = useState(group.name);
  const [members, setMembers] = useState(group.members);
  const nodeIndex = new Map(nodes.map(node => [node.id, node]));
  const exclude = new Set([...members, ...groups.filter(item => item.id !== group.id).flatMap(other => other.members)]);

  return <form className="graph-dialog-form" onSubmit={event => {
    event.preventDefault();
    if (name.trim() && members.length >= 2) void save({ ...group, name: name.trim(), members });
  }}>
    <label>分组名称<input value={name} maxLength={80} required onChange={event => setName(event.target.value)} /></label>
    <div className="graph-members">
      {members.map(id => <button key={id} type="button" className="chip" aria-label={'移出分组 ' + (nodeIndex.get(id)?.title ?? '已移除节点')}
        onClick={() => setMembers(current => current.filter(item => item !== id))}>{nodeIndex.get(id)?.title ?? '已移除节点'}<X size={14} /></button>)}
    </div>
    <NodePicker nodes={nodes} label="向分组添加节点" pick={node => setMembers(current => [...current, node.id])} exclude={exclude} />
    <div className="action-row">
      <button className="button primary" disabled={pending || !name.trim() || members.length < 2}>保存分组</button>
      <button type="button" className="button secondary" onClick={cancel} disabled={pending}>取消</button>
    </div>
  </form>;
}

export function GroupsDialog({ data, view, focus, close, commit, pending, error, collapse }: DialogProps & {
  data: GraphData; view: GraphView; focus: string | null; collapse: (group: GraphGroup) => void;
}) {
  const [editing, setEditing] = useState<GraphGroup>();
  return <Modal title="图谱分组" onClose={close} busy={pending}>
    {editing ? <GroupFields key={editing.id} group={editing} nodes={data.nodes} groups={data.groups}
      cancel={() => setEditing(undefined)} pending={pending} save={async group => {
        if (await commit({ kind: 'group', group })) setEditing(undefined);
      }} /> : <>
      <button type="button" className="button secondary" onClick={() => {
        const singleton = focus && !data.groups.some(group => group.members.includes(focus)) ? [focus] : [];
        setEditing({ id: crypto.randomUUID(), name: '', members: singleton });
      }}><Plus size={16} />新建分组</button>
      {data.groups.length ? <ul className="graph-dialog-list">{data.groups.map(group => {
        const collapsed = view.collapsed.includes(group.id);
        const Icon = collapsed ? ChevronRight : ChevronDown;
        return <li key={group.id} className="graph-dialog-row">
          <strong>{group.name} · {group.members.length}</strong>
          <div className="action-row">
            <button type="button" className="text-action" onClick={() => collapse(group)} aria-label={(collapsed ? '展开 ' : '收起 ') + group.name}>
              <Icon size={14} />{collapsed ? '展开' : '收起'}
            </button>
            <button type="button" className="text-action" onClick={() => setEditing({ ...group,
              members: group.members.filter(id => data.nodes.some(node => node.id === id)),
            })}>编辑</button>
            <button type="button" className="text-action" disabled={pending} onClick={() => void commit({ kind: 'dropGroup', id: group.id })}>取消分组</button>
          </div>
        </li>;
      })}</ul> : <Empty title="还没有分组" />}
    </>}
    <ErrorNote message={error} />
  </Modal>;
}

export function FavoritesDialog({ favorites, view, apply, close, commit, pending, error }: DialogProps & {
  favorites: GraphFavorite[]; view: GraphView; apply: (view: GraphView) => void;
}) {
  const [name, setName] = useState('');
  async function save(event: FormEvent) {
    event.preventDefault();
    if (name.trim() && await commit({ kind: 'favorite', favorite: { id: crypto.randomUUID(), name: name.trim(), view } })) setName('');
  }

  return <Modal title="收藏视角" onClose={close} busy={pending}>
    <form className="graph-dialog-row" onSubmit={event => void save(event)}>
      <label>视角名称<input value={name} required maxLength={80} onChange={event => setName(event.target.value)} /></label>
      <button className="button primary" disabled={pending || !name.trim()}>保存当前视角</button>
    </form>
    {favorites.length ? <ul className="graph-dialog-list">{favorites.map(item => <li key={item.id} className="graph-dialog-row">
      <button type="button" className="text-action" onClick={() => { apply(item.view); close(); }}>{item.name}</button>
      <button type="button" className="text-action" aria-label={'删除收藏 ' + item.name} disabled={pending}
        onClick={() => void commit({ kind: 'dropFavorite', id: item.id })}>删除</button>
    </li>)}</ul> : <Empty title="还没有收藏的视角" />}
    <ErrorNote message={error} />
  </Modal>;
}

export function HistoryDialog({ head, close, commit, pending, error }: DialogProps & { head: number | null }) {
  const [page, setPage] = useState<number[]>([]);
  const [chosen, setChosen] = useState<number>();
  const history = useRead<{ items: { id: number; name: string; at: string }[]; next: number | null }>(
    '/library/graph/history' + (page.length ? '?before=' + page[page.length - 1] : ''),
  );
  return <Modal title="图谱历史" onClose={close} busy={pending}>
    {history.value ? <>
      {history.value.items.length ? <ul className="graph-dialog-list">{history.value.items.map(item => <li key={item.id} className="graph-dialog-row">
        <span><strong>{item.name}</strong><small>{new Date(item.at.endsWith('Z') ? item.at : item.at + 'Z').toLocaleString('zh-CN')}</small></span>
        {item.id === head ? <span className="chip">当前版本</span> : <button className="text-action" type="button"
          aria-pressed={chosen === item.id} onClick={() => setChosen(item.id)}>选择此版本</button>}
      </li>)}</ul> : <Empty title="还没有图谱编辑记录" />}
      {chosen !== undefined && <div className="graph-dialog-row">
        <p className="muted">恢复将应用该版本的位置、连线、分组和隐藏设置。</p>
        <button type="button" className="button primary" disabled={pending} onClick={async () => {
          if (await commit({ kind: 'restore', id: chosen })) close();
        }}>确认恢复</button>
      </div>}
      <div className="action-row">
        {!!page.length && <button type="button" className="text-action" onClick={() => { setPage(page.slice(0, -1)); setChosen(undefined); }}>上一页</button>}
        {history.value.next !== null && <button type="button" className="text-action"
          onClick={() => { setPage([...page, history.value!.next!]); setChosen(undefined); }}>下一页</button>}
      </div>
    </> : <BackedStatus error={history.error} retry={history.refresh} />}
    <ErrorNote message={error} />
  </Modal>;
}
