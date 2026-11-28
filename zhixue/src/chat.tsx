import { useEffect, useRef, useState, type ChangeEvent, type FormEvent } from 'react';
import { ArrowRight, Bookmark, BookOpen, ChevronLeft, ChevronRight, Plus, Send, Sparkles, X } from 'lucide-react';
import { Link, NavLink, useNavigate, useParams } from 'react-router-dom';
import { api, courseUrl, projectUrl } from './api';
import { AssistantScope, type AssistantSettings } from './assistant-scope';
import { ModelChatDialog, useChatModel, type ModelChatDraft } from './chat-model';
import { ChatMemoryAction } from './local-memory';
import type { AgentConfig } from './professional-agents';
import { AuthorizationDialog, type Consent } from './generation';
import { GoalAuthoring, useGoalAuthoring, type GoalChanges } from './goal-authoring';
import { FailoverRecord } from './model-backup';
import { isMoneyHold } from './money-limit';
import {
  chatUrl, GOAL_ORIGINS, questionSource, useStartConversation, type ChatHistory, type CourseAction, type CourseSelection, type Evidence,
  type FavoriteQuestion, type GoalDraft, type LessonQuestion, type Message, type ModelChatReply, type Thread,
} from './conversation';
import { CourseActionDialog } from './route-versions';
import { BackedStatus, Empty, ErrorNote, FileUpload, Markdown, useAction, useRead } from './shared';
import { TeachingStyleSelect } from './teaching-style';
import { citationUrl } from './verification';
import { McpSourceLink } from './repository-mcp';
import { VoiceInput } from './voice-input';

const LEGACY_KEY = 'zhixue-demo-conversation';
type GoalEdits = { values: Record<string, string>; removed: string[] };

function editorChanges(key: string): GoalEdits {
  try {
    const prior = JSON.parse(sessionStorage.getItem(key) ?? 'null');
    if (prior?.values && typeof prior.values === 'object' && !Array.isArray(prior.values) && Array.isArray(prior.removed)) {
      return {
        values: Object.fromEntries(Object.entries(prior.values).filter(
          (entry): entry is [string, string] => typeof entry[1] === 'string',
        )),
        removed: prior.removed.filter((value: unknown): value is string => typeof value === 'string'),
      };
    }
  } catch {
    // The persistent server draft is still available.
  }
  return { values: {}, removed: [] };
}

function keepEditorChanges(key: string, changes?: GoalEdits) {
  try {
    if (changes && (Object.keys(changes.values).length || changes.removed.length)) {
      sessionStorage.setItem(key, JSON.stringify(changes));
    } else {
      sessionStorage.removeItem(key);
    }
  } catch {
    // The editor and its explicit save remain available when browser storage is restricted.
  }
}

function LegacyChatImport({ refreshed }: { refreshed: () => void }) {
  const [attempt, setAttempt] = useState(0);
  const [failure, setFailure] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    let contents: { messages?: unknown[]; goal?: unknown } | null;
    try {
      contents = JSON.parse(sessionStorage.getItem(LEGACY_KEY) ?? 'null');
    } catch {
      return;
    }
    if (!contents || (!contents.messages?.length && !contents.goal)) return;
    api<Thread>('/conversations/legacy', 'POST', contents, controller.signal).then(() => {
      if (controller.signal.aborted) return;
      sessionStorage.removeItem(LEGACY_KEY);
      setFailure('');
      refreshed();
    }).catch(() => {
      if (!controller.signal.aborted) setFailure('旧对话导入暂未完成。');
    });
    return () => controller.abort();
  }, [attempt, refreshed]);
  return <ErrorNote message={failure} retry={() => setAttempt(value => value + 1)} />;
}

function messageDraft(key: string | undefined) {
  if (!key) return '';
  try { return localStorage.getItem(key) ?? ''; } catch { return ''; }
}

const AUTO_SKILL = ':auto';
const COURSE_ACTION_PHRASES = new Set([
  '这节我已经会了', '这节我会了', '把这个知识点提前', '把这节提前', '修改我的学习目标', '修改学习目标',
]);

