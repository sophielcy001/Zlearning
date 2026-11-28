import { useState } from 'react';
import { api } from './api';
import { AgentDrafts, AUTHORING, useAgentAuthoring, type AgentSuggestion, type AuthoringJob } from './agent-authoring';
import type { Consent } from './generation';
import type { MemoryScope } from './conversation';
import type { LibraryFolder } from './library-folders';
import type { McpChoice } from './repository-mcp';
import { Empty, ErrorNote, Loading, Modal, useAction, useRead } from './shared';
import type { Reviewer } from './verification';

export type AgentConfig = {
  id: string;
  revision: number;
  name: string;
  purpose: string;
  responsibility: string;
  connectionId: string | null;
  skills: string[];
  knowledgeFolders?: string[];
  memoryScopes?: MemoryScope[];
  autoMemory?: boolean;
  mcpRepositories?: string[];
};

type Draft = Omit<AgentConfig, 'revision'> & { revision?: number };
type SkillOption = { name: string; description: string };
type ModelOption = { id: string; name: string; model: string; provider: string; hasCredential: boolean };
type AgentPreview = {
  recipient: Reviewer | null;
  skills: { name: string; checksum: string; version: string | null; declaredTools: string | null }[];
  knowledgeFolders?: string[];
  memoryScopes?: MemoryScope[];
  autoMemory?: boolean;
  mcpRepositories?: string[];
  confirmation: string;
};

const memoryScopes = [
  { value: 'shared', label: '共享记忆' },
  { value: 'private', label: '自身专属记忆' },
] as const;

type DraftDisclosure = { id: string; purpose: string; recipient: Reviewer; consent: Consent };

function blankAgent(): Draft {
  return { id: crypto.randomUUID(), name: '', purpose: '', responsibility: '', connectionId: null, skills: [] };
}

