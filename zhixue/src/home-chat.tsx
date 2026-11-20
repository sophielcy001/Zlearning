import { useRef, useState, type FormEvent } from 'react';
import { ArrowRight, BookOpen, CalendarDays, Sparkles, X } from 'lucide-react';
import { Link, useNavigate } from 'react-router-dom';
import { courseUrl, projectUrl, type DemoState } from './api';
import { ModelChatDialog, useChatModel, type ModelChatDraft } from './chat-model';
import { chatUrl, EXAMPLE_MESSAGE, useStartConversation, type Attachment } from './conversation';
import { ErrorNote, FileUpload } from './shared';
import { VoiceInput } from './voice-input';

export function Home({ state }: { state: DemoState }) {
  const [input, setInput] = useState('');
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [attaching, setAttaching] = useState(false);
  const start = useStartConversation();
  const model = useChatModel();
  const [modelDraft, setModelDraft] = useState<ModelChatDraft>();
  const attempted = useRef<{ fingerprint: string; draft: ModelChatDraft } | undefined>(undefined);
  const navigate = useNavigate();
  const active = state.continuation;
  const next = active?.nextLesson;
  const due = state.dueCount;
  async function explore(text: string, sample = false) {
    const thread = await start.start(text, sample ? { sample } : { attachments: attachments.map(item => item.id) });
    if (thread) navigate(chatUrl(thread.id));
  }
  function beginModelChat() {
    const text = input.trim();
    if (!text || attaching || start.pending) return;
    const files = attachments.map(item => item.id);
    const fingerprint = JSON.stringify([text, files]);
    if (attempted.current?.fingerprint !== fingerprint) {
      attempted.current = { fingerprint, draft: {
        conversationId: crypto.randomUUID(), id: crypto.randomUUID(), text, attachments: files,
      } };
    }
    setModelDraft(attempted.current.draft);
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    if (input.trim() && !attaching) void explore(input.trim());
  }
  return <section className="home">
    <div className="hero">
      <h1>今天，想弄懂什么？</h1>
      <p>提出学习问题，或继续上次的课程。</p>
    </div>
    <form className="composer" onSubmit={submit}>
      <textarea aria-label="学习问题或目标" value={input} maxLength={2000} disabled={start.pending} onChange={event => setInput(event.target.value)} placeholder="例如，我想系统学习 SQLite" />
      {attachments.length > 0 && <div className="intake-attachments">
        {attachments.map(file => <button type="button" key={file.id} className="chip" disabled={start.pending}
          aria-label={`移除附件 ${file.name}`} onClick={() => setAttachments(items => items.filter(item => item.id !== file.id))}>{file.name}<X size={14} /></button>)}
      </div>}
      <div className="composer-actions">
        <div className="action-row">
          <VoiceInput label="学习问题语音输入" value={input} limit={2000} onChange={setInput} disabled={start.pending} />
          <FileUpload label="上传资料" disabled={start.pending} onPending={setAttaching} onImport={(id, name) => {
            setAttachments(items => items.some(item => item.id === id) ? items : [...items, { id, name }]);
          }} />
        </div>
        <div className="action-row">
          {model.value?.available && <button type="button" className="button secondary"
            disabled={!input.trim() || start.pending || attaching} onClick={beginModelChat}>用主模型聊天</button>}
          <button className="button primary" disabled={!input.trim() || start.pending || attaching}>开始探索 <ArrowRight size={17} /></button>
        </div>
      </div>
      <ErrorNote message={start.error} />
    </form>
    {modelDraft && <ModelChatDialog draft={modelDraft} close={() => setModelDraft(undefined)}
      saved={thread => navigate(chatUrl(thread.id))} />}
    <div className="example-row">
      <button className="chip sample-button" disabled={start.pending} onClick={() => void explore(EXAMPLE_MESSAGE, true)}><Sparkles size={15} />体验 SQLite 样例 <ArrowRight size={14} /></button>
    </div>
    <div className="home-links">
      <div className="home-row">
        <div className="row-title"><CalendarDays size={18} /><strong>今日任务</strong></div>
        <span>查看按学习时段安排的新课和复习。</span>
        <Link to="/study-plan">查看课程表 <ArrowRight size={16} /></Link>
      </div>
      <div className="home-row">
        <div className="row-title"><BookOpen size={18} /><strong>继续学习</strong></div>
        <span>{active ? `${active.topic} · ${next?.title ?? '学习路线'} · ${active.lessonCount ? `已学 ${active.learned} / ${active.lessonCount} 节` : '尚无进度'}` : '创建学习目标后，可以在这里继续。'}</span>
        <Link to={active ? next?.ready ? courseUrl(next.id) : projectUrl(active.id) : '/projects'}>{active ? '继续学习' : '查看项目'} <ArrowRight size={16} /></Link>
      </div>
      <div className="home-row">
        <div className="row-title"><CalendarDays size={18} /><strong>今日复习</strong></div>
        <span>{due ? `${due} 个知识目标待复习` : '今天没有到期复习'}</span>
        <Link to="/reviews">{due ? '开始复习' : '查看安排'} <ArrowRight size={16} /></Link>
      </div>
    </div>
  </section>;
}

