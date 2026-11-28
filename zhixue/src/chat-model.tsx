import { useEffect, useState } from 'react';
import { api } from './api';
import type { ChatAgentTrace, ChatSkillTrace, MemorySelection, Thread } from './conversation';
import { ErrorNote, Loading, Modal, useAction, useRead } from './shared';
import { TEACHING_STYLE_NAMES, type TeachingStyle } from './teaching-style';
import type { Reviewer } from './verification';
import { McpRepositoryPicker, McpSourceLink, type McpEvidence } from './repository-mcp';

export type ModelChatDraft = {
  conversationId: string;
  id: string;
  text: string;
  attachments?: string[];
  retry?: boolean;
  autoSkill?: boolean;
  knowledgeEnabled?: boolean;
  memoryAgentId?: string;
  mcpEnabled?: boolean;
  agentId?: string;
  skill?: string;
};

type Preview = {
  recipient: Reviewer;
  question: string;
  style: TeachingStyle;
  agent?: Omit<ChatAgentTrace, 'skill' | 'memories'> & {
    responsibility: string; skill: (ChatSkillTrace & { source: string }) | null; memories?: MemorySelection[];
    autoMemory?: boolean;
  };
  history: { role: 'user' | 'assistant'; text: string }[];
  confirmation: string;
};

export function useChatModel(enabled = true) {
  return useRead<{ available: boolean }>(enabled ? '/chat-model' : null);
}

const MEMORY_LIMIT = 4;

function MemoryPicker({ agent, selected, changed, pending }: {
  agent: string; selected: string[]; changed: (ids: string[]) => void; pending: boolean;
}) {
  const [open, setOpen] = useState(false);
  const memories = useRead<{ items: MemorySelection[] }>(open ? '/agents/' + agent + '/memories' : null);
  return <details onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>选择本轮附带的记忆</summary>
    {open && <>
      <ErrorNote message={memories.error} retry={memories.refresh} />
      {!memories.value && !memories.error && <Loading />}
      {memories.value?.items.length === 0 && <p className="model-help">当前没有可选的已授权记忆。</p>}
      {!!memories.value?.items.length && <fieldset className="agent-fields" disabled={pending}>
        <legend>最多选择 {MEMORY_LIMIT} 条</legend>
        {memories.value.items.map(item => <label className="project-check" key={item.id}>
          <input type="checkbox" checked={selected.includes(item.id)}
            disabled={selected.length >= MEMORY_LIMIT && !selected.includes(item.id)}
            onChange={event => changed(event.target.checked ? [...selected, item.id] : selected.filter(id => id !== item.id))} />
          <span className="evidence-text">{item.scope === 'private' ? '专属' : '共享'} · {item.text}</span>
        </label>)}
      </fieldset>}
      {!!selected.length && <button type="button" className="button soft" disabled={pending}
        onClick={() => { changed([]); memories.refresh(); }}>移除已选记忆</button>}
    </>}
  </details>;
}