function AgentEditor({ existing, suggestion, close, saved, queued }: {
  existing?: AgentConfig; suggestion?: AgentSuggestion; close: () => void; saved: () => void; queued: () => void;
}) {
  const [draft, setDraft] = useState<Draft>(() => existing ?? { ...blankAgent(), ...suggestion });
  const [describe, setDescribe] = useState(!existing && !suggestion);
  const [disclosure, setDisclosure] = useState<DraftDisclosure>();
  const [preview, setPreview] = useState<AgentPreview>();
  const action = useAction();
  const skills = useRead<{ items: SkillOption[] }>('/skills');
  const models = useRead<{ connections: ModelOption[] }>('/models');
  const folders = useRead<LibraryFolder[]>(describe ? null : '/files/folders');
  const repositories = useRead<{ items: McpChoice[] }>(describe ? null : '/repository-mcp/repositories');
  const grantedRepositories = draft.mcpRepositories ?? [];
  const grantedFolders = draft.knowledgeFolders ?? [];
  const grantedMemory = draft.memoryScopes ?? [];
  const connections = models.value?.connections.filter(item => item.provider === 'custom' || item.hasCredential);

  async function previewDraft() {
    const id = crypto.randomUUID();
    const found = await action.run(() => api<DraftDisclosure>(AUTHORING + '/preview', 'POST', { id, purpose: draft.purpose }));
    if (found) setDisclosure({ ...found, id });
  }

  async function queueDraft() {
    if (!disclosure) return;
    const { id, purpose, consent } = disclosure;
    const job = await action.run(() => api<AuthoringJob>(AUTHORING, 'POST', { id, purpose, consent }));
    if (job) queued();
  }


  async function inspect() {
    const result = await action.run(() => api<AgentPreview>('/agents/preview', 'POST', draft));
    if (result) setPreview(result);
  }

  async function confirm() {
    if (!preview) return;
    const result = await action.run(() => api<AgentConfig>('/agents', 'POST', { ...draft, confirmation: preview.confirmation }));
    if (result) saved();
  }

  return <Modal title={existing ? '编辑专业 Agent' : '新建专业 Agent'} onClose={close} busy={action.pending}>
    <div className="project-dialog-form">
      {describe ? <>
        {disclosure ? <>
          <div className="generation-destination"><strong>{disclosure.recipient.name} · {disclosure.recipient.model}</strong>
            <code>{disclosure.recipient.address}</code></div>
          <dl className="generation-inputs" aria-label="AI 起草发送范围">
            <div><dt>将发送的需求描述</dt><dd className="evidence-text">{disclosure.purpose}</dd></div>
          </dl>
          <p className="model-help">确认后调用主模型一次。草稿中的名称和职责可以继续编辑，权限须另外确认。</p>
          <div className="project-dialog-actions">
            <button type="button" className="button soft" disabled={action.pending} onClick={() => setDisclosure(undefined)}>返回描述</button>
            <button type="button" className="button primary" disabled={action.pending} onClick={() => void queueDraft()}>确认生成草稿</button>
          </div>
        </> : <>
          <label className="project-field">需求描述<textarea value={draft.purpose} maxLength={2000}
            disabled={action.pending} onChange={event => setDraft({ ...draft, purpose: event.target.value })} /></label>
          <div className="project-dialog-actions">
            <button type="button" className="button soft" disabled={action.pending || !draft.purpose.trim()} onClick={() => {
              setDraft({ ...draft, responsibility: draft.purpose.trim() });
              setDescribe(false);
            }}>编辑配置</button>
            <button type="button" className="button primary" disabled={action.pending || !draft.purpose.trim()}
              onClick={() => void previewDraft()}>预览 AI 起草</button>
          </div>
        </>}

      </> : preview ? <>
        <dl className="generation-inputs" aria-label="Agent 配置与授权">
          <div><dt>名称</dt><dd>{draft.name}</dd></div>
          <div><dt>需求描述</dt><dd>{draft.purpose}</dd></div>
          <div><dt>职责</dt><dd className="evidence-text">{draft.responsibility}</dd></div>
          <div><dt>模型</dt><dd>{preview.recipient
            ? <>{preview.recipient.name} · {preview.recipient.model}<br /><code>{preview.recipient.address}</code></>
            : '使用主模型，当前需要配置连接'}</dd></div>
          <div><dt>已授权的 Skills</dt><dd>{preview.skills.length ? <ul>
            {preview.skills.map(skill => <li key={skill.name}>{skill.name}
              {skill.version && ' · ' + skill.version}{skill.declaredTools && ' · 声明工具 ' + skill.declaredTools}</li>)}
          </ul> : '未选择'}</dd></div>
          <div><dt>可发送范围</dt><dd>本次问题和发送时勾选的对话历史。Skill 原文随每次发送单独确认。</dd></div>
          <div><dt>知识库检索</dt><dd>{preview.knowledgeFolders?.length ? <>
            <span>只读检索以下文件夹及其子目录。每次发送仍需确认。</span>
            <ul>{preview.knowledgeFolders.map(path => <li key={path}>{path}</li>)}</ul>
          </> : '未授予'}</dd></div>
          {!!preview.mcpRepositories?.length && <div><dt>MCP 仓库只读</dt>
            <dd><ul>{preview.mcpRepositories.map(path => <li key={path}>{path}</li>)}</ul></dd>
          </div>}
          <div><dt>记忆授权</dt><dd>{preview.memoryScopes?.length ? <ul>
            {memoryScopes.filter(scope => preview.memoryScopes?.includes(scope.value))
              .map(scope => <li key={scope.value}>{scope.label}</li>)}
          </ul> : '未授予'}</dd></div>
          {preview.autoMemory && <div><dt>自动整理专属记忆</dt>
            <dd>每次新问题收到回复后，向上述模型仅发送本轮用户原话进行一次整理。建议只保存在此 Agent 的专属范围，可在此关闭自动整理。</dd>
          </div>}
        </dl>
        <div className="project-dialog-actions">
          <button type="button" className="button soft" disabled={action.pending} onClick={() => setPreview(undefined)}>返回编辑</button>
          <button type="button" className="button primary" disabled={action.pending} onClick={() => void confirm()}>确认保存 Agent</button>
        </div>
      </> : <>
        <fieldset className="agent-fields" disabled={action.pending}>
          <legend>职责与模型</legend>
          <label className="project-field">Agent 名称<input value={draft.name} maxLength={80}
            onChange={event => setDraft({ ...draft, name: event.target.value })} /></label>
          <label className="project-field">需求描述<textarea value={draft.purpose} maxLength={2000}
            onChange={event => setDraft({ ...draft, purpose: event.target.value })} /></label>
          <label className="project-field">职责<textarea value={draft.responsibility} maxLength={2000}
            onChange={event => setDraft({ ...draft, responsibility: event.target.value })} /></label>
          <label className="project-field">模型连接<select value={draft.connectionId ?? ''}
            onChange={event => setDraft({ ...draft, connectionId: event.target.value || null })}>
            <option value="">使用已分配的主模型</option>
            {draft.connectionId && !connections?.some(item => item.id === draft.connectionId) &&
              <option value={draft.connectionId}>原连接</option>}
            {connections?.map(item => <option key={item.id} value={item.id}>{item.name} · {item.model}</option>)}
          </select></label>
          <ErrorNote message={models.error} retry={models.refresh} />
        </fieldset>
        <fieldset className="agent-fields" disabled={action.pending}>
          <legend>工具与 Skills</legend>
          <ErrorNote message={skills.error} retry={skills.refresh} />
          {!skills.value && !skills.error && <Loading text="正在读取能力库" />}
          {skills.value?.items.length === 0 && <p>能力库还没有已安装的 Skill。</p>}
          {skills.value?.items.map(skill => <label className="project-check" key={skill.name}>
            <input type="checkbox" checked={draft.skills.includes(skill.name)} onChange={event => {
              const next = event.target.checked ? [...draft.skills, skill.name] : draft.skills.filter(name => name !== skill.name);
              setDraft({ ...draft, skills: next });
            }} />{skill.name}
          </label>)}
        </fieldset>
        <fieldset className="agent-fields" disabled={action.pending}>
          <legend>知识范围</legend>
          <label className="project-field">添加授权文件夹
            <select value="" disabled={!folders.value || grantedFolders.length >= 12} onChange={event => {
              if (event.target.value) setDraft({ ...draft, knowledgeFolders: [...grantedFolders, event.target.value] });
            }}>
              <option value="">选择可检索的文件夹</option>
              {folders.value?.filter(entry => !grantedFolders.includes(entry.path)).map(entry =>
                <option key={entry.path} value={entry.path}>{entry.parent ? entry.path : entry.name}</option>)}
            </select>
          </label>
          <ErrorNote message={folders.error} retry={folders.refresh} />
          {!!grantedFolders.length && <ul className="skill-list">
            {grantedFolders.map(path => <li key={path}><span>{path}</span>
              <button type="button" className="button soft" aria-label={'撤销 ' + path}
                onClick={() => setDraft({ ...draft, knowledgeFolders: grantedFolders.filter(item => item !== path) })}>撤销</button>
            </li>)}
          </ul>}
        </fieldset>
        {(!!repositories.value?.items.length || !!grantedRepositories.length || repositories.error) &&
          <fieldset className="agent-fields" disabled={action.pending}>
            <legend>MCP 仓库只读</legend>
            <ErrorNote message={repositories.error} retry={repositories.refresh} />
            {repositories.value?.items.map(item => <label className="project-check" key={item.id}>
              <input type="checkbox" checked={grantedRepositories.includes(item.id)} onChange={event =>
                setDraft({ ...draft, mcpRepositories: event.target.checked
                  ? [...grantedRepositories, item.id] : grantedRepositories.filter(id => id !== item.id) })} />{item.name}
            </label>)}
            {grantedRepositories.filter(id => repositories.value && !repositories.value.items.some(item => item.id === id))
              .map(id => <button type="button" className="button soft" key={id}
                onClick={() => setDraft({ ...draft, mcpRepositories: grantedRepositories.filter(value => value !== id) })}>
                移除不可用仓库 {id}
              </button>)}
          </fieldset>}
        <fieldset className="agent-fields" disabled={action.pending}>
          <legend>记忆授权</legend>
          {memoryScopes.map(scope => <label className="project-check" key={scope.value}>
            <input type="checkbox" checked={grantedMemory.includes(scope.value)} onChange={event => {
              setDraft({ ...draft, memoryScopes: event.target.checked
                ? [...grantedMemory, scope.value] : grantedMemory.filter(value => value !== scope.value) });
            }} />读取{scope.label}
          </label>)}
          <label className="project-check">
            <input type="checkbox" checked={draft.autoMemory ?? false}
              onChange={event => setDraft({ ...draft, autoMemory: event.target.checked })} />自动整理专属记忆
          </label>
        </fieldset>
        <div className="project-dialog-actions">
          <button type="button" className="button primary" disabled={action.pending ||
            ![draft.name, draft.purpose, draft.responsibility].every(value => value.trim())}
            onClick={() => void inspect()}>预览配置</button>
        </div>
      </>}
      <ErrorNote message={action.error} />
    </div>
  </Modal>;
}

