import { useEffect, useState } from 'react';
import { api } from './api';
import { ErrorNote, Loading, Modal, useAction, useRead } from './shared';

const ENDPOINT = '/external-memory';
type Scope = 'shared' | 'private';
const SCOPE_NAMES: Record<Scope, string> = { shared: '共享记忆', private: 'Agent 专属记忆' };
const STATES = {
  queued: '等待发送', sending: '正在同步', uncertain: '外部保存待核实', synced: '已保存到 Mem0',
  pending_delete: '待同步删除', removed: '外部副本已删除', cancelled: '已取消外部保存', failed: '外部保存失败',
};
type Copy = {
  id: string;
  memoryId: string;
  scope: Scope;
  state: keyof typeof STATES;
  working: boolean;
  error: string | null;
  updatedAt: string;
};
type Settings = { connected: boolean; revision: number; scopes: Scope[]; provider: string; recipient: string };
type ExternalStatus = Settings & { copies: Copy[] };

export function useExternalMemory() {
  const state = useRead<ExternalStatus>(ENDPOINT);
  const working = state.value?.copies.some(copy => copy.working);
  useEffect(() => {
    if (!working) return;
    const timer = window.setInterval(state.refresh, 900);
    return () => window.clearInterval(timer);
  }, [working, state.refresh]);
  return state;
}

export function activeExternalCopy(copy: Copy) {
  return copy.state !== 'cancelled' && copy.state !== 'failed' && copy.state !== 'removed';
}

function ConnectionSettings({ settings, close, saved }: { settings: Settings; close: () => void; saved: () => void }) {
  const [scopes, setScopes] = useState(settings.scopes);
  const [key, setKey] = useState('');
  const action = useAction();

  async function save() {
    const result = await action.run(() => api<Settings>(ENDPOINT + '/settings', 'PUT', {
      revision: settings.revision, scopes, apiKey: key.trim() || undefined,
    }));
    if (result) saved();
  }

  return <Modal title="配置外接记忆" onClose={close} busy={action.pending}>
    <div className="project-dialog-form">
      <div className="generation-destination"><strong>Mem0</strong><code>{settings.recipient}</code></div>
      <label className="project-field">Mem0 API 密钥
        <input type="password" autoComplete="off" maxLength={512} value={key} disabled={action.pending}
          placeholder={settings.connected ? '已保存在系统凭据库' : undefined} onChange={event => setKey(event.target.value)} />
      </label>
      <fieldset className="agent-fields" disabled={action.pending}>
        <legend>允许发送的记忆范围</legend>
        {(Object.entries(SCOPE_NAMES) as [Scope, string][]).map(([scope, label]) => <label className="project-check" key={scope}>
          <input type="checkbox" checked={scopes.includes(scope)} onChange={event => setScopes(event.target.checked
            ? [...scopes, scope] : scopes.filter(value => value !== scope))} />允许外发{label}
        </label>)}
      </fieldset>
      <p className="model-help">选中的范围可逐条确认发送正文。取消授权范围会同步删除现有的外部副本。</p>
      <ErrorNote message={action.error} />
      <div className="project-dialog-actions">
        <button type="button" className="button primary" disabled={action.pending || (
          scopes.length > 0 && !settings.connected && !key.trim()
        )} onClick={() => void save()}>确认保存外接设置</button>
      </div>
    </div>
  </Modal>;
}

