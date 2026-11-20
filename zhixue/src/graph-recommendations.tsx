import { useState } from 'react';
import { Check, Sparkles, X } from 'lucide-react';
import { Link } from 'react-router-dom';
import { api } from './api';
import { NodePicker } from './graph-dialogs';
import { KIND_LABELS, type GraphCandidate, type GraphData, type GraphKind, type GraphNode, type GraphRecommendationQueue } from './graph-model';
import { BackedStatus, Empty, ErrorNote, Modal, useAction } from './shared';

export const GRAPH_RECOMMENDATIONS = '/library/graph/recommendations';
const MAX_SELECTED = 24;
const ITEMS_PER_PAGE = 8;

type Request = { id: string; nodeIds: string[]; withExcerpts: boolean };
type Scope = {
  recipient: { name: string; address: string; model: string };
  consent: { connectionId: string; revision: number; maxCalls: number; token: string };
  nodes: { key: string; title: string; kind: GraphKind; grouped: boolean; excerpt?: string }[];
  existingLinks: { source: string; target: string; both: boolean }[];
};

export function RecommendationDialog({ nodes, recommended, focus, close, queued }: {
  nodes: GraphNode[]; recommended: GraphNode[]; focus?: GraphNode; close: () => void; queued: () => void;
}) {
  const action = useAction();
  const [selected, setSelected] = useState<GraphNode[]>(focus && !focus.hidden ? [focus] : []);
  const [withExcerpts, setWithExcerpts] = useState(false);
  const [preview, setPreview] = useState<{ input: Request; scope: Scope }>();
  const selectable = nodes.filter(node => !node.hidden && (node.lessonId || (node.path && node.fileStatus !== 'missing')));
  const available = new Set(selectable.map(node => node.id));
  const inView = recommended.length <= MAX_SELECTED ? recommended.filter(node => available.has(node.id)) : [];
  const keys = new Set(selected.map(node => node.id));

  function inspect() {
    const input = { id: crypto.randomUUID(), nodeIds: selected.map(node => node.id), withExcerpts };
    void action.run(async () => {
      const scope = await api<Scope>(GRAPH_RECOMMENDATIONS + '/preview', 'POST', input);
      setPreview({ input, scope });
    });
  }

  function send() {
    if (!preview) return;
    void action.run(async () => {
      await api(GRAPH_RECOMMENDATIONS, 'POST', { ...preview.input, consent: preview.scope.consent });
      queued();
    });
  }

  return <Modal title="发起图谱推荐" onClose={close} busy={action.pending}>
    <div className="graph-dialog-form">
      {preview ? <>
        <div className="graph-recommendation-recipient">
          <strong>{preview.scope.recipient.name} · {preview.scope.recipient.model}</strong>
          <small>{preview.scope.recipient.address}</small>
          <small>本次最多 {preview.scope.consent.maxCalls} 次模型调用</small>
        </div>
        <h3>将发送的节点</h3>
        <ul className="graph-scope-material">
          {preview.scope.nodes.map(node => <li key={node.key}>
            <strong>{node.title}</strong><small>{KIND_LABELS[node.kind]}{node.grouped ? ' · 已有分组' : ''}</small>
            {node.excerpt && <p>{node.excerpt}</p>}
          </li>)}
        </ul>
        {preview.scope.existingLinks.length > 0 && <details className="graph-existing-links">
          <summary>将发送的现有连线 · {preview.scope.existingLinks.length}</summary>
          <ul>{preview.scope.existingLinks.map(link => <li key={link.source + '/' + link.target}>
            {preview.scope.nodes.find(node => node.key === link.source)?.title} {link.both ? '↔' : '→'} {preview.scope.nodes.find(node => node.key === link.target)?.title}
          </li>)}</ul>
        </details>}
        <div className="action-row">
          <button type="button" className="button primary" disabled={action.pending} onClick={send}>确认发送并排队</button>
          <button type="button" className="button secondary" disabled={action.pending} onClick={() => setPreview(undefined)}>返回选取节点</button>
        </div>
      </> : <>
        <p className="muted">选择 2 至 24 个节点，预览发送范围后再请求模型推荐关系和分组。</p>
        <div className="graph-members">
          {selected.map(node => <button type="button" className="chip" key={node.id} disabled={action.pending}
            aria-label={'从推荐范围移除 ' + node.title} onClick={() => setSelected(current => current.filter(item => item.id !== node.id))}>
            {node.title}<X size={14} />
          </button>)}
        </div>
        {inView.length > 1 && inView.length <= MAX_SELECTED && <button type="button" className="text-action"
          disabled={action.pending} onClick={() => setSelected(inView)}>选择当前视图的 {inView.length} 个节点</button>}
        <NodePicker nodes={selectable} label="搜索并选择推荐节点" exclude={keys} disabled={action.pending || selected.length >= MAX_SELECTED}
          pick={node => setSelected(current => [...current, node])} />
        <label className="graph-check graph-excerpt-check"><input type="checkbox" checked={withExcerpts} disabled={action.pending}
          onChange={event => setWithExcerpts(event.target.checked)} />发送选中节点的可用简介</label>
        <div className="action-row">
          <button className="button primary" disabled={action.pending || selected.length < 2} onClick={inspect}>预览发送范围</button>
        </div>
      </>}
      <ErrorNote message={action.error} />
      {action.error && !preview && <Link className="text-action" to="/settings/models">查看 API 与模型设置</Link>}
    </div>
  </Modal>;
}

