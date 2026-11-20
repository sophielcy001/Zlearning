import { useEffect, useId, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from './api';
import { ErrorNote, Loading, useAction, useRead } from './shared';

type Kind = 'voice' | 'ocr';
type Destination = { connectionId: string; name: string; model: string; endpoint: string; scope: string };
type Authorization = Destination & { id: string };
type Selection = { authorization: Authorization | null };
type Options = Selection & { connections: Destination[] };
type Preview = Destination & { confirmation: string };

type Props = {
  kind: Kind;
  description: string;
  disabled?: boolean;
  replacesDraft?: boolean;
  recognize?: (authorizationId: string, signal: AbortSignal) => Promise<void>;
};

export function ExternalRecognition(props: Props) {
  const [open, setOpen] = useState(false);
  return open ? <RecognitionOptions {...props} close={() => setOpen(false)} /> : <button type="button"
    className="text-action recognition-toggle" disabled={props.disabled} onClick={() => setOpen(true)}>
    选择外部识别服务
  </button>;
}

function RecognitionOptions({ kind, description, disabled, replacesDraft, recognize, close }: Props & { close: () => void }) {
  const path = '/recognition/' + kind;
  const read = useRead<Options>(path);
  const action = useAction();
  const serviceLabel = useId();
  const [connectionId, select] = useState('');
  const [preview, setPreview] = useState<Preview>();
  const [override, setOverride] = useState<{ source: Options | undefined; value: Authorization | null }>();
  const [sending, setSending] = useState(false);
  const request = useRef<AbortController | null>(null);
  const authorization = override && override.source === read.value ? override.value : read.value?.authorization;
  const selected = connectionId || authorization?.connectionId || '';
  const approved = selected === authorization?.connectionId ? authorization : undefined;
  const details = preview ?? approved;
  const busy = disabled || action.pending;

  useEffect(() => () => request.current?.abort(), []);

  function previewSelection() {
    if (!selected) return;
    void action.run(async () => setPreview(await api<Preview>(path + '/preview', 'POST', { connectionId: selected })));
  }

  function authorize() {
    if (!preview) return;
    void action.run(async () => {
      try {
        const result = await api<Selection>(path + '/authorize', 'POST', {
          connectionId: preview.connectionId, confirmation: preview.confirmation,
        });
        setOverride({ source: read.value, value: result.authorization });
      } catch (error) {
        read.refresh();
        throw error;
      } finally {
        setPreview(undefined);
      }
    });
  }

  function revoke() {
    void action.run(async () => {
      const result = await api<Selection>(path, 'DELETE');
      setOverride({ source: read.value, value: result.authorization });
      setPreview(undefined);
      select('');
    });
  }

  function send() {
    if (!approved || !recognize) return;
    void action.run(async () => {
      const controller = new AbortController();
      request.current = controller;
      setSending(true);
      try {
        await recognize(approved.id, controller.signal);
      } catch (error) {
        if (!controller.signal.aborted) {
          read.refresh();
          throw error;
        }
      } finally {
        request.current = null;
        setSending(false);
      }
    });
  }

  return <section className="recognition-options" aria-label="外部识别服务">
    <p className="file-status">{description}。请求使用所选连接的凭据，服务商可能收费。</p>
    {!read.value && !read.error && <Loading text="正在读取服务连接" />}
    <ErrorNote message={read.error} retry={read.refresh} />
    {read.value?.connections.length ? <>
      <div className="project-field">
        <label htmlFor={serviceLabel}>识别服务</label>
        <select id={serviceLabel} value={selected} disabled={busy} onChange={event => {
          select(event.target.value);
          setPreview(undefined);
        }}>
          <option value="">请选择服务</option>
          {read.value.connections.map(option => <option key={option.connectionId} value={option.connectionId}>
            {option.name} · {option.model}
          </option>)}
        </select>
      </div>
      {details && <dl className="recognition-destination">
        <div><dt>接收地址</dt><dd>{details.endpoint}</dd></div>
        <div><dt>模型</dt><dd>{details.model}</dd></div>
        <div><dt>发送范围</dt><dd>{details.scope}</dd></div>
      </dl>}
      {sending && <Loading text="正在等待外部识别" />}
      <div className="action-row">
        {preview ? <button type="button" className="button secondary" disabled={busy} onClick={authorize}>保存授权</button>
          : approved ? recognize && <button type="button" className="button secondary" disabled={busy} onClick={send}>
            {kind === 'voice' ? '发送此段录音' : '发送所选页图'}{replacesDraft ? '并替换草稿' : ''}
          </button> : <button type="button" className="button secondary" disabled={busy || !selected}
            onClick={previewSelection}>预览发送范围</button>}
        {authorization && <button type="button" className="text-action" disabled={busy} onClick={revoke}>撤销授权</button>}
        <button type="button" className="text-action" disabled={busy} onClick={close}>收起</button>
      </div>
    </> : <div className="action-row">
      {read.value && <Link className="text-action" to="/settings/models">前往 API 与模型设置添加连接</Link>}
      <button type="button" className="text-action" onClick={close}>收起</button>
    </div>}
    <ErrorNote message={action.error} />
  </section>;
}
