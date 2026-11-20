import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from './api';
import { ErrorNote, Loading, Markdown, Modal, useAction, useRead } from './shared';

export type NoteAISelection = { markdown: string; replace: (markdown: string) => string };

const actions = {
  draft: '生成草稿', explain: '解释', expand: '扩写', shorten: '精简', organize: '整理', example: '补例',
} as const;

type Operation = keyof typeof actions;
type Recipient = { name: string; address: string; model: string };
type Request = { id: string; operation: Operation; text: string; lessonId?: string };
type Preview = {
  recipient: Recipient; confirmation: string; text: string; maxCalls: number;
  course: { title: string; version: number | null; sections: { title: string; text: string }[]; truncated: boolean } | null;
};

export type NoteAIJob = {
  id: string; operation: Operation; state: 'queued' | 'running' | 'ready' | 'failed' | 'dismissed' | 'blocked';
  recipient: Recipient; markdown: string | null; error: string | null;
  usage: { inputTokens?: number; outputTokens?: number } | null;
};

const maxSelection = 8_000;
const maxSuggested = 16_000;

export function NoteAIDialog({ endpoint, lessonId, selected, existing, onClose, onApply }: {
  endpoint: string; lessonId?: string; selected?: NoteAISelection; existing?: NoteAIJob;
  onClose: () => void; onApply: (markdown: string, replacement: boolean, signal: AbortSignal) => Promise<void>;
}) {
  const [operation, setOperation] = useState<Operation>(selected ? 'explain' : 'draft');
  const [instruction, setInstruction] = useState('');
  const [includeCourse, setIncludeCourse] = useState(Boolean(lessonId));
  const [scope, setScope] = useState<{ request: Request; preview: Preview }>();
  const [submitted, setSubmitted] = useState(existing);
  const [edited, setEdited] = useState<string>();
  const [applied, setApplied] = useState(false);
  const action = useAction();
  const applying = useRef<AbortController | null>(null);
  const jobPath = submitted ? endpoint + '/' + submitted.id : null;
  const read = useRead<NoteAIJob>(jobPath);
  const job = read.value ?? submitted;
  const waiting = job?.state === 'queued' || job?.state === 'running';
  const budgetHold = job?.state === 'blocked';

  useEffect(() => () => applying.current?.abort(), []);
  useEffect(() => {
    if ((!waiting && !budgetHold) || !read.value || read.error) return;
    const timer = window.setTimeout(read.refresh, waiting ? 480 : 3000);
    return () => window.clearTimeout(timer);
  }, [waiting, budgetHold, read.value, read.error, read.refresh]);

  function preview() {
    const request = {
      id: crypto.randomUUID(), operation,
      text: selected?.markdown ?? instruction,
      ...(includeCourse && !selected && lessonId ? { lessonId } : {}),
    };
    void action.run(async () => setScope({
      request, preview: await api<Preview>(endpoint + '/preview', 'POST', request),
    }));
  }

  function send() {
    if (!scope) return;
    void action.run(async () => setSubmitted(await api<NoteAIJob>(endpoint, 'POST', {
      ...scope.request, confirmation: scope.preview.confirmation,
    })));
  }

  function dismiss() {
    if (!jobPath) return;
    void action.run(async () => {
      await api(jobPath, 'DELETE');
      onClose();
    });
  }

  function accept(replace: boolean) {
    const markdown = edited ?? job?.markdown;
    if (!jobPath || !markdown?.trim() || applied) return;
    void action.run(async () => {
      const controller = new AbortController();
      applying.current = controller;
      try {
        await onApply(markdown, replace, controller.signal);
      } finally {
        applying.current = null;
      }
      if (controller.signal.aborted) return;
      setApplied(true);
      await api(jobPath, 'DELETE');
      onClose();
    });
  }

  const previewed = scope?.preview;
  const proposal = edited ?? job?.markdown ?? '';
  const tooLong = selected && [...selected.markdown].length > maxSelection;
  const title = job && job.state === 'ready' ? '预览 AI 笔记建议'
    : (job?.operation ?? operation) === 'draft' ? 'AI 笔记草稿' : 'AI 选段处理';

  return <Modal title={title} onClose={onClose} busy={action.pending}>
    <div className="project-dialog-form note-ai-form">
      {!job && !previewed && <>
        {selected ? <>
          <label className="project-field">处理方式<select value={operation} disabled={action.pending}
            onChange={event => setOperation(event.target.value as Operation)}>
            {Object.entries(actions).filter(([key]) => key !== 'draft').map(([key, name]) => <option key={key} value={key}>{name}</option>)}
          </select></label>
          <label className="project-field">选段原文<pre aria-label="选段原文">{selected.markdown}</pre></label>
          {tooLong && <p className="file-status">选段最多 8,000 字。可以返回笔记缩小选择范围。</p>}
        </> : <>
          <label className="project-field">草稿要求<textarea maxLength={2000} rows={4} value={instruction}
            placeholder="例如，整理索引的工作原理，附一个查询示例。" disabled={action.pending}
            onChange={event => setInstruction(event.target.value)} /></label>
          {lessonId && <label className="note-ai-check">
            <input type="checkbox" checked={includeCourse} disabled={action.pending}
              onChange={event => setIncludeCourse(event.target.checked)} />使用本节当前讲稿
          </label>}
        </>}
        <ErrorNote message={action.error} />
        {action.error && <Link to="/settings/models" className="text-action">打开 API 与模型设置</Link>}
        <div className="action-row">
          <button type="button" className="button primary" disabled={action.pending || !!tooLong || (!selected && !includeCourse && !instruction.trim())}
            onClick={preview}>查看发送范围</button>
          <button type="button" className="button secondary" disabled={action.pending} onClick={onClose}>取消</button>
        </div>
      </>}
      {!job && previewed && <>
        <div className="note-ai-recipient"><strong>{previewed.recipient.name} · {previewed.recipient.model}</strong>
          <small>{previewed.recipient.address}</small>
          <small>{actions[operation]} · 最多 {previewed.maxCalls} 次模型调用</small>
        </div>
        {previewed.text && <section><h3>{selected ? '将发送的选段' : '将发送的草稿要求'}</h3><pre>{previewed.text}</pre></section>}
        {previewed.course && <details open>
          <summary>将发送的讲稿 · {previewed.course.title}{previewed.course.version ? ' · 第 ' + previewed.course.version + ' 版' : ' · 本地讲稿'}</summary>
          {previewed.course.truncated && <p className="file-status">讲稿超出本次范围，发送下列已截取内容。</p>}
          {previewed.course.sections.map((section, index) => <section key={index}><h3>{section.title}</h3><pre>{section.text}</pre></section>)}
        </details>}
        <ErrorNote message={action.error} />
        <div className="action-row">
          <button type="button" className="button primary" disabled={action.pending} onClick={send}>发送给主模型</button>
          <button type="button" className="button secondary" disabled={action.pending} onClick={() => setScope(undefined)}>返回修改范围</button>
        </div>
      </>}
      {job && <>
        <div className="note-ai-recipient">
          <strong>{job.recipient.name} · {job.recipient.model}</strong>
          <small>{actions[job.operation]}
            {job.usage?.inputTokens !== undefined && ' · 输入 ' + job.usage.inputTokens + ' tokens'}
            {job.usage?.outputTokens !== undefined && ' · 输出 ' + job.usage.outputTokens + ' tokens'}
          </small>
        </div>
        {waiting && <Loading text={job.state === 'queued' ? '正在等待模型调用' : '主模型正在生成建议'} />}
        <ErrorNote message={job.error ?? read.error} retry={read.error ? read.refresh : undefined} />
        {budgetHold && <Link className="text-action" to="/settings/budget">查看额度恢复</Link>}
        {job.state === 'ready' && <>
          {applied ? <p className="saved" role="status">建议已加入笔记。</p> : <>
            {selected && <details><summary>对照所选原文</summary><pre>{selected.markdown}</pre></details>}
            <div className="note-ai-preview" aria-label="建议预览"><Markdown>{proposal}</Markdown></div>
            <details><summary>修改建议原文</summary>
              <textarea aria-label="建议 Markdown" rows={6} maxLength={maxSuggested} value={proposal} disabled={action.pending}
                onChange={event => setEdited(event.target.value)} />
            </details>
          </>}
        </>}
        <ErrorNote message={action.error} />
        <div className="action-row">
          {job.state === 'ready' && !applied && <>
            <button type="button" className="button primary" disabled={action.pending || !proposal.trim()}
              onClick={() => accept(false)}>确认追加到笔记</button>
            {selected && <button type="button" className="button secondary" disabled={action.pending || !proposal.trim()}
              onClick={() => accept(true)}>确认替换选段</button>}
          </>}
          {job.state !== 'dismissed' && <button type="button" className="button secondary" disabled={action.pending} onClick={dismiss}>
            {waiting ? '取消本次建议' : applied ? '关闭已采用建议' : '舍弃建议'}
          </button>}
          <button type="button" className="text-action" disabled={action.pending} onClick={onClose}>返回笔记</button>
        </div>
      </>}
    </div>
  </Modal>;
}
