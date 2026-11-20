import { useState } from 'react';
import { Plus } from 'lucide-react';
import { useSearchParams } from 'react-router-dom';
import { api } from './api';
import { GenerationSettings } from './generation';
import type { Pricing } from './model-usage';
import { BackedStatus, Empty, ErrorNote, Loading, Modal, useAction, useRead } from './shared';

const PROTOCOLS = {
  'chat-completions': 'Chat Completions（OpenAI 格式）',
  'anthropic-messages': 'Anthropic Messages',
  'gemini-generate-content': 'Gemini GenerateContent',
};

const ROLE_LABELS = {
  main: '主模型',
  review: '复核模型',
  mainBackup: '主模型备用连接',
  reviewBackup: '复核备用连接',
};

const TEST_LABELS = {
  passed: '文本回复通过',
  missing_key: '缺少可用的 API 密钥',
  credential_store: '系统凭据库暂不可读，请解锁后重试',
  unauthorized: '服务商拒绝认证或模型访问',
  quota: '服务商额度不足',
  rate_limited: '服务商请求受限，请稍后重试',
  request_rejected: '服务商拒绝请求，请检查地址、模型和协议',
  service_unavailable: '模型服务暂不可用',
  redirect: '服务地址发生跳转，请核对并填写目标地址',
  timeout: '连接测试超时',
  network: '网络连接失败，请检查地址与网络',
  invalid_response: '返回内容与所选协议不匹配',
  no_text: '服务已响应，未收到可显示的文本',
};

type Protocol = keyof typeof PROTOCOLS;
type Role = keyof typeof ROLE_LABELS;
type Roles = Record<Role, string | null>;
type Provider = { id: string; label: string; protocols: Protocol[] };
type ModelTest = {
  status: keyof typeof TEST_LABELS;
  checkedAt: string;
  durationMs: number;
  capabilities: 'text'[];
  usage: { inputTokens?: number; outputTokens?: number } | null;
};
type ModelConnection = {
  id: string;
  name: string;
  provider: string;
  protocol: Protocol;
  endpoint: string;
  model: string;
  hasCredential: boolean;
  testAddress: string;
  revision: number;
  test: ModelTest | null;
  pricing: Pricing | null;
};
type Settings = {
  defaultProvider: string;
  providers: Provider[];
  connections: ModelConnection[];
  roles: Roles;
  testPrompt: string;
  credentialCleanupPending: boolean;
};
type ConnectionDraft = Pick<ModelConnection, 'name' | 'provider' | 'protocol' | 'endpoint' | 'model'>;
type Dialog = { mode: 'create' } | { mode: 'edit' | 'test' | 'delete' | 'pricing'; connection: ModelConnection };
const roles = Object.keys(ROLE_LABELS) as Role[];

function TestStatus({ result }: { result: ModelTest | null }) {
  const label = result ? TEST_LABELS[result.status] : '尚未测试';
  return <span className={`model-status ${result ? result.status === 'passed' ? 'passed' : 'failed' : ''}`}>{label}</span>;
}