export function ModelChatDialog({ draft, withHistory = false, close, saved }: {
  draft: ModelChatDraft;
  withHistory?: boolean;
  close: () => void;
  saved: (thread: Thread) => void;
}) {
  const [includeHistory, setIncludeHistory] = useState(withHistory);
  const [version, setVersion] = useState(0);
  const [preview, setPreview] = useState<Preview>();
  const [failure, setFailure] = useState('');
  const [knowledgeText, setKnowledgeText] = useState('');
  const [knowledgeQuery, setKnowledgeQuery] = useState<string>();
  const [memoryIds, setMemoryIds] = useState<string[]>([]);
  const [mcpEvidence, setMcpEvidence] = useState<McpEvidence>();
  const [readingRepository, setReadingRepository] = useState(false);
  const editedSearch = knowledgeText.trim() !== (knowledgeQuery ?? '');
  const action = useAction();
  const endpoint = '/chat-model/' + draft.conversationId;
  const { id, text, attachments, retry, agentId, skill, autoSkill } = draft;

  useEffect(() => {
    const controller = new AbortController();
    api<Preview>(endpoint + '/preview', 'POST', { id, text, attachments, retry, agentId, skill, autoSkill, includeHistory, knowledgeQuery, memoryIds, mcpEvidence }, controller.signal)
      .then(result => {
        if (!controller.signal.aborted) setPreview(result);
      }).catch((error: Error) => {
        if (!controller.signal.aborted) setFailure(error.message);
      });
    return () => controller.abort();
  }, [endpoint, id, text, attachments, retry, agentId, skill, autoSkill, includeHistory, knowledgeQuery, memoryIds, mcpEvidence, version]);

  function refresh() {
    setPreview(undefined);
    setFailure('');
    setVersion(value => value + 1);
  }

  async function send() {
    if (!preview || editedSearch || readingRepository) return;
    const thread = await action.run(() => api<Thread>(endpoint + '/send', 'POST', {
      id, text, attachments, retry, agentId, skill, autoSkill, includeHistory, knowledgeQuery, memoryIds, mcpEvidence, confirmation: preview.confirmation,
    }));
    if (thread) saved(thread);
  }

  return <Modal title="确认聊天发送范围" onClose={close} busy={action.pending}>
    <div className="project-dialog-form">
      {withHistory && <label className="project-check"><input type="checkbox" checked={includeHistory} disabled={action.pending}
        onChange={event => {
          setPreview(undefined);
          setFailure('');
          setIncludeHistory(event.target.checked);
        }} />附带本对话最近三轮问答</label>}
      {withHistory && includeHistory && <p className="model-help">附带历史时会跳过曾引用知识库资料、仓库资料或记忆的问答。</p>}
      {draft.knowledgeEnabled && <div className="project-field">
        <label>知识库检索关键词
          <input value={knowledgeText} maxLength={120} disabled={action.pending}
            onChange={event => setKnowledgeText(event.target.value)} />
        </label>
        <div className="action-row">
          <button type="button" className="button soft" disabled={!knowledgeText.trim() || action.pending} onClick={() => {
            setKnowledgeQuery(knowledgeText.trim());
            refresh();
          }}>预览资料片段</button>
          {(knowledgeText || knowledgeQuery) && <button type="button" className="button soft" disabled={action.pending} onClick={() => {
            setKnowledgeText('');
            setKnowledgeQuery(undefined);
            refresh();
          }}>不附带资料</button>}
        </div>
      </div>}
      {draft.memoryAgentId && <MemoryPicker agent={draft.memoryAgentId} selected={memoryIds} pending={action.pending}
        changed={selection => {
          setPreview(undefined);
          setFailure('');
          setMemoryIds(selection);
        }} />}
      {draft.mcpEnabled && agentId && <McpRepositoryPicker agent={agentId} thread={draft.conversationId}
        message={id} value={mcpEvidence} pending={action.pending} busy={setReadingRepository}
        changed={value => {
          if (value === mcpEvidence) return;
          setPreview(undefined);
          setFailure('');
          setMcpEvidence(value);
        }} />}
      {!preview && !failure && <Loading text="正在读取本次发送范围" />}
      <ErrorNote message={failure} retry={refresh} />
      {preview && <>
        <div className="generation-destination">
          <strong>{preview.recipient.name} · {preview.recipient.model}</strong>
          <code>{preview.recipient.address}</code>
        </div>
        {preview.agent && <dl className="generation-inputs" aria-label="本次发送的 Agent 配置">
          <div><dt>专业 Agent</dt><dd>{preview.agent.name}</dd></div>
          <div><dt>职责</dt><dd className="evidence-text">{preview.agent.responsibility}</dd></div>
          {preview.agent.autoMemory !== undefined && <div><dt>专属记忆自动整理</dt>
            <dd>{preview.agent.autoMemory
              ? '收到回复后，向同一个模型额外发送本次问题原话一次，用于整理此 Agent 的专属记忆。调用计入额度。'
              : '模型连接已变化。请在管理中心重新确认此 Agent 的自动整理授权。'}</dd>
          </div>}
          {preview.agent.skillMode === 'automatic' && !preview.agent.skill && <div>
            <dt>Skill</dt><dd>未匹配到明确的已授权 Skill。</dd>
          </div>}
          {preview.agent.skill && <div><dt>Skill</dt><dd>
            {preview.agent.skill.name} · 第 {preview.agent.skill.position} 版
            {preview.agent.skillMode === 'automatic' && ' · 自动选用'}
            {preview.agent.skill.declaredTools && <p>声明的工具 · {preview.agent.skill.declaredTools}</p>}
            <details><summary>查看本次发送的 Skill 原文</summary>
              <pre className="skill-source">{preview.agent.skill.source}</pre></details>
          </dd></div>}
        </dl>}

        {preview.agent?.knowledge && !editedSearch && <dl className="generation-inputs" aria-label="本次发送的资料片段">
          {preview.agent.knowledge.excerpts.length === 0 && <div><dt>知识库检索</dt><dd>授权文件夹内没有匹配的正文片段。</dd></div>}
          {preview.agent.knowledge.excerpts.map(item => <div key={item.id + ':' + item.number}>
            <dt>{item.name} · {item.label}</dt><dd className="evidence-text">{item.excerpt}</dd>
          </div>)}
        </dl>}

        {preview.agent?.mcp && <div role="group" aria-label="本次发送的仓库资料">
          <McpSourceLink source={preview.agent.mcp.source} label={'查看原条目 · #' + preview.agent.mcp.number} />
          <pre className="skill-source">{preview.agent.mcp.excerpt}</pre>
          {preview.agent.mcp.truncated && <p className="model-help">片段已截断，可打开来源查看原文。</p>}
        </div>}
        {!!preview.agent?.memories?.length && <div role="group" aria-label="本次发送的记忆">
          <dl className="generation-inputs">
            {preview.agent.memories.map(item => <div key={item.id}>
              <dt>{item.scope === 'private' ? 'Agent 专属记忆' : '共享记忆'}</dt>
              <dd className="evidence-text">{item.text}</dd>
            </div>)}
          </dl>
        </div>}
        <dl className="generation-inputs" aria-label="本次发送的对话">
          <div><dt>教学风格</dt><dd>{TEACHING_STYLE_NAMES[preview.style]}</dd></div>
          <div><dt>本次问题</dt><dd className="evidence-text">{preview.question}</dd></div>
          {preview.history.map((message, index) => <div key={index}>
            <dt>{message.role === 'user' ? '之前的问题' : '之前的回复'}</dt>
            <dd className="evidence-text">{message.text}</dd>
          </div>)}
        </dl>
        <p className="model-help">附件留在本机。调用计入每日额度，临时故障最多自动重试两次。</p>
      </>}
      <ErrorNote message={action.error} />
      <div className="project-dialog-actions">
        {action.error && <button type="button" className="button secondary" disabled={action.pending} onClick={refresh}>重新预览发送范围</button>}
        <button type="button" className="button primary" disabled={!preview || editedSearch || action.pending || readingRepository} onClick={() => void send()}>
          {action.pending ? '正在保存问题' : '确认发送'}
        </button>
      </div>
    </div>
  </Modal>;
}