function MessageComposer({ send, pending, locked = false, draftKey, intent = '', model, allowCourseActions = false }: {
  send: (text: string, id: string) => Promise<boolean>;
  pending: boolean; locked?: boolean; draftKey?: string; intent?: string;
  allowCourseActions?: boolean;
  model?: { conversationId?: string; saved: (thread: Thread) => void };
}) {
  const [input, setInput] = useState(() => messageDraft(draftKey));
  const attempt = useRef<{ key: string; id: string; conversationId?: string } | null>(null);
  const available = useChatModel(Boolean(model));
  const agents = useRead<{ items: AgentConfig[] }>(model ? '/agents' : null);
  const [agentId, setAgentId] = useState('');
  const [skill, setSkill] = useState(AUTO_SKILL);
  const agent = agents.value?.items.find(item => item.id === agentId);
  const [modelDraft, setModelDraft] = useState<ModelChatDraft>();
  const checking = Boolean(model && !available.value && !available.error);
  const courseAction = allowCourseActions && COURSE_ACTION_PHRASES.has(input.replace(/\s+/gu, '')
    .replace(/[。！.!]+$/u, '').replace(/^请/u, ''));
  const inputLocked = locked && !courseAction;
  function storeInput(text: string) {
    setInput(text);
    if (!draftKey) return;
    try {
      if (text) localStorage.setItem(draftKey, text);
      else localStorage.removeItem(draftKey);
    } catch { /* The current draft remains in memory. */ }
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    const text = input.trim();
    if (!text || pending || inputLocked || checking) return;
    const key = JSON.stringify([text, intent, agent?.id, skill]);
    if (attempt.current?.key !== key) attempt.current = { key, id: crypto.randomUUID() };
    if (model && (agent || available.value?.available)) {
      attempt.current.conversationId ??= model.conversationId ?? crypto.randomUUID();
      setModelDraft({
        conversationId: attempt.current.conversationId, id: attempt.current.id, text,
        agentId: agent?.id,
        skill: agent && skill !== AUTO_SKILL && skill ? skill : undefined,
        autoSkill: Boolean(agent?.skills.length && skill === AUTO_SKILL),
        knowledgeEnabled: Boolean(agent?.knowledgeFolders?.length),
        memoryAgentId: agent?.memoryScopes?.length ? agent.id : undefined,
        mcpEnabled: Boolean(agent?.mcpRepositories?.length),
      });
      return;
    }
    if (await send(text, attempt.current.id)) {
      storeInput('');
      attempt.current = null;
    }
  }
  return <>
    <form className="chat-composer panel" onSubmit={submit}>
      {model && !!agents.value?.items.length && <div className="agent-chat-options">
        <label className="project-field">聊天 Agent<select value={agentId} disabled={pending}
          onChange={event => { setAgentId(event.target.value); setSkill(AUTO_SKILL); }}>
          <option value="">智学助手</option>
          {agents.value.items.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select></label>
        {!!agent?.skills.length && <label className="project-field">本次使用的 Skill<select value={skill} disabled={pending}
          onChange={event => setSkill(event.target.value)}>
          <option value={AUTO_SKILL}>自动匹配</option>
          <option value="">不附带 Skill</option>
          {agent.skills.map(name => <option key={name} value={name}>{name}</option>)}
        </select></label>}
      </div>}

      <textarea aria-label="对话消息" placeholder="输入你的问题" value={input} maxLength={2000} disabled={pending}
        onChange={event => storeInput(event.target.value)} />
      <div className="chat-composer-actions">
        <VoiceInput label="对话语音输入" value={input} limit={2000} onChange={storeInput}
          disabled={pending || (locked && !allowCourseActions)} iconOnly />
        <button className="button primary" aria-label="发送消息" disabled={!input.trim() || pending || inputLocked || checking}><Send size={17} /></button>
      </div>
    </form>
    {model && !agent?.connectionId && available.value?.available === false && <Link className="text-action" to="/settings/models">配置主模型以使用模型聊天</Link>}
    {modelDraft && model && <ModelChatDialog draft={modelDraft} withHistory={Boolean(model.conversationId)}
      close={() => setModelDraft(undefined)} saved={thread => {
        setModelDraft(undefined);
        storeInput('');
        attempt.current = null;
        model.saved(thread);
      }} />}
  </>;
}

function NewThread({ created, lessonId }: { created: (thread: Thread) => void; lessonId?: string }) {
  const action = useStartConversation();
  return <>
    <Empty title={lessonId ? '开始本节答疑' : '开始新对话'} />
    <ErrorNote message={action.error} />
    <MessageComposer pending={action.pending} model={lessonId ? undefined : { saved: created }} send={async text => {
      const thread = await action.start(text, lessonId ? { lessonId } : undefined);
      if (!thread) return false;
      created(thread);
      return true;
    }} />
  </>;
}

function CourseComposer({ lesson, selection, clearSelection, pending, send }: {
  lesson: string; selection?: CourseSelection; clearSelection?: () => void; pending: boolean;
  send: (text: string, id: string, selection?: CourseSelection) => Promise<boolean>;
}) {
  const path = `/lessons/${lesson}/assistant`;
  const request = useRead<AssistantSettings>(path);
  const [scope, setScope] = useState(false);
  const [saved, setSaved] = useState<{ base: AssistantSettings | undefined; value: AssistantSettings }>();
  const action = useAction();
  const settings = saved && saved.base === request.value ? saved.value : request.value;
  function apply(value: AssistantSettings) {
    setSaved({ base: request.value, value });
    request.refresh();
  }
  return <div className="lesson-question-composer">
    {selection && <div className="lesson-selection panel">
      <div className="action-row"><strong>选段 · 第 {selection.version} 版</strong>
        <button type="button" className="icon-button" aria-label="清除选段" onClick={clearSelection}><X size={16} /></button>
      </div><blockquote>{selection.text}</blockquote>
    </div>}
    {settings ? <div className="action-row">
      <button type="button" className={settings.authorized ? 'text-action' : 'button secondary'}
        onClick={() => setScope(true)}>{settings.authorized ? '答疑范围与额度' : '启用本节课内答疑'}</button>
      {settings.authorized && <span className="model-help">已计入 {settings.calls} 次调用{settings.maxCalls !== null ? ` / 上限 ${settings.maxCalls}` : ''}</span>}
      {settings.granted && <button type="button" className="text-action" disabled={action.pending}
        onClick={() => void action.run(async () => apply(await api<AssistantSettings>(`${path}/revoke`, 'POST')))}>撤销答疑授权</button>}
    </div> : <BackedStatus error={request.error} retry={request.refresh} />}
    <ErrorNote message={action.error} />
    <MessageComposer pending={pending} locked={!settings?.authorized} draftKey={`zhixue-lesson-question-draft:${lesson}`}
      allowCourseActions={!selection}
      intent={selection ? JSON.stringify(selection) : ''} send={async (text, id) => {
        const sent = await send(text, id, selection);
        request.refresh();
        if (!sent) return false;
        clearSelection?.();
        return true;
      }} />
    {scope && settings && <AssistantScope endpoint={path} existing={settings.maxCalls} existingBackups={settings.backups} close={() => setScope(false)}
      saved={value => { apply(value); setScope(false); }} />}
  </div>;
}

function NewModelLessonThread({ lesson, selection, clearSelection, created }: {
  lesson: string; selection?: CourseSelection; clearSelection?: () => void; created: (thread: Thread) => void;
}) {
  const action = useAction();
  const identity = useRef<string | undefined>(undefined);
  return <>
    <ErrorNote message={action.error} />
    <CourseComposer lesson={lesson} selection={selection} clearSelection={clearSelection} pending={action.pending}
      send={async (text, id, selected) => {
        if (!identity.current) identity.current = crypto.randomUUID();
        const thread = await action.run(async () => {
          const starter = await api<Thread>('/conversations', 'POST', { id: identity.current, lessonId: lesson });
          return api<Thread>(`/conversations/${starter.id}/questions`, 'POST', { id, text, selection: selected });
        });
        if (!thread) return false;
        created(thread);
        return true;
      }} />
  </>;
}

function sourcePath(source: Evidence): string | null {
  if (!source.target) return null;
  if (source.kind === 'history') return chatUrl(source.target);
  if (source.kind === 'project') return projectUrl(source.target);
  if (source.kind === 'file') return `/library?file=${encodeURIComponent(source.target)}`;
  return null;
}

function GoalCard({ draft, thread, onSaved, onSubmitted }: {
  draft: GoalDraft; thread: string; onSaved: () => void; onSubmitted: (projectId: string) => void;
}) {
  const draftKey = `zhixue-goal-draft-v1:${draft.id}`;
  const [saved, setSaved] = useState<GoalDraft>();
  const [authorize, setAuthorize] = useState(false);
  const [changes, setChanges] = useState(() => editorChanges(draftKey));
  const action = useAction();
  const current = saved && saved.revision >= draft.revision ? saved : draft;
  const { values: edits, removed } = changes;
  useEffect(() => { keepEditorChanges(draftKey, changes); }, [draftKey, changes]);
  const removedSet = new Set(removed);
  const formValue = (key: string) => {
    if (Object.hasOwn(edits, key)) return edits[key];
    const field = current.fields.find(item => item.key === key);
    return field && !field.evidenceIds.some(id => removedSet.has(id)) ? field.value : '';
  };
  const dirty = Object.keys(edits).length > 0 || removed.length > 0;
  const url = `/conversations/${thread}/goal`;
  const authoring = useGoalAuthoring(`${url}/ai`);
  const editingDisabled = action.pending || authoring.active;
  const payload: GoalChanges = {
    goalId: current.id, revision: current.revision,
    values: Object.fromEntries(current.fields.filter(field => Object.hasOwn(edits, field.key)).map(field => [field.key, edits[field.key]])),
    removeEvidence: removed.filter(id => current.evidence.some(source => source.id === id)),
  };

  function updateDraft(result: GoalDraft) {
    setSaved(result);
    setChanges({ values: {}, removed: [] });
    onSaved();
  }
  async function save() {
    const result = await action.run(() => api<GoalDraft>(url, 'PUT', payload));
    if (result) updateDraft(result);
  }
  async function adopt(id: string, approved: Pick<GoalChanges, 'values' | 'removeEvidence'> | null) {
    const input = dirty || !approved ? payload : { ...payload, ...approved };
    const result = await action.run(() => api<GoalDraft>(`${url}/ai/${id}/apply`, 'POST', input));
    if (result) updateDraft(result);
    return Boolean(result);
  }
  async function toggleEvidence(id: string) {
    const result = await action.run(() => api<GoalDraft>(`${url}/ai/revoke`, 'POST', {
      goalId: current.id, revision: current.revision,
    }));
    if (result) {
      setSaved(result);
      setChanges(previous => ({ ...previous, removed: previous.removed.includes(id)
        ? previous.removed.filter(identity => identity !== id) : [...previous.removed, id] }));
      authoring.refresh();
      onSaved();
    }
  }
  async function publish(modelConsent?: Consent) {
    const result = await api<{ projectId: string }>(`${url}/submit`, 'POST', { ...payload, modelConsent });
    keepEditorChanges(draftKey);
    onSubmitted(result.projectId);
  }
  const missingGoal = !formValue('topic').trim() || !formValue('outcome').trim();
  const publishDisabled = editingDisabled || missingGoal;
  return <><form className="panel goal-form" onSubmit={event => event.preventDefault()}>
    <h2><BookOpen size={20} />学习目标</h2>
    {current.question && missingGoal && <p className="goal-question">{current.question}</p>}
    {current.fields.map(field => {
      function changeValue(value: string) {
        setChanges(previous => ({ ...previous, values: { ...previous.values, [field.key]: value } }));
      }
      const props = {
        'aria-label': field.label,
        value: formValue(field.key),
        disabled: editingDisabled,
        required: field.key === 'topic' || field.key === 'outcome',
        maxLength: field.limit,
        onChange: (event: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => changeValue(event.target.value),
      };
      return <div className="goal-input" key={field.key}>
        <label>
          <span className="goal-field-title">{field.label}<small>{GOAL_ORIGINS[Object.hasOwn(edits, field.key) ? 'edited' : field.origin]}</small></span>
          {field.key === 'outcome' ? <textarea {...props} placeholder="完成后，你希望自己能做什么？" />
            : <input {...props} type={field.type} />}
        </label>
        {field.type === 'text' && <VoiceInput label={`语音改写 ${field.label}`} value={props.value} limit={field.limit}
          onChange={changeValue} disabled={editingDisabled} replace iconOnly />}
      </div>;
    })}
    {current.evidence.length > 0 && <details className="goal-evidence">
      <summary>学习依据（{current.evidence.length - payload.removeEvidence.length}）</summary>
      {current.evidence.map(source => {
        const path = sourcePath(source);
        const excluded = removedSet.has(source.id);
        return <div key={source.id} className="goal-source">
          <div>
            {path ? <Link to={path}>{source.label}</Link> : <strong>{source.label}</strong>}
            {source.excerpt && <p>{source.excerpt}</p>}
          </div>
          <button type="button" className="text-action" aria-label={`${excluded ? '恢复依据' : '移除依据'} ${source.label}`}
            disabled={action.pending} onClick={() => void toggleEvidence(source.id)}>
            {excluded ? '撤销移除' : '移除'}
          </button>
        </div>;
      })}
    </details>}
    <GoalAuthoring authoring={authoring} changes={payload} evidence={current.evidence} busy={action.pending} adopt={adopt} />
    <ErrorNote message={action.error} retry={onSaved} />
    <div className="action-row">
      <button type="button" className="button secondary" disabled={editingDisabled || !dirty} onClick={() => void save()}>保存草稿</button>
      <button type="button" className="button primary" disabled={publishDisabled} onClick={() => setAuthorize(true)}>
        用主模型生成路线 <ArrowRight size={17} />
      </button>
      <button type="button" className="button secondary" disabled={publishDisabled}
        onClick={() => void action.run(() => publish())}>提交并生成路线（本地模板）</button>
    </div>
    {dirty && <p className="model-help">目标卡有待保存的改动。</p>}
    {saved && !dirty && <p className="model-saved" role="status">目标草稿已保存</p>}
  </form>
    {authorize && <AuthorizationDialog previewPath={`${url}/model-preview`} payload={payload} confirmationLabel="确认并生成路线"
      close={() => setAuthorize(false)} confirm={publish} />}
  </>;
}

function ChatMessage({ message, pending, onFavorite, onRetry, onModelRetry }: {
  message: Message; pending: boolean; onFavorite: (question: LessonQuestion) => void; onRetry: (questionId: string) => void;
  onModelRetry: (reply: ModelChatReply) => void;
}) {
  const question = message.question;
  const answer = message.answer;
  const modelReply = message.modelReply;
  return <div className={`message ${message.role}`} id={`message-${message.id}`}>
    {message.role === 'demo' && <span className="message-label"><Sparkles size={15} />智学 · 本地演示</span>}
    {answer && <span className="message-label"><Sparkles size={15} />课内助手{answer.recipient && ` · ${answer.recipient.name}`}</span>}
    {question?.selection && <blockquote className="question-excerpt">{question.selection.text}</blockquote>}
    {modelReply && <span className="message-label"><Sparkles size={15} />{modelReply.agent?.name ?? '智学'} · {modelReply.recipient.name}</span>}
    {modelReply?.agent?.skill && <p className="model-help">Skill {modelReply.agent.skill.name} · 第 {modelReply.agent.skill.position} 版
      {modelReply.agent.skillMode === 'automatic' && ' · 自动选用'}</p>}
    {!!modelReply?.agent?.knowledge?.excerpts.length && <details className="model-help">
      <summary>资料片段 · {modelReply.agent.knowledge.excerpts.length} 条</summary>
      <ul>{modelReply.agent.knowledge.excerpts.map(item => <li key={item.id + ':' + item.number}>
        <strong>{item.name} · {item.label}</strong><p>{item.excerpt}</p>
      </li>)}</ul>
    </details>}
    {!!modelReply?.agent?.memories?.length && <p className="model-help">已确认记忆 · {modelReply.agent.memories.length} 条</p>}
    {modelReply?.agent?.mcp && <details className="model-help">
      <summary>仓库资料 · {modelReply.agent.mcp.repository}</summary>
      <McpSourceLink source={modelReply.agent.mcp.source} label="查看原条目" />
      <pre className="skill-source">{modelReply.agent.mcp.excerpt}</pre>
    </details>}
    {message.text && (modelReply ? <Markdown>{message.text}</Markdown> : <p className={answer ? 'evidence-text' : undefined}>{message.text}</p>)}
    {message.role === 'user' && message.text.trim() && <ChatMemoryAction original={message} />}
    {modelReply?.state === 'queued' && <p className="model-help" role="status">{modelReply.agent ? '问题已保存，等待所选模型。' : '问题已保存，等待主模型。'}</p>}
    {modelReply?.state === 'running' && <p className="model-help" role="status">{modelReply.agent ? '所选模型正在回复。' : '主模型正在回复。'}</p>}
    {modelReply?.state === 'blocked' && <ErrorNote message={modelReply.error ?? '暂时无法完成模型回复。'}
      retry={() => onModelRetry(modelReply)} />}
    {(modelReply?.errorCode === 'daily_budget' || isMoneyHold(modelReply?.errorCode)) &&
      <Link className="text-action" to="/settings/budget">查看额度恢复</Link>}
    {question && <div className="action-row message-actions">
      <Link className="text-action" to={questionSource(question)}>{question.selection ? '定位到课程选段' : `查看课程第 ${question.version} 版`}</Link>
      <button type="button" className="text-action" aria-pressed={question.favorite} disabled={pending} onClick={() => onFavorite(question)}>
        <Bookmark size={14} />{question.favorite ? '已收藏疑问' : '收藏疑问'}
      </button>
    </div>}
    <FailoverRecord items={answer?.failovers} />
    {answer?.state === 'queued' && <p className="model-help" role="status">问题已保存，等待模型答复。</p>}
    {answer?.state === 'running' && <p className="model-help" role="status">正在结合课程依据答疑。</p>}
    {answer?.state === 'paused' && <p className="model-help" role="status">项目已暂停，恢复后继续答疑。</p>}
    {answer?.state === 'blocked' && <ErrorNote message={answer.error ?? '本次答疑暂未完成。'}
      retry={pending ? undefined : () => onRetry(answer.questionId)} />}
    {answer?.state === 'answered' && <>
      <p className="model-help">{answer.support === 'grounded' ? '引文已定位'
        : answer.support === 'insufficient' ? '可用依据有限' : '引用未能完整定位，答复待核验'}</p>
      {answer.citations.length > 0 && <ul className="answer-citations">
        {answer.citations.map((entry, index) => <li key={index}><Link to={citationUrl(entry)} title={entry.quote}>
          {entry.title} · {entry.label}
        </Link></li>)}
      </ul>}
    </>}
  </div>;
}

function ThreadPane({ id, refreshed, projectCreated, assistant = false, selection, clearSelection, focus, visible = true,
  showLearnedControl, onRouteChanged }: {
  id: string; refreshed?: () => void; projectCreated?: (projectId: string) => void; assistant?: boolean;
  selection?: CourseSelection; clearSelection?: () => void; focus?: FavoriteQuestion; visible?: boolean;
  showLearnedControl?: () => void;
  onRouteChanged: () => void;
}) {
  const endpoint = `/conversations/${id}`;
  const read = useRead<Thread>(endpoint);
  const [optimistic, setOptimistic] = useState<{ previous: Thread | undefined; next: Thread }>();
  const [past, setPast] = useState<{ messages: Message[]; before: number | null }>();
  const action = useAction();
  const attach = useAction();
  const messageAction = useAction();
  const [pendingFile, setPendingFile] = useState<string>();
  const [retryReply, setRetryReply] = useState<ModelChatReply>();
  const [courseAction, setCourseAction] = useState<CourseAction>();
  const thread = optimistic && optimistic.previous === read.value ? optimistic.next : read.value;
  const previous = past?.messages ?? [];
  const active = thread?.messages.some(item => [item.answer?.state, item.modelReply?.state]
    .some(state => state === 'queued' || state === 'running'));
  const paused = thread?.messages.some(item => item.answer?.state === 'paused'
    || item.modelReply?.errorCode === 'daily_budget' || isMoneyHold(item.modelReply?.errorCode));
  const interval = active ? 800 : paused ? 3000 : 0;
  const focusId = focus?.id;
  const needsPage = focus && thread && ![...previous, ...thread.messages].some(message => message.id === focusId);
  const archive = useRead<Thread>(needsPage ? `${endpoint}?before=${focus.position + 2}` : null);
  const focusedPair = archive.value?.messages.filter(message => message.id === focusId || message.answer?.questionId === focusId) ?? [];
  const located = !!focusId && [...previous, ...focusedPair, ...thread?.messages ?? []].some(message => message.id === focusId);
  const refresh = read.refresh;
  useEffect(() => {
    if (!visible || !interval) return;
    const timer = window.setInterval(refresh, interval);
    return () => window.clearInterval(timer);
  }, [interval, refresh, visible]);
  useEffect(() => {
    if (visible && located) document.getElementById(`message-${focusId}`)?.scrollIntoView({ block: 'nearest' });
  }, [focusId, located, visible]);
  if (!thread) return <BackedStatus error={read.error} retry={read.refresh} />;
  const before = past ? past.before : thread.olderBefore;
  const all = [...new Map([...previous, ...focusedPair, ...thread.messages].map(message => [message.id, message])).values()]
    .sort((a, b) => a.position - b.position);
  const canMakeGoal = !thread.goal && all.some(message => message.role === 'user' && !message.courseCommand);
  const last = thread.messages.at(-1);
  const canMarkLearned = showLearnedControl && (last?.answer?.state === 'answered' || last?.role === 'demo');

  function update(next?: Thread) {
    if (next) setOptimistic({ previous: read.value, next });
    read.refresh();
    refreshed?.();
  }
  const model = thread.assistantMode === 'model';
  function retainTranscript(next: Thread) {
    if (past && thread?.messages.length) setPast({ ...past, messages: [...past.messages, ...thread.messages] });
    update(next);
  }
  async function send(text: string, identity: string, selected?: CourseSelection) {
    const next = await action.run(() => api<Thread>(`${endpoint}/${model ? 'questions' : 'messages'}`, 'POST', {
      id: identity, text, ...(model && selected ? { selection: selected } : {}),
    }));
    if (!next) return false;
    retainTranscript(next);
    if (next.courseAction) setCourseAction(next.courseAction);
    return true;
  }
  async function earlier() {
    const next = await action.run(() => api<Thread>(`${endpoint}?before=${before}`));
    if (next) setPast({ messages: [...next.messages, ...previous], before: next.olderBefore });
  }
  function attachFile(file: string) {
    setPendingFile(file);
    void attach.run(async () => {
      await api<Thread>(`${endpoint}/attachments/${file}`, 'PUT');
      setPendingFile(undefined);
      update();
    });
  }
  function favorite(question: LessonQuestion) {
    void messageAction.run(async () => {
      const saved = await api<LessonQuestion>(`/lesson-questions/${question.id}/favorite`, 'PUT', { favorite: !question.favorite });
      const apply = (message: Message) => message.id === saved.id ? { ...message, question: saved } : message;
      setPast(current => current && { ...current, messages: current.messages.map(apply) });
      archive.refresh();
      if (thread) update({ ...thread, messages: thread.messages.map(apply) });
    });
  }
  function retry(questionId: string) {
    void messageAction.run(async () => update(await api<Thread>(`/lesson-questions/${questionId}/retry`, 'POST')));
  }
  return <div className={assistant ? 'thread-pane assistant-thread' : 'thread-pane'}>
    {!assistant && <div className="thread-head">
      <h2>{thread.title}</h2>
      {thread.lessonId && <Link className="text-action" to={courseUrl(thread.lessonId)}>返回关联课程</Link>}
    </div>}
    <ErrorNote message={read.error} retry={read.refresh} />
    <ErrorNote message={archive.error} retry={archive.refresh} />
    {before !== null && <button type="button" className="text-action" disabled={action.pending} onClick={() => void earlier()}>查看更早消息</button>}
    <div className="conversation">
      {all.map(message => <ChatMessage key={message.id} message={message} pending={messageAction.pending}
        onFavorite={favorite} onRetry={retry} onModelRetry={setRetryReply} />)}
      {!assistant && thread.projects.length > 0 && <div className="intake-attachments">
        {thread.projects.map(project => <Link className="chip" key={project.id} to={projectUrl(project.id)}><BookOpen size={15} />{project.topic}</Link>)}
      </div>}
      {!assistant && canMakeGoal && <button type="button" className="button soft" disabled={action.pending} onClick={() => {
        void action.run(async () => { await api(`${endpoint}/goal`, 'POST'); update(); });
      }}><BookOpen size={17} />整理为学习目标</button>}
      {!assistant && thread.goal && projectCreated && <GoalCard key={thread.goal.id} thread={id} draft={thread.goal}
        onSaved={() => update()} onSubmitted={projectCreated} />}
    </div>
    {!assistant && <div className="intake-attachments attached-files">
      <FileUpload label="添加资料" disabled={attach.pending} onImport={attachFile} />
      {thread.attachments.map(file => <Link className="chip" key={file.id} to={`/library?file=${encodeURIComponent(file.id)}`}>{file.name}</Link>)}
    </div>}
    <ErrorNote message={attach.error} retry={pendingFile && !attach.pending ? () => attachFile(pendingFile) : undefined} />
    <ErrorNote message={action.error} />
    <ErrorNote message={messageAction.error} />
    {model && thread.lessonId ? <CourseComposer lesson={thread.lessonId} selection={selection} clearSelection={clearSelection}
      pending={action.pending} send={send} /> : <MessageComposer pending={action.pending} send={send}
        model={thread.lessonId ? undefined : { conversationId: id, saved: retainTranscript }}
        draftKey={thread.lessonId ? undefined : 'zhixue-chat-draft:' + id} />}
    {retryReply && <ModelChatDialog draft={{
      conversationId: id, id: retryReply.questionId, text: retryReply.questionText, retry: true, memoryAgentId: retryReply.agent?.id,
    }}
      withHistory={retryReply.hasHistory} close={() => setRetryReply(undefined)}
      saved={next => { setRetryReply(undefined); retainTranscript(next); }} />}
    {canMarkLearned && <button type="button" className="text-action assistant-history-link learned-reminder" onClick={showLearnedControl}>
      理解后可以勾选「已学完」<ArrowRight size={15} />
    </button>}
    {assistant && <Link className="text-action assistant-history-link" to={chatUrl(thread.id)}>在聊天中打开</Link>}
    {courseAction && visible && <CourseActionDialog request={courseAction}
      onClose={() => setCourseAction(undefined)} onChanged={onRouteChanged} />}
  </div>;
}

export function ChatPage({ onProjectsChange }: { onProjectsChange: () => void }) {
  const { conversationId } = useParams();
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState('');
  const navigate = useNavigate();
  const history = useRead<ChatHistory>(`/conversations?page=${page}&q=${encodeURIComponent(search)}`);
  function created(thread: Thread) {
    history.refresh();
    navigate(chatUrl(thread.id));
  }
  return <section className="page chat-page">
    <div className="page-head"><h1>聊天</h1>
      <div className="action-row">
        <TeachingStyleSelect />
        <button type="button" className="button secondary" onClick={() => navigate('/chat')}><Plus size={17} />新对话</button>
      </div>
    </div>
    <LegacyChatImport refreshed={history.refresh} />
    <div className="chat-workspace">
      <nav className="panel chat-history" aria-label="历史对话">
        <h2>历史对话</h2>
        <input type="search" aria-label="搜索对话标题" placeholder="搜索对话标题" maxLength={120} value={search}
          onChange={event => { setSearch(event.target.value); setPage(1); }} />
        {!history.value ? <BackedStatus error={history.error} retry={history.refresh} /> : <>
          <ErrorNote message={history.error} retry={history.refresh} />
          {history.value.items.map(item => <NavLink key={item.id} to={chatUrl(item.id)} aria-label={item.title}>
            <strong>{item.title}</strong>
            <time dateTime={item.updatedAt}>{new Date(item.updatedAt).toLocaleDateString('zh-CN')}</time>
          </NavLink>)}
          {!history.value.items.length && <p className="model-help">{search ? '暂无匹配的对话。' : '已发送的对话会显示在这里。'}</p>}
          {history.value.lastPage > 1 && <div className="chat-pages">
            <button type="button" className="icon-button" aria-label="对话上一页" disabled={history.value.page === 1}
              onClick={() => setPage(history.value!.page - 1)}><ChevronLeft size={17} /></button>
            <span>{history.value.page} / {history.value.lastPage}</span>
            <button type="button" className="icon-button" aria-label="对话下一页" disabled={history.value.page === history.value.lastPage}
              onClick={() => setPage(history.value!.page + 1)}><ChevronRight size={17} /></button>
          </div>}
        </>}
      </nav>
      {conversationId ? <ThreadPane key={conversationId} id={conversationId} refreshed={history.refresh}
        onRouteChanged={onProjectsChange} projectCreated={projectId => {
        onProjectsChange();
        navigate(projectUrl(projectId));
      }} /> : <div className="thread-pane"><NewThread created={created} /></div>}
    </div>
  </section>;
}

export function LessonAssistant({ lessonId, model, selection, clearSelection, close, visible, showLearnedControl, onRouteChanged }: {
  lessonId: string; model: boolean; selection?: CourseSelection; clearSelection: () => void; close: () => void; visible: boolean;
  showLearnedControl?: () => void;
  onRouteChanged: () => void;
}) {
  const recent = useRead<ChatHistory>(`/conversations?lessonId=${lessonId}`);
  const favorites = useRead<{ items: FavoriteQuestion[] }>(model ? `/lessons/${lessonId}/assistant/favorites` : null);
  const [created, setCreated] = useState<string>();
  const [focus, setFocus] = useState<FavoriteQuestion>();
  const [courseAction, setCourseAction] = useState<CourseAction>();
  const thread = created ?? recent.value?.items[0]?.id;
  function openThread(saved: Thread) {
    setCreated(saved.id);
    if (saved.courseAction) setCourseAction(saved.courseAction);
  }
  return <aside className="panel lesson-assistant" aria-label="课内助手">
    <div className="drawer-head"><h2>课内助手</h2><button type="button" className="icon-button" aria-label="关闭课内助手" onClick={close}><X size={19} /></button></div>
    <TeachingStyleSelect />
    {!!favorites.value?.items.length && <details className="question-favorites">
      <summary>已收藏疑问 · {favorites.value.items.length}</summary>
      <ul>{favorites.value.items.map(item => <li key={item.id}>
        <button type="button" className="text-action" onClick={() => { setCreated(item.conversationId); setFocus(item); }}>{item.text}</button>
        <Link className="text-action" to={questionSource(item)}>回到原文</Link>
      </li>)}</ul>
    </details>}
    {thread ? <ThreadPane key={thread} id={thread} assistant selection={selection} clearSelection={clearSelection}
      focus={focus} refreshed={favorites.refresh} visible={visible} showLearnedControl={showLearnedControl}
      onRouteChanged={onRouteChanged} />
      : recent.value ? model ? <NewModelLessonThread lesson={lessonId} selection={selection} clearSelection={clearSelection} created={openThread} />
        : <NewThread lessonId={lessonId} created={openThread} />
      : <BackedStatus error={recent.error} retry={recent.refresh} />}
    {courseAction && visible && <CourseActionDialog request={courseAction}
      onClose={() => setCourseAction(undefined)} onChanged={onRouteChanged} />}
  </aside>;
}
