import { useEffect, useState } from 'react';
import { api } from './api';
import type { Thread } from './conversation';
import { ErrorNote, Loading, Modal, useAction, useRead } from './shared';
import { TEACHING_STYLE_NAMES, type TeachingStyle } from './teaching-style';
import type { Reviewer } from './verification';

export type ModelChatDraft = {
  conversationId: string;
  id: string;
  text: string;
  attachments?: string[];
  retry?: boolean;
};

type Preview = {
  recipient: Reviewer;
  question: string;
  style: TeachingStyle;
  history: { role: 'user' | 'assistant'; text: string }[];
  confirmation: string;
};

export function useChatModel(enabled = true) {
  return useRead<{ available: boolean }>(enabled ? '/chat-model' : null);
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
  const action = useAction();
  const endpoint = '/chat-model/' + draft.conversationId;
  const { id, text, attachments, retry } = draft;

  useEffect(() => {
    const controller = new AbortController();
    api<Preview>(endpoint + '/preview', 'POST', { id, text, attachments, retry, includeHistory }, controller.signal)
      .then(result => {
        if (!controller.signal.aborted) setPreview(result);
      }).catch((error: Error) => {
        if (!controller.signal.aborted) setFailure(error.message);
      });
    return () => controller.abort();
  }, [endpoint, id, text, attachments, retry, includeHistory, version]);

  function refresh() {
    setPreview(undefined);
    setFailure('');
    setVersion(value => value + 1);
  }

  async function send() {
    if (!preview) return;
    const thread = await action.run(() => api<Thread>(endpoint + '/send', 'POST', {
      id, text, attachments, retry, includeHistory, confirmation: preview.confirmation,
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
      {!preview && !failure && <Loading text="正在读取本次发送范围" />}
      <ErrorNote message={failure} retry={refresh} />
      {preview && <>
        <div className="generation-destination">
          <strong>{preview.recipient.name} · {preview.recipient.model}</strong>
          <code>{preview.recipient.address}</code>
        </div>
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
        <button type="button" className="button primary" disabled={!preview || action.pending} onClick={() => void send()}>
          {action.pending ? '正在保存问题' : '确认发送'}
        </button>
      </div>
    </div>
  </Modal>;
}
