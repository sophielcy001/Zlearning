import { useCallback, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { api } from './api';
import type { AgentConfig } from './professional-agents';
import { activeExternalCopy, ExternalExportDialog, ExternalMemoryPanel, useExternalMemory } from './external-memory';
import { MemoryInferenceDialog, MemoryInferenceJobs } from './memory-inference';
import { Empty, ErrorNote, Loading, Modal, useAction, useRead } from './shared';

type MemorySource = { kind: 'manual' } | { kind: 'chat'; messageId: string; conversationId: string };
type Entry = {
  id: string;
  text: string;
  state: 'active' | 'pending';
  revision: number;
  agentId: string | null;
  origin: 'explicit' | 'inferred';
  source: MemorySource;
  reviewOn: string | null;
};
type Original = { id: string; text: string };
type View = 'shared' | 'private' | 'pending';

const views = [
  { id: 'shared', label: '共享记忆' },
  { id: 'private', label: 'Agent 专属' },
  { id: 'pending', label: '待确认' },
] as const;

function MemoryEditor({ entry, original, close, saved }: {
  entry?: Entry;
  original?: Original;
  close: () => void;
  saved: (result: Entry) => void;
}) {
  const [id] = useState(() => crypto.randomUUID());
  const [text, setText] = useState(entry?.text ?? original?.text ?? '');
  const [agent, setAgent] = useState('');
  const candidates = useRead<{ items: AgentConfig[] }>(entry ? null : '/agents');
  const action = useAction();

  async function save() {
    const result = await action.run(() => entry
      ? api<Entry>('/memories/' + entry.id, 'PUT', { text, revision: entry.revision })
      : api<Entry>('/memories', 'POST', { id, text, agentId: agent || null, sourceMessageId: original?.id }));
    if (result) saved(result);
  }

  return <Modal title={entry ? '编辑记忆' : '新增记忆'} onClose={close} busy={action.pending}>
    <div className="project-dialog-form">
      <label className="project-field">记忆内容
        <textarea value={text} maxLength={2000} disabled={action.pending} onChange={event => setText(event.target.value)} />
      </label>
      {!entry && <label className="project-field">可见范围
        <select value={agent} disabled={action.pending} onChange={event => setAgent(event.target.value)}>
          <option value="">共享记忆（待确认后生效）</option>
          {candidates.value?.items.map(item => <option value={item.id} key={item.id}>Agent 专属 · {item.name}</option>)}
        </select>
      </label>}
      <ErrorNote message={candidates.error} retry={candidates.refresh} />
      <ErrorNote message={action.error} />
      <div className="project-dialog-actions">
        <button type="button" className="button primary" disabled={!text.trim() || action.pending}
          onClick={() => void save()}>保存记忆</button>
      </div>
    </div>
  </Modal>;
}

export function ChatMemoryAction({ original }: { original: Original }) {
  const [mode, setMode] = useState<'manual' | 'inference'>();
  const [saved, setSaved] = useState<Entry>();
  const [queued, setQueued] = useState(false);
  return <div className="action-row message-actions">
    {saved ? <span className="model-help" role="status">{saved.state === 'pending' ? '已加入待确认' : '已保存 Agent 专属记忆'}</span>
      : queued ? <Link className="text-action" to="/settings/memory?view=pending">查看整理记录</Link>
        : <>
          <button type="button" className="text-action" onClick={() => setMode('manual')}>保存为记忆</button>
          <button type="button" className="text-action" onClick={() => setMode('inference')}>AI 整理记忆</button>
        </>}
    {mode === 'manual' && <MemoryEditor original={original} close={() => setMode(undefined)} saved={result => {
      setSaved(result);
      setMode(undefined);
    }} />}
    {mode === 'inference' && <MemoryInferenceDialog sourceId={original.id} close={() => setMode(undefined)} queued={() => {
      setQueued(true);
      setMode(undefined);
    }} />}
  </div>;
}

function SourceDetails({ id }: { id: string }) {
  const source = useRead<{ text: string; conversationId: string }>('/memories/' + id + '/source');
  return <>
    <ErrorNote message={source.error} retry={source.refresh} />
    {!source.value && !source.error && <Loading />}
    {source.value && <>
      <blockquote className="question-excerpt">{source.value.text}</blockquote>
      <Link to={'/chat/' + source.value.conversationId} className="text-action">打开原对话</Link>
    </>}
  </>;
}

function MemoryDetails({ entry, agent }: { entry: Entry; agent?: string }) {
  const [expanded, setExpanded] = useState(false);
  return <details onToggle={event => setExpanded(event.currentTarget.open)}>
    <summary>查看来源与范围</summary>
    <dl className="skill-fields">
      <div><dt>来源</dt><dd>{entry.source.kind === 'chat' ? '对话原话' : '手动添加'}</dd></div>
      <div><dt>可见范围</dt><dd>{entry.agentId ? 'Agent 专属 · ' + (agent ?? '原 Agent')
        : entry.state === 'pending' ? '待确认共享' : '共享记忆'}</dd></div>
    </dl>
    {entry.source.kind === 'chat' && expanded && <SourceDetails id={entry.id} />}
  </details>;
}

function PendingMemoryEditor({ entries, agents, close, saved }: {
  entries: Entry[]; agents: AgentConfig[]; close: () => void; saved: (updated: number) => void;
}) {
  const [texts, setTexts] = useState(() => entries.map(entry => entry.text));
  const action = useAction();
  const changes = entries.flatMap((entry, index) => {
    const text = texts[index];
    return text === entry.text ? [] : [{ id: entry.id, revision: entry.revision, text }];
  });
  const invalid = changes.some(({ text }) => !text.trim());

  async function save() {
    if (invalid || !changes.length) return;
    const result = await action.run(() => api<{ updated: number }>('/memories/review', 'PATCH', { items: changes }));
    if (result) saved(result.updated);
  }

  return <Modal title="批量编辑待确认记忆" onClose={close} busy={action.pending}>
    <form className="project-dialog-form" onSubmit={event => { event.preventDefault(); void save(); }}>
      <p className="model-help">已选 {entries.length} 条。保存后仍可在待确认中核对共享范围。</p>
      {entries.map((entry, index) => <section key={entry.id} className="panel settings">
        <label className="project-field">第 {index + 1} 条记忆内容
          <textarea value={texts[index]} rows={3} required maxLength={2000} disabled={action.pending}
            onChange={event => {
              const text = event.target.value;
              setTexts(previous => previous.map((value, position) => position === index ? text : value));
            }} />
        </label>
        {texts[index] !== entry.text && <details>
          <summary>查看修改前的记忆</summary><p className="evidence-text">{entry.text}</p>
        </details>}
        <MemoryDetails entry={entry} agent={agents.find(candidate => candidate.id === entry.agentId)?.name} />
      </section>)}
      <ErrorNote message={action.error} />
      <div className="project-dialog-actions">
        <button type="button" className="button secondary" disabled={action.pending} onClick={close}>取消</button>
        <button type="submit" className="button primary" disabled={action.pending || invalid || !changes.length}>
          {action.pending ? '正在保存' : '保存所选修改'}
        </button>
      </div>
    </form>
  </Modal>;
}

export function LocalMemory({ onChange }: { onChange: () => void }) {
  const [params, setParams] = useSearchParams();
  const scope = params.get('view');
  const view: View = scope === 'private' || scope === 'pending' ? scope : 'shared';
  const items = useRead<{ items: Entry[] }>('/memories?view=' + view);
  const external = useExternalMemory();
  const externalIds = new Set(external.value?.copies.filter(activeExternalCopy).map(copy => copy.memoryId));
  const [exporting, setExporting] = useState<Entry>();
  const agents = useRead<{ items: AgentConfig[] }>('/agents');
  const action = useAction();
  const [editing, setEditing] = useState<{ entry?: Entry }>();
  const [batch, setBatch] = useState<Entry[]>();
  const [batchSaved, setBatchSaved] = useState<number>();
  const [deleting, setDeleting] = useState<Entry>();
  const [selected, setSelected] = useState<string[]>([]);
  const choices = items.value?.items.filter(item => selected.includes(item.id)) ?? [];
  const refresh = useCallback(() => {
    items.refresh();
    external.refresh();
    onChange();
  }, [items.refresh, external.refresh, onChange]);

  function change(next?: View) {
    setSelected([]);
    setBatchSaved(undefined);
    if (next && next !== view) setParams(next === 'shared' ? {} : { view: next });
    refresh();
  }

  async function mutate(entry: Entry, verb: 'share' | 'delete') {
    const result = await action.run(() => api('/memories/' + entry.id + '/' + verb, 'POST', { revision: entry.revision }));
    if (result) {
      setDeleting(undefined);
      change();
    }
  }

  async function decide(verb: 'accept' | 'reject' | 'defer') {
    if (!choices.length) return;
    const result = await action.run(() => api('/memories/review', 'POST', {
      action: verb, items: choices.map(item => ({ id: item.id, revision: item.revision })),
    }));
    if (result) change();
  }

  return <section className="panel settings">
    <div className="skill-library-heading">
      <h2>记忆管理</h2>
      <button type="button" className="button primary" onClick={() => setEditing({})}>新增记忆</button>
    </div>
    <div className="segmented" role="group" aria-label="记忆范围">
      {views.map(tab => <button type="button" key={tab.id} aria-pressed={tab.id === view}
        disabled={action.pending} onClick={() => change(tab.id)}>{tab.label}</button>)}
    </div>
    {!deleting && <ErrorNote message={action.error} retry={items.refresh} />}
    <ErrorNote message={items.error} retry={items.refresh} />
    {batchSaved !== undefined && <p className="notice success" role="status">
      {batchSaved > 0 ? `已保存 ${batchSaved} 条待确认记忆修改。` : '内容与当前版本一致。'}
    </p>}
    {!items.value && !items.error && <Loading />}
    {items.value?.items.length === 0 && <Empty title="这里还没有记忆" />}
    {view === 'pending' && !!items.value?.items.length && <div className="action-row">
      <button type="button" className="button primary" disabled={!choices.length || action.pending}
        onClick={() => void decide('accept')}>接受所选</button>
      <button type="button" className="button soft" disabled={!choices.length || action.pending}
        onClick={() => { setBatchSaved(undefined); setBatch(choices); }}>修改所选</button>
      <button type="button" className="button soft" disabled={!choices.length || action.pending}
        onClick={() => void decide('reject')}>拒绝所选</button>
      <button type="button" className="button soft" disabled={!choices.length || action.pending}
        onClick={() => void decide('defer')}>延后到明天</button>
    </div>}
    {!!items.value?.items.length && <ul className="skill-list">
      {items.value.items.map(entry => <li key={entry.id}>
        <div>
          {view === 'pending' && <label className="project-check">
            <input type="checkbox" checked={selected.includes(entry.id)}
              disabled={action.pending || (selected.length >= 100 && !selected.includes(entry.id))}
              onChange={event => setSelected(event.target.checked
                ? [...selected, entry.id] : selected.filter(id => id !== entry.id))} />选择记忆
          </label>}
          <p className="evidence-text">{entry.text}</p>
          <p className="model-help">{entry.origin === 'inferred' ? 'AI 推断' : '用户明确表达'}</p>
          {entry.reviewOn && view === 'pending' && <p className="model-help">延后至 {entry.reviewOn}</p>}
          {entry.state === 'pending' && entry.agentId && <p className="model-help">待共享确认</p>}
          <MemoryDetails entry={entry} agent={agents.value?.items.find(agent => agent.id === entry.agentId)?.name} />
        </div>
        <div className="action-row">
          <button type="button" className="button soft" disabled={action.pending} onClick={() => setEditing({ entry })}>编辑</button>
          {entry.state === 'active' && external.value?.connected && !externalIds.has(entry.id) &&
            external.value.scopes.includes(entry.agentId ? 'private' : 'shared') &&
            <button type="button" className="button soft" disabled={action.pending}
              onClick={() => setExporting(entry)}>发送到 Mem0</button>}
          {view === 'private' && entry.state === 'active' && <button type="button" className="button soft"
            disabled={action.pending} onClick={() => void mutate(entry, 'share')}>申请共享</button>}
          <button type="button" className="button soft" disabled={action.pending}
            onClick={() => setDeleting(entry)}>删除</button>
        </div>
      </li>)}
    </ul>}
    <ExternalMemoryPanel state={external} />
    <MemoryInferenceJobs changed={refresh} />
    {exporting && <ExternalExportDialog memory={exporting} close={() => setExporting(undefined)}
      queued={() => { setExporting(undefined); external.refresh(); }} />}
    {editing && <MemoryEditor {...editing} close={() => setEditing(undefined)} saved={result => {
      change(editing.entry ? undefined : result.state === 'pending' ? 'pending' : 'private');
      setEditing(undefined);
    }} />}
    {batch && <PendingMemoryEditor entries={batch} agents={agents.value?.items ?? []}
      close={() => { setBatch(undefined); refresh(); }} saved={updated => {
        setBatch(undefined);
        change();
        setBatchSaved(updated);
      }} />}
    {deleting && <Modal title="删除记忆" onClose={() => setDeleting(undefined)} busy={action.pending}>
      <div className="project-dialog-form">
        <p className="evidence-text">{deleting.text}</p>
        <p className="model-help">删除将停用此记忆。来源对话会继续保留。</p>
        {externalIds.has(deleting.id) && <p className="model-help">智学将同步删除外部副本，待 Mem0 确认后更新状态。</p>}
        <ErrorNote message={action.error} />
        <div className="project-dialog-actions">
          <button type="button" className="button primary" disabled={action.pending}
            onClick={() => void mutate(deleting, 'delete')}>确认删除</button>
        </div>
      </div>
    </Modal>}
  </section>;
}