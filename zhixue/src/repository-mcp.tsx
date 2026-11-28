import { useState } from 'react';
import { api } from './api';
import { ErrorNote, Loading, Modal, useAction, useRead } from './shared';

const API = '/repository-mcp';
type Kind = 'issue' | 'merge';

export type McpChoice = { id: string; name: string; endpoint: string; kinds: Kind[] };
export type McpReference = {
  repository: string;
  kind: Kind;
  number: number;
  source: string;
  excerpt: string;
  truncated: boolean;
};
export type McpEvidence = McpReference & {
  connectionRevision: number; agentRevision: number; expiresAt: number; confirmation: string;
};

export function McpSourceLink({ source, label }: { source: string; label: string }) {
  let address: URL;
  try {
    address = new URL(source);
  } catch {
    return null;
  }
  if (address.protocol !== 'https:' || address.username || address.password ||
    !['github.com', 'gitlab.com'].includes(address.host)) return null;
  return <a href={address.href} target="_blank" rel="noreferrer">{label}</a>;
}

type Connection = {
  id: 'github' | 'gitlab'; name: string; endpoint: string; revision: number;
  enabled: boolean; connected: boolean; repositories: string[];
};

function ConnectorDialog({ connection, close, saved }: { connection: Connection; close: () => void; saved: () => void }) {
  const [credential, setCredential] = useState('');
  const [repositories, setRepositories] = useState(connection.repositories.join('\n'));
  const [enabled, setEnabled] = useState(connection.enabled);
  const action = useAction();
  const paths = repositories.split('\n').map(path => path.trim()).filter(Boolean);

  async function save() {
    const result = await action.run(() => api<Connection>(API + '/' + connection.id, 'PUT', {
      revision: connection.revision, enabled, repositories: paths, credential: credential.trim() || undefined,
    }));
    if (result) saved();
  }

  return <Modal title={'配置 ' + connection.name + ' MCP'} onClose={close} busy={action.pending}>
    <div className="project-dialog-form">
      <div className="generation-destination"><strong>{connection.name} MCP</strong><code>{connection.endpoint}</code></div>
      <label className="project-field">MCP 访问令牌
        <input type="password" autoComplete="off" maxLength={2048} value={credential} disabled={action.pending}
          placeholder={connection.connected ? '已保存在系统凭据库' : undefined} onChange={event => setCredential(event.target.value)} />
      </label>
      <p className="model-help">{connection.id === 'github'
        ? '使用具有所选仓库 Issue 与 PR 读取权限的 GitHub 访问令牌。'
        : '使用带 mcp 范围的 OAuth 令牌，或具有 MCP 工具执行权限的细粒度访问令牌。'}</p>
      <label className="project-field">允许读取的仓库
        <textarea value={repositories} rows={3} maxLength={20000} disabled={action.pending}
          onChange={event => setRepositories(event.target.value)} />
      </label>
      <p className="model-help">每行一个完整仓库路径（例如 owner/repository）。保存后可单独授予 Agent。移除仓库会撤销对应授权。</p>
      <label className="project-check">
        <input type="checkbox" checked={enabled} disabled={action.pending} onChange={event => setEnabled(event.target.checked)} />
        启用只读连接
      </label>
      <ErrorNote message={action.error} />
      <div className="project-dialog-actions">
        <button type="button" className="button primary" disabled={action.pending || paths.length > 40 ||
          (enabled && (!paths.length || (!connection.connected && !credential.trim())))}
          onClick={() => void save()}>{enabled || credential.trim() ? '验证并保存连接' : '保存连接设置'}</button>
      </div>
    </div>
  </Modal>;
}

export function RepositoryConnections({ changed }: { changed: () => void }) {
  const connections = useRead<{ items: Connection[] }>(API);
  const [editing, setEditing] = useState<Connection>();
  return <>
    <ErrorNote message={connections.error} retry={connections.refresh} />
    {!!connections.value?.items.length && <div className="action-row">
      {connections.value.items.map(connection => <button type="button" key={connection.id} className="button soft"
        onClick={() => setEditing(connection)}>配置 {connection.name} MCP</button>)}
    </div>}
    {editing && <ConnectorDialog connection={editing} close={() => setEditing(undefined)} saved={() => {
      setEditing(undefined);
      connections.refresh();
      changed();
    }} />}
  </>;
}

export function McpRepositoryPicker({ agent, thread, message, value, pending, busy, changed }: {
  agent: string;
  thread: string;
  message: string;
  value: McpEvidence | undefined;
  pending: boolean;
  busy: (loading: boolean) => void;
  changed: (result: McpEvidence | undefined) => void;
}) {
  const [open, setOpen] = useState(false);
  const options = useRead<{ items: McpChoice[] }>(open ? '/agents/' + agent + '/mcp-repositories' : null);
  const [repository, setRepository] = useState('');
  const [kind, setKind] = useState<Kind>('issue');
  const [number, setNumber] = useState('');
  const action = useAction();
  const selected = options.value?.items.find(option => option.id === repository);
  const parsed = Number(number);
  const valid = !!selected?.kinds.includes(kind) && Number.isInteger(parsed) && parsed >= 1 && parsed <= 2147483647;

  async function read() {
    if (!valid) return;
    busy(true);
    changed(undefined);
    try {
      const result = await action.run(() => api<McpEvidence>(API + '/read', 'POST', {
        agentId: agent, threadId: thread, messageId: message, repository, kind, number: parsed,
      }));
      if (result) changed(result);
    } finally {
      busy(false);
    }
  }

  return <details onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>读取已授权的仓库资料</summary>
    {open && <>
      <ErrorNote message={options.error} retry={options.refresh} />
      {!options.value && !options.error && <Loading />}
      {options.value?.items.length === 0 && <p className="model-help">此 Agent 当前没有可用的仓库连接。</p>}
      {!!options.value?.items.length && <fieldset className="agent-fields" disabled={pending || action.pending}>
        <legend>选择一个只读条目</legend>
        <label className="project-field">MCP 仓库
          <select value={repository} onChange={event => {
            const choice = options.value?.items.find(item => item.id === event.target.value);
            setRepository(event.target.value);
            setKind(choice?.kinds[0] ?? 'issue');
            changed(undefined);
          }}>
            <option value="">选择仓库</option>
            {options.value.items.map(choice => <option key={choice.id} value={choice.id}>{choice.name}</option>)}
          </select>
        </label>
        {selected && <>
          <label className="project-field">条目类型
            <select value={kind} onChange={event => { setKind(event.target.value as Kind); changed(undefined); }}>
              {selected.kinds.map(value => <option value={value} key={value}>{value === 'issue' ? 'Issue' : '合并请求'}</option>)}
            </select>
          </label>
          <label className="project-field">条目编号
            <input type="number" min={1} max={2147483647} value={number} onChange={event => { setNumber(event.target.value); changed(undefined); }} />
          </label>
          <p className="model-help">读取会联系 {selected.endpoint}，查看指定仓库的条目。</p>
          <div className="action-row">
            <button type="button" className="button soft" disabled={!valid} onClick={() => void read()}>读取并预览仓库资料</button>
            {value && <button type="button" className="button soft" onClick={() => changed(undefined)}>移除仓库资料</button>}
          </div>
        </>}
      </fieldset>}
      <ErrorNote message={action.error} />
    </>}
  </details>;
}