function ConnectionList({ settings, open, check, managePrices }: {
  settings: Settings; open: (dialog: Dialog) => void; check: boolean; managePrices: boolean;
}) {
  const assigned = new Set([settings.roles.main, settings.roles.review]);
  const connections = check ? settings.connections.filter(item => assigned.has(item.id)) : settings.connections;
  if (!connections.length) {
    if (check) return <Empty title="暂无已分配的模型" />;
    return <Empty title="尚未添加服务连接"><span>添加连接后即可测试和设置模型分工。</span></Empty>;
  }
  return <div className="model-connections">
    {connections.map(connection => {
      const result = connection.test;
      const assigned = roles.filter(role => settings.roles[role] === connection.id).map(role => ROLE_LABELS[role]);
      const provider = settings.providers.find(item => item.id === connection.provider)?.label;
      return <article className="model-connection" aria-label={connection.name} key={connection.id}>
        <div className="model-connection-head">
          <div><h3>{connection.name}</h3><p>{provider} · {connection.model}</p></div>
          <TestStatus result={result} />
        </div>
        <p className="model-address">{connection.endpoint}</p>
        <p>{PROTOCOLS[connection.protocol]} · {connection.hasCredential ? '密钥已配置' : '未配置密钥'}</p>
        {managePrices && <p>{connection.pricing
          ? `按 ${connection.pricing.currency} 估算 · 每百万 Token 输入 ${connection.pricing.inputPerMillion} / 输出 ${connection.pricing.outputPerMillion}`
          : '未配置估算单价'}</p>}
        {assigned.length > 0 && <p>用于 {assigned.join('、')}</p>}
        {result && <div className="model-test-detail">
          <time dateTime={result.checkedAt}>{new Date(result.checkedAt).toLocaleString('zh-CN')}</time>
          <span>{result.durationMs} 毫秒</span>
          {result.usage?.inputTokens !== undefined && <span>输入 {result.usage.inputTokens} token</span>}
          {result.usage?.outputTokens !== undefined && <span>输出 {result.usage.outputTokens} token</span>}
        </div>}
        <div className="action-row">
          <button className="button soft" type="button" onClick={() => open({ mode: 'test', connection })}>测试连接</button>
          {!check && <>
            <button className="button secondary" type="button" onClick={() => open({ mode: 'edit', connection })}>编辑</button>
            {managePrices && <button className="button secondary" type="button" onClick={() => open({ mode: 'pricing', connection })}>配置单价</button>}
            <button className="text-action model-delete" type="button" onClick={() => open({ mode: 'delete', connection })}>删除</button>
          </>}
        </div>
      </article>;
    })}
  </div>;
}

