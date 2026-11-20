import { useEffect, useRef, useState } from 'react';
import { Mic, Square } from 'lucide-react';
import { apiResponse } from './api';
import { ExternalRecognition } from './recognition';
import { ErrorNote, Loading, Modal, useRead } from './shared';
import { recordVoice } from './voice-recording';

type Language = 'zh' | 'en';
type Phase = 'idle' | 'opening' | 'recording' | 'recognizing' | 'review';

function recordingError(error: unknown) {
  if (error instanceof DOMException) {
    if (error.name === 'NotAllowedError' || error.name === 'SecurityError') return '麦克风权限不可用，可在系统设置允许后重试。';
    if (error.name === 'NotFoundError') return '未找到可用麦克风。可接入设备或继续键入。';
    if (error.name === 'NotReadableError') return '麦克风正忙或暂时无法打开。';
  }
  return error instanceof Error ? error.message : '录音暂时失败，可继续键入。';
}

function VoiceDraft({ space, replacing, apply, close }: {
  space: number; replacing: boolean; apply: (text: string) => void; close: () => void;
}) {
  const service = useRead<{ available: boolean; maxSeconds: number }>('/voice');
  const [language, setLanguage] = useState<Language>('zh');
  const [phase, setPhase] = useState<Phase>('idle');
  const [draft, setDraft] = useState('');
  const [error, setError] = useState('');
  const [clip, setClip] = useState<Blob>();
  const [externalPending, setExternalPending] = useState(false);
  const active = useRef<AbortController | null>(null);
  const stopRecording = useRef<(() => void) | null>(null);
  const closeDialog = useRef(close);
  closeDialog.current = close;
  const supported = typeof AudioContext !== 'undefined' && typeof AudioWorkletNode !== 'undefined'
    && typeof OfflineAudioContext !== 'undefined' && !!navigator.mediaDevices?.getUserMedia;
  const text = draft.trim();
  const tooLong = text.length > space;

  useEffect(() => {
    function cancelActiveRecording() {
      const request = active.current;
      if (!request) return;
      active.current = null;
      request.abort();
      closeDialog.current();
    }
    function hide() {
      if (document.hidden) cancelActiveRecording();
    }
    document.addEventListener('visibilitychange', hide);
    window.addEventListener('zhixue:window-hidden', cancelActiveRecording);
    const unsubscribe = window.zhixueDesktop?.onWindowHidden(cancelActiveRecording);
    return () => {
      document.removeEventListener('visibilitychange', hide);
      window.removeEventListener('zhixue:window-hidden', cancelActiveRecording);
      unsubscribe?.();
      active.current?.abort();
    };
  }, []);

  async function start() {
    if (active.current || !service.value || !supported) return;
    const request = new AbortController();
    active.current = request;
    const { signal } = request;
    setError('');
    setPhase('opening');
    let recording: Blob | undefined;
    try {
      recording = await recordVoice(service.value.maxSeconds, signal, stop => {
        stopRecording.current = stop;
        setPhase('recording');
      });
      signal.throwIfAborted();
      setClip(recording);
      if (!service.value.available) {
        setError('本机语音模型暂不可用。可以手动填写草稿或选择外部服务。');
        setPhase('review');
        return;
      }
      setPhase('recognizing');
      const response = await apiResponse(`/voice/transcribe?language=${language}`, 'POST', recording, 'audio/wav', signal);
      const result = await response.json() as { text: string };
      signal.throwIfAborted();
      setDraft(result.text);
      setPhase('review');
    } catch (failure) {
      if (!signal.aborted) {
        setError(recordingError(failure));
        setPhase(recording ? 'review' : 'idle');
      }
    } finally {
      if (active.current === request) active.current = null;
      stopRecording.current = null;
    }
  }

  async function recognizeExternally(authorizationId: string, signal: AbortSignal) {
    if (!clip) return;
    setExternalPending(true);
    try {
      const params = new URLSearchParams({ language, authorizationId });
      const response = await apiResponse('/voice/external?' + params, 'POST', clip, 'audio/wav', signal);
      const result = await response.json() as { text: string };
      signal.throwIfAborted();
      setDraft(result.text);
      setError('');
    } finally {
      setExternalPending(false);
    }
  }

  return <Modal title="录音转文字" onClose={close}>
    {phase === 'idle' && <>
      <label className="project-field">录音语言
        <select value={language} onChange={event => setLanguage(event.target.value as Language)}>
          <option value="zh">普通话</option><option value="en">English</option>
        </select>
      </label>
      {!service.value && !service.error && <Loading text="正在检查本机语音识别" />}
      <ErrorNote message={service.error} retry={service.refresh} />
      {!supported ? <p className="file-status">当前窗口无法录音，可关闭后继续键入。</p>
        : service.value?.available ? <p className="file-status">录音在本机识别，每段最长 {service.value.maxSeconds} 秒。</p>
        : service.value && <p className="file-status">本机语音模型暂不可用。录音后可选择外部服务，每段最长 {service.value.maxSeconds} 秒。</p>}
    </>}
    {phase === 'opening' && <Loading text="正在打开麦克风" />}
    {phase === 'recording' && <p className="voice-status" role="status"><span />正在录音</p>}
    {phase === 'recognizing' && <Loading text="正在本机识别" />}
    {phase === 'review' && <>
      <label className="project-field">语音草稿
        <textarea rows={5} value={draft} disabled={externalPending} onChange={event => setDraft(event.target.value)} />
      </label>
      {tooLong && <p className="file-status" role="status">当前输入框还可填入 {space} 个字符，请缩短草稿。</p>}
    </>}
    <ErrorNote message={error} />
    <div className="action-row voice-actions">
      {phase === 'idle' && <button type="button" className="button primary"
        disabled={!service.value || !supported} onClick={() => void start()}><Mic size={16} />开始录音</button>}
      {phase === 'recording' && <button type="button" className="button primary" onClick={() => stopRecording.current?.()}>
        <Square size={15} />停止并识别</button>}
      {phase === 'review' && <>
        <button type="button" className="button primary" disabled={!text || tooLong || externalPending} onClick={() => apply(text)}>
          {replacing ? '替换字段文字' : '填入文字'}
        </button>
        <button type="button" className="button secondary" disabled={externalPending} onClick={() => {
          setDraft('');
          setClip(undefined);
          setError('');
          setPhase('idle');
        }}>重新录音</button>
      </>}
      <button type="button" className="button secondary" onClick={close}>取消</button>
    </div>
    {(phase === 'idle' || (phase === 'review' && clip)) && <ExternalRecognition kind="voice"
      description={clip ? `本次录音约 ${Math.ceil(clip.size / 1024)} KB，录音语言为${language === 'zh' ? '普通话' : '英语'}`
        : '用于你主动选择的单段录音'}
      replacesDraft={!!text}
      recognize={clip ? recognizeExternally : undefined} />}
  </Modal>;
}

export function VoiceInput({ label, value, limit, onChange, disabled, replace = false, iconOnly = false }: {
  label: string; value: string; limit: number; onChange: (value: string) => void;
  disabled?: boolean; replace?: boolean; iconOnly?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const separator = value && !/\s$/.test(value) ? '\n' : '';
  const space = Math.max(0, replace ? limit : limit - value.length - separator.length);
  return <>
    <button type="button" className={iconOnly ? 'icon-button' : 'button secondary'} disabled={disabled || !space}
      aria-label={label} title={label} onClick={() => setOpen(true)}><Mic size={17} />{iconOnly ? null : '语音输入'}</button>
    {open && <VoiceDraft space={space} replacing={replace} close={() => setOpen(false)} apply={text => {
      onChange(replace ? text : value + separator + text);
      setOpen(false);
    }} />}
  </>;
}