export function ProfessionalAgents() {
  const list = useRead<{ items: AgentConfig[] }>('/agents');
  const [editing, setEditing] = useState<{ existing?: AgentConfig; suggestion?: AgentSuggestion }>();
  const drafts = useAgentAuthoring();
  return <section className="panel settings">
    <div className="skill-library-heading">
      <h2>专业 Agent</h2>
      <button type="button" className="button primary" onClick={() => setEditing({})}>新建专业 Agent</button>
    </div>
    <ErrorNote message={list.error} retry={list.refresh} />
    {!list.value && !list.error && <Loading />}
    {list.value?.items.length === 0 && <Empty title="还没有专业 Agent" />}
    {!!list.value?.items.length && <ul className="skill-list">
      {list.value.items.map(agent => <li key={agent.id}>
        <div><strong>{agent.name}</strong><p>{agent.responsibility}</p>
          {!!agent.skills.length && <p>已授权 Skills · {agent.skills.join('、')}</p>}
          {!!agent.knowledgeFolders?.length && <p>知识库检索 · {agent.knowledgeFolders.length} 个文件夹</p>}
          {agent.autoMemory && <p>已授权自动整理专属记忆</p>}
          {!!agent.mcpRepositories?.length && <p>MCP 仓库只读 · {agent.mcpRepositories.length} 个仓库</p>}</div>
        <button type="button" className="button soft" onClick={() => setEditing({ existing: agent })}>编辑 {agent.name}</button>
      </li>)}
    </ul>}
    <AgentDrafts drafts={drafts} adopt={suggestion => setEditing({ suggestion })} />
    {editing && <AgentEditor {...editing} queued={() => { setEditing(undefined); drafts.refresh(); }}
      close={() => { setEditing(undefined); list.refresh(); }}
      saved={() => { setEditing(undefined); list.refresh(); }} />}
  </section>;
}