function ConnectionEditor({
  existing, settings, onClose, onSave,
}: {
  existing?: ModelConnection;
  settings: Settings;
  onClose: () => void;
  onSave: () => void;
}) {
  const defaultProvider = settings.providers.find(item => item.id === settings.defaultProvider) ?? settings.providers[0];
  const [draft, setDraft] = useState<ConnectionDraft>(existing ? {
    name: existing.name, provider: existing.provider, protocol: existing.protocol, endpoint: existing.endpoint, model: existing.model,
  } : { name: '', provider: defaultProvider.id, protocol: defaultProvider.protocols[0], endpoint: '', model: '' });
  const [key, setKey] = useState('');
  const [clearKey, setClearKey] = useState(false);
  const action = useAction();
  const provider = settings.providers.find(item => item.id === draft.provider) ?? defaultProvider;

  async function save() {
    const payload = { ...draft, ...(key.trim() ? { apiKey: key.trim() } : clearKey ? { apiKey: null } : {}) };
    const result = await action.run(() => existing
      ? api<ModelConnection>(`/models/connections/${existing.id}`, 'PUT', { ...payload, revision: existing.revision })
      : api<ModelConnection>('/models/connections', 'POST', payload));
    if (result) onSave();
  }

  return <Modal title={existing ? '编辑服务连接' : '新增服务连接'} onClose={onClose} busy={action.pending}>
    <form className="project-dialog-form" autoComplete="off" onSubmit={event => { event.preventDefault(); void save(); }}>
      <label className="project-field">连接名称
        <input required maxLength={180} disabled={action.pending} value={draft.name}
          onChange={event => setDraft({ ...draft, name: event.target.value })} />
      </label>
      <div className="connection-form-pair">
        <label className="project-field">服务商
          <select disabled={action.pending} value={draft.provider} onChange={event => {
            const selected = settings.providers.find(item => item.id === event.target.value);
            if (selected) setDraft({ ...draft, provider: selected.id, protocol: selected.protocols[0] });
          }}>
            {settings.providers.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
          </select>
        </label>
        <label className="project-field">接口协议
          <select disabled={action.pending} value={draft.protocol}
            onChange={event => setDraft({ ...draft, protocol: event.target.value as Protocol })}>
            {provider.protocols.map(option => <option key={option} value={option}>{PROTOCOLS[option]}</option>)}
          </select>
        </label>
      </div>
      <label className="project-field">API 地址
        <input type="url" required maxLength={2048} disabled={action.pending} aria-describedby="model-address-hint"
          value={draft.endpoint} placeholder="填写服务商的 API 地址"
          onChange={event => setDraft({ ...draft, endpoint: event.target.value })} />
      </label>
      <p id="model-address-hint" className="model-help">填写所选协议的 API 基础地址或完整请求地址。本机服务可使用 HTTP。</p>
      <label className="project-field">模型名称
        <input required maxLength={180} disabled={action.pending} value={draft.model} placeholder="填写服务商提供的模型 ID"
          onChange={event => setDraft({ ...draft, model: event.target.value })} />
      </label>
      <label className="project-field">API 密钥
        <input type="password" autoComplete="new-password" spellCheck={false} disabled={clearKey || action.pending}
          maxLength={4096} value={key} placeholder={existing?.hasCredential ? '留空保留已配置密钥' : '可稍后填写'}
          onChange={event => setKey(event.target.value)} />
      </label>
      {existing?.hasCredential
        ? <label className="model-check"><input type="checkbox" disabled={action.pending} checked={clearKey} onChange={event => {
          setClearKey(event.target.checked);
          if (event.target.checked) setKey('');
        }} />移除已配置的密钥</label>
        : <p className="model-help">{provider.id === 'custom' ? '自定义服务可按接口要求填写密钥。' : '密钥保存在本机系统凭据库。'}</p>}
      <ErrorNote message={action.error} />
      <div className="project-dialog-actions">
        <button className="button secondary" type="button" disabled={action.pending} onClick={onClose}>取消</button>
        <button className="button primary" type="submit" disabled={action.pending}>{action.pending ? '正在保存' : '保存连接'}</button>
      </div>
    </form>
  </Modal>;
}

function PricingEditor({ connection, close, saved }: { connection: ModelConnection; close: () => void; saved: () => void }) {
  const [pricing, setPricing] = useState<Pricing>(connection.pricing ?? { currency: 'CNY', inputPerMillion: '', outputPerMillion: '' });
  const action = useAction();
  async function save(value: Pricing | null) {
    const result = await action.run(() => api<ModelConnection>(`/models/connections/${connection.id}/pricing`, 'PUT', {
      revision: connection.revision, previousPricing: connection.pricing, pricing: value,
    }));
    if (result) saved();
  }
  return <Modal title="配置模型单价" onClose={close} busy={action.pending}>
    <form className="project-dialog-form" onSubmit={event => { event.preventDefault(); void save(pricing); }}>
      <strong>{connection.name} · {connection.model}</strong>
      <p className="model-help">用于按输入与输出 Token 固定单价估算。套餐和特殊计价可以留空。</p>
      <label className="project-field">币种
        <input required pattern="[A-Z]{3}" maxLength={3} value={pricing.currency} disabled={action.pending}
          onChange={event => setPricing({ ...pricing, currency: event.target.value.toUpperCase() })} />
      </label>
      <div className="connection-form-pair">
        <label className="project-field">每百万输入 Token 单价
          <input required inputMode="decimal" maxLength={15} value={pricing.inputPerMillion} disabled={action.pending}
            onChange={event => setPricing({ ...pricing, inputPerMillion: event.target.value })} />
        </label>
        <label className="project-field">每百万输出 Token 单价
          <input required inputMode="decimal" maxLength={15} value={pricing.outputPerMillion} disabled={action.pending}
            onChange={event => setPricing({ ...pricing, outputPerMillion: event.target.value })} />
        </label>
      </div>
      <ErrorNote message={action.error} />
      <div className="project-dialog-actions">
        {connection.pricing && <button type="button" className="text-action model-delete" disabled={action.pending}
          onClick={() => void save(null)}>清除单价</button>}
        <button type="button" className="button secondary" disabled={action.pending} onClick={close}>取消</button>
        <button type="submit" className="button primary" disabled={action.pending}>保存单价</button>
      </div>
    </form>
  </Modal>;
}

function ConfirmConnectionAction({
  connection, mode, settings, onClose, onDone,
}: {
  connection: ModelConnection;
  mode: 'test' | 'delete';
  settings: Settings;
  onClose: () => void;
  onDone: () => void;
}) {
  const action = useAction();
  const testing = mode === 'test';
  const path = `/models/connections/${connection.id}`;
  const rolesUsing = roles.filter(role => settings.roles[role] === connection.id);
  const sendsRequest = connection.hasCredential || connection.provider === 'custom';

  async function confirm() {
    const result = await action.run<ModelTest | { deleted: boolean }>(() => testing
      ? api<ModelTest>(`${path}/test`, 'POST', { revision: connection.revision })
      : api<{ deleted: boolean }>(`${path}?revision=${connection.revision}`, 'DELETE'));
    if (result) onDone();
  }

  return <Modal title={testing ? '测试服务连接' : '删除服务连接'} onClose={onClose} busy={action.pending}>
    <div className="project-dialog-form">
      <strong>{connection.name} · {connection.model}</strong>
      {testing ? <>
        <code className="model-destination">{connection.testAddress}</code>
        {sendsRequest
          ? <p className="model-help">测试会向此地址发送固定短句 <code>{settings.testPrompt}</code>{connection.hasCredential && '，并使用已配置的密钥'}。服务商可能计费。</p>
          : <p className="model-help">此连接尚未配置 API 密钥。测试将检查当前配置。</p>}
        {action.pending && <Loading text="正在测试文本回复" />}
      </> : <>
        <p className="model-address">{connection.endpoint}</p>
        <p className="model-help">{rolesUsing.length
          ? `请先在模型分工中更换或解除相关角色的连接（${rolesUsing.map(role => ROLE_LABELS[role]).join('、')}）。`
          : connection.hasCredential ? '将删除这项连接和已配置的系统凭据。' : '将删除这项连接设置。'}</p>
      </>}
      <ErrorNote message={action.error} />
      <div className="project-dialog-actions">
        <button className="button secondary" type="button" disabled={action.pending} onClick={onClose}>取消</button>
        <button className={`button ${testing ? 'primary' : 'danger'}`} type="button"
          disabled={action.pending || (!testing && rolesUsing.length > 0)} onClick={() => void confirm()}>
          {testing ? '开始测试' : '确认删除'}
        </button>
      </div>
    </div>
  </Modal>;
}

function RoleForm({ settings, onSave, onAdd, primaryOnly }: {
  settings: Settings; onSave: () => void; onAdd: () => void; primaryOnly: boolean;
}) {
  const [changes, setChanges] = useState<Partial<Roles>>({});
  const [saved, setSaved] = useState<{ before: Roles; after: Roles }>();
  const action = useAction();
  if (!settings.connections.length) {
    return <Empty title="暂无可分配的服务连接">
      <button type="button" className="button soft" onClick={onAdd}><Plus size={17} />新增连接</button>
    </Empty>;
  }
  const needsSave = Object.keys(changes).length > 0;
  const assigned = saved?.before === settings.roles ? saved.after : settings.roles;
  const selection = { ...assigned, ...changes };

  async function save() {
    const result = await action.run(() => api<{ roles: Roles }>('/models/roles', 'PATCH', changes));
    if (result) {
      setSaved({ before: settings.roles, after: result.roles });
      setChanges({});
      onSave();
    }
  }

  return <form className="model-role-form" onSubmit={event => { event.preventDefault(); void save(); }}>
    {(primaryOnly ? ['main', 'review'] as const : roles).map(role => {
      const selected = settings.connections.find(item => item.id === selection[role]);
      return <div className="model-role" key={role}>
        <label className="project-field">{ROLE_LABELS[role]}
          <select disabled={action.pending} value={selection[role] ?? ''} onChange={event => {
            const value = event.target.value || null;
            setChanges(previous => {
              const next = { ...previous };
              if (assigned[role] === value) delete next[role];
              else next[role] = value;
              return next;
            });
          }}>
            <option value="">未分配</option>
            {settings.connections.map(item => <option key={item.id} value={item.id}>{item.name} · {item.model}</option>)}
          </select>
        </label>
        {selected && <TestStatus result={selected.test} />}
      </div>;
    })}
    {!primaryOnly && <p className="model-help">在生成或评审的发送预览中授权备用连接后，任务可在服务故障时自动切换。</p>}
    <ErrorNote message={action.error} />
    <div className="action-row">
      <button className="button primary" type="submit" disabled={!needsSave || action.pending}>保存模型分工</button>
      {saved && !needsSave && <span className="model-saved" role="status">模型分工已保存</span>}
    </div>
  </form>;
}

export function ModelSettings({ purpose = 'manage' }: { purpose?: 'manage' | 'setup' | 'check' }) {
  const read = useRead<Settings>('/models');
  const cleanup = useAction();
  const [params, setParams] = useSearchParams();
  const check = purpose === 'check';
  const showRoles = !check && params.get('view') === 'roles';
  const [dialog, setDialog] = useState<Dialog>();
  const settings = read.value;
  const close = () => { setDialog(undefined); read.refresh(); };
  return <section className="panel settings model-settings" aria-labelledby="model-settings-heading">
    <h2 id="model-settings-heading">{check ? '连接测试结果' : purpose === 'setup' ? '服务连接与分工' : 'API与模型'}</h2>
    {!check && <div className="model-toolbar">
      <div className="segmented" role="group" aria-label="API与模型分类">
        <button type="button" aria-pressed={!showRoles} onClick={() => setParams({})}>服务连接</button>
        <button type="button" aria-pressed={showRoles} onClick={() => setParams({ view: 'roles' })}>模型分工</button>
      </div>
      {settings && !showRoles && <button type="button" className="button primary"
        onClick={() => setDialog({ mode: 'create' })}><Plus size={17} />新增连接</button>}
    </div>}
    {!settings ? <BackedStatus error={read.error} retry={read.refresh} /> : <>
      <ErrorNote message={read.error} retry={read.refresh} />
      {settings.credentialCleanupPending && <div className="notice">
        <span>系统凭据库还有待清理的旧密钥。</span>
        <button className="text-action" disabled={cleanup.pending} onClick={() => void cleanup.run(async () => {
          const result = await api<{ credentialCleanupPending: boolean }>('/models/credential-cleanup', 'POST');
          read.refresh();
          if (result.credentialCleanupPending) throw new Error('请解锁系统凭据库后重试。');
        })}>重试清理</button>
      </div>}
      <ErrorNote message={cleanup.error} />
      {showRoles
        ? <RoleForm settings={settings} onSave={read.refresh} primaryOnly={purpose === 'setup'}
          onAdd={() => { setParams({}); setDialog({ mode: 'create' }); }} />
        : <ConnectionList settings={settings} open={setDialog} check={check} managePrices={purpose === 'manage'} />}
      {purpose === 'manage' && <GenerationSettings />}
      {dialog?.mode === 'create' && <ConnectionEditor settings={settings} onClose={close} onSave={close} />}
      {dialog?.mode === 'edit' && <ConnectionEditor existing={dialog.connection} settings={settings} onClose={close} onSave={close} />}
      {dialog?.mode === 'pricing' && <PricingEditor connection={dialog.connection} close={close} saved={close} />}
      {(dialog?.mode === 'test' || dialog?.mode === 'delete') && <ConfirmConnectionAction
        connection={dialog.connection} mode={dialog.mode} settings={settings} onClose={close} onDone={close} />}
    </>}
  </section>;
}