export function ExternalExportDialog({ memory, close, queued }: {
  memory: { id: string; revision: number }; close: () => void; queued: () => void;
}) {
  const [id] = useState(() => crypto.randomUUID());
  const request = { id, memoryId: memory.id, revision: memory.revision };
  const [preview, setPreview] = useState<{ text: string; recipient: string; scope: Scope; confirmation: string }>();
  const action = useAction();

  async function inspect() {
    const result = await action.run(() => api<typeof preview>(ENDPOINT + '/preview', 'POST', request));
    if (result) setPreview(result);
  }

  async function send() {
    if (!preview) return;
    const result = await action.run(() => api<Copy>(ENDPOINT + '/copies', 'POST', { ...request, confirmation: preview.confirmation }));
    if (result) queued();
  }

  return <Modal title="发送记忆到 Mem0" onClose={close} busy={action.pending}>
    <div className="project-dialog-form">
      {preview && <>
        <div className="generation-destination"><strong>Mem0</strong><code>{preview.recipient}</code></div>
        <dl className="generation-inputs">
          <div><dt>记忆范围</dt><dd>{SCOPE_NAMES[preview.scope]}</dd></div>
          <div><dt>发送的记忆正文</dt><dd className="evidence-text">{preview.text}</dd></div>
        </dl>
        <p className="model-help">确认后保存这条正文和随机副本标识。修改或停用记忆会同步删除旧副本。</p>
      </>}
      <ErrorNote message={action.error} />
      <div className="project-dialog-actions">
        {preview
          ? <button type="button" className="button primary" disabled={action.pending} onClick={() => void send()}>确认发送记忆</button>
          : <button type="button" className="button primary" disabled={action.pending} onClick={() => void inspect()}>预览发送范围</button>}
      </div>
    </div>
  </Modal>;
}

export function ExternalMemoryPanel({ state }: { state: ReturnType<typeof useExternalMemory> }) {
  const [configuring, setConfiguring] = useState(false);
  const [removing, setRemoving] = useState<Copy>();
  const action = useAction();
  const settings = state.value;

  async function sync(copy: Copy, method: 'POST' | 'DELETE') {
    const target = ENDPOINT + '/copies/' + copy.id + (method === 'POST' ? '/retry' : '');
    const result = await action.run(() => api<Copy>(target, method));
    if (result) {
      setRemoving(undefined);
      state.refresh();
    }
  }

  return <section aria-labelledby="external-memory-heading">
    <div className="skill-library-heading">
      <h3 id="external-memory-heading">外接记忆</h3>
      <button type="button" className="button soft" disabled={!settings} onClick={() => setConfiguring(true)}>配置外接记忆</button>
    </div>
    <ErrorNote message={state.error} retry={state.refresh} />
    {!settings && !state.error && <Loading />}
    {settings && <p className="model-help">{settings.connected && settings.scopes.length
      ? 'Mem0 · 允许逐条发送' + settings.scopes.map(scope => SCOPE_NAMES[scope]).join('、')
      : '外部保存未启用'}</p>}
    {!!settings?.copies.length && <ul className="skill-list" aria-label="外部副本状态">
      {settings.copies.map(copy => <li key={copy.id}>
        <div>
          <strong>{STATES[copy.state]}</strong>
          <p className="model-help">Mem0 · {SCOPE_NAMES[copy.scope]} · {copy.id.slice(0, 8)}</p>
          <ErrorNote message={copy.error ?? undefined} />
        </div>
        {copy.error && !copy.working && activeExternalCopy(copy) && <button type="button" className="button soft"
          disabled={action.pending} onClick={() => void sync(copy, 'POST')}>
          {copy.state === 'pending_delete' ? '重试同步删除' : '重试同步'}
        </button>}
        {activeExternalCopy(copy) && copy.state !== 'pending_delete' && <button type="button" className="button soft"
          disabled={action.pending} onClick={() => setRemoving(copy)}>移除外部副本</button>}
      </li>)}
    </ul>}
    {!removing && <ErrorNote message={action.error} />}
    {configuring && settings && <ConnectionSettings settings={settings} close={() => setConfiguring(false)}
      saved={() => { setConfiguring(false); state.refresh(); }} />}
    {removing && <Modal title="移除外部副本" onClose={() => setRemoving(undefined)} busy={action.pending}>
      <div className="project-dialog-form">
        <p>本地记忆继续可用。Mem0 确认后，外部副本的状态会更新为已删除。</p>
        <ErrorNote message={action.error} />
        <div className="project-dialog-actions">
          <button type="button" className="button primary" disabled={action.pending}
            onClick={() => void sync(removing, 'DELETE')}>确认移除外部副本</button>
        </div>
      </div>
    </Modal>}
  </section>;
}