const JOB_STATES = {
  queued: '正在排队', running: '正在生成候选', blocked: '等待调用额度',
  ready: '推荐已生成', failed: '推荐未完成', cancelled: '推荐已停止',
};

function candidateTitle(candidate: GraphCandidate) {
  if (candidate.kind === 'group') return candidate.name;
  return candidate.titles[candidate.source] + (candidate.both ? ' ↔ ' : ' → ') + candidate.titles[candidate.target];
}

type Decisions = {
  accepted: number; rejected: number; skipped: { id: string; reason: string }[]; graph: GraphData | null;
};

export function GraphReviewPanel({ queue, loadError, refresh, graph, onChanged, close, openRequest }: {
  queue?: GraphRecommendationQueue; loadError?: string; refresh: () => void; graph: GraphData;
  onChanged: (value: GraphData) => void; close: () => void; openRequest: () => void;
}) {
  const action = useAction();
  const [picked, setPicked] = useState<string[]>([]);
  const [result, setResult] = useState<{ summary: string; skipped: { title: string; reason: string }[] }>();
  const [page, setPage] = useState(0);
  const candidates = queue?.candidates ?? [];
  const last = Math.max(0, Math.ceil(candidates.length / ITEMS_PER_PAGE) - 1);
  const present = Math.min(page, last);
  const current = candidates.slice(present * ITEMS_PER_PAGE, (present + 1) * ITEMS_PER_PAGE);
  const selected = current.filter(item => picked.includes(item.id));

  function review(choice: 'accept' | 'reject') {
    if (!selected.length) return;
    void action.run(async () => {
      const response = await api<Decisions>(GRAPH_RECOMMENDATIONS + '/review', 'POST', {
        ids: selected.map(item => item.id), action: choice, revision: graph.revision,
      });
      if (response.graph) onChanged(response.graph);
      setPicked([]);
      refresh();
      const verb = choice === 'accept' ? '已采用' : '已拒绝';
      const count = choice === 'accept' ? response.accepted : response.rejected;
      const names = new Map(selected.map(item => [item.id, candidateTitle(item)]));
      setResult({
        summary: verb + count + ' 项。' + (response.skipped.length ? '跳过 ' + response.skipped.length + ' 项。' : ''),
        skipped: response.skipped.map(item => ({ title: names.get(item.id) ?? '候选', reason: item.reason })),
      });
    });
  }

  return <div className="graph-review-panel">
    <div className="graph-review-title"><h2 id="graph-review-title">图谱待审核</h2>
      <button type="button" className="icon-button" aria-label="返回节点详情" onClick={close}><X size={16} /></button>
    </div>
    <button type="button" className="button secondary" disabled={action.pending} onClick={openRequest}><Sparkles size={16} />发起推荐</button>
    <ErrorNote message={action.error} />
    {result && <div className="graph-review-result">
      <p role="status">{result.summary}</p>
      {result.skipped.length > 0 && <ul>{result.skipped.map(({ title, reason }, index) =>
        <li key={index}><strong>{title}</strong><span>{reason}</span></li>)}</ul>}
    </div>}
    {!queue ? <BackedStatus error={loadError} retry={refresh} /> : <>
      <ErrorNote message={loadError} retry={refresh} />
      {current.length ? <>
        <div className="graph-review-choices">
          <button type="button" className="text-action" disabled={action.pending} onClick={() => setPicked(
            selected.length === current.length ? [] : current.map(item => item.id),
          )}>{selected.length === current.length ? '取消全选' : '全选本页'}</button>
          <button type="button" className="text-action" disabled={action.pending || !selected.length} onClick={() => review('accept')}><Check size={15} />确认所选</button>
          <button type="button" className="text-action" disabled={action.pending || !selected.length} onClick={() => review('reject')}>拒绝所选</button>
        </div>
        <ul className="graph-review-items">{current.map(item => <li key={item.id}>
          <label className="graph-check">
            <input type="checkbox" checked={picked.includes(item.id)} disabled={action.pending}
              onChange={event => setPicked(list => event.target.checked ? [...list, item.id] : list.filter(id => id !== item.id))}
              aria-label={'选择' + (item.kind === 'link' ? '连线 ' : '分组 ') + candidateTitle(item)} />
            <span><small>{item.kind === 'link' ? '候选连线' : '推荐分组'}</small>
              <strong>{candidateTitle(item)}</strong>
              {item.kind === 'group' && <span className="graph-review-members">{item.members.map(id => item.titles[id]).join('、')}</span>}
              <span>{item.reason}</span>
            </span>
          </label>
        </li>)}</ul>
        {last > 0 && <div className="graph-review-choices">
          <button type="button" className="text-action" disabled={present === 0} onClick={() => { setPage(present - 1); setPicked([]); }}>上一页</button>
          <small>{present + 1} / {last + 1}</small>
          <button type="button" className="text-action" disabled={present === last} onClick={() => { setPage(present + 1); setPicked([]); }}>下一页</button>
        </div>}
      </> : <Empty title="暂无待审核候选" />}
      {queue.jobs.length > 0 && <details className="graph-review-jobs" open={queue.jobs.some(job => ['queued', 'running', 'blocked'].includes(job.state)) || undefined}>
        <summary>最近的推荐请求</summary>
        <ul>{queue.jobs.map(job => <li key={job.id}>
          <strong>{job.state === 'ready' ? job.count ? '已生成 ' + job.count + ' 项候选' : '没有新的候选' : JOB_STATES[job.state]}</strong>
          <small>{job.recipient.model}{job.usage?.inputTokens !== undefined && ' · 输入 ' + job.usage.inputTokens + ' Token'}
            {job.usage?.outputTokens !== undefined && ' · 输出 ' + job.usage.outputTokens + ' Token'}</small>
          {job.error && <p>{job.error}</p>}
          {job.budgetBlocked && <Link className="text-action" to="/settings/budget">打开额度恢复</Link>}
          {['queued', 'running', 'blocked'].includes(job.state) && <button type="button" className="text-action" disabled={action.pending}
            onClick={() => void action.run(async () => {
              await api(GRAPH_RECOMMENDATIONS + '/' + job.id + '/cancel', 'POST');
              refresh();
            })}>停止推荐</button>}
        </li>)}</ul>
      </details>}
    </>}
  </div>;
}
