import { useState } from 'react';
import { Link } from 'react-router-dom';
import { api, TASK_NAMES, type Task, type TaskStatus } from './api';
import { BackupPermission, BackupRecipient } from './model-backup';
import { CostTotals, type CostSummary } from './model-usage';
import { isMoneyHold, MoneyLimitFields, moneyFields, TaskCostForm, type Money, type TaskCostChange } from './money-limit';
import { refreshReminders } from './reminders';
import { BackedStatus, ErrorNote, Modal, useAction, useRead } from './shared';

export type Consent = { connectionId: string; revision: number; maxCalls: number | null; token: string; allowBackup?: boolean; maxCost?: Money };
type Recipient = { id: string; name: string; model: string; address: string };
type Disclosure = {
  connection: Recipient;
  backup: Recipient | null;
  inputs: { key: string; label: string; value: string }[];
  consent: Consent;
};
type Phase = 'pending' | 'running' | 'complete' | 'awaiting_review' | 'outside_window' | 'failed' | 'discarded'
  | 'review_pending' | 'reviewing' | 'reviewed' | 'disputed' | 'review_failed' | 'revision_ready' | 'revision_unverified';
export type Study = {
  state: TaskStatus;
  reason: string | null;
  error: string | null;
  authorizationCurrent: boolean;
  backup: Recipient | null;
  failovers: { role: 'main' | 'review'; connection: Recipient; reason: string; message: string | null }[];
  focusId: string | null;
  window: string[];
  maxCalls: number | null;
  maxCost: Money | null;
  usesDefaultCost: boolean;
  usage: { calls: number; inputTokens: number | null; outputTokens: number | null; reportedCalls: number; costs: CostSummary };
  steps: { id: string; lessonId: string | null; kind: 'route' | 'draft'; state: Phase }[];
};
type DeletePlan = { drafts: number; pending: number; running: number; routeNodes: number; confirmation: string };
type SavedSettings = {
  dailyLimit: number | null; concurrency: number; autoContinueDailyBudget: boolean;
  dailyCostLimit: Money | null; defaultTaskCostLimit: Money | null;
};
type SettingsDraft = { daily: string; concurrency: string; automatic: boolean; dailyCost: Money; defaultTaskCost: Money };

function settingsDraft(settings: SavedSettings): SettingsDraft {
  return { daily: settings.dailyLimit === null ? '' : String(settings.dailyLimit), concurrency: String(settings.concurrency),
    automatic: settings.autoContinueDailyBudget, dailyCost: moneyFields(settings.dailyCostLimit),
    defaultTaskCost: moneyFields(settings.defaultTaskCostLimit) };
}

const STEP_NAMES: Record<Phase, string> = {
  pending: '已排队', running: '正在生成', complete: '已生成目录', awaiting_review: '草稿待核验',
  outside_window: '待选入准备范围', failed: '准备中断', discarded: '草稿已删除',
  review_pending: '等待独立复核', reviewing: '正在独立复核', reviewed: '复核通过',
  disputed: '有争议片段', review_failed: '复核中断',
  revision_ready: '修订稿待确认', revision_unverified: '修订稿未通过核验',
};

const DRAFT_PROGRESS: [Phase, string][] = [
  ['running', '正在生成草稿'], ['awaiting_review', '草稿待核验'], ['review_pending', '等待独立复核'],
  ['reviewing', '正在独立复核'], ['reviewed', '复核完成'], ['disputed', '有争议片段'],
];

export function lessonStage(study: Study | undefined, identity: string) {
  return study?.steps.find(item => item.lessonId === identity)?.state;
}

export function stepLabel(state: Phase | undefined) {
  return state ? STEP_NAMES[state] : '待生成';
}

export function hasGenerationWork(study: Study | undefined) {
  return study !== undefined && (study.state === 'queued'
    || study.steps.some(step => step.state === 'running' || step.state === 'reviewing'));
}

export function AuthorizationDialog({
  previewPath, payload, initialLimit = null, initialBackup = false, confirmationLabel, confirm, close,
}: {
  previewPath: string;
  payload?: object;
  initialLimit?: number | null;
  initialBackup?: boolean;
  confirmationLabel: string;
  confirm: (consent: Consent) => Promise<void>;
  close: () => void;
}) {
  const [limit, setLimit] = useState(initialLimit === null ? '' : String(initialLimit));
  const [cost, setCost] = useState(() => moneyFields());
  const [allowBackup, setAllowBackup] = useState(initialBackup);
  const [disclosure, setDisclosure] = useState<Disclosure>();
  const action = useAction();
  async function preview() {
    const result = await action.run(() => api<Disclosure>(previewPath, 'POST', {
      ...payload, maxCalls: limit ? Number(limit) : null, ...(allowBackup ? { allowBackup } : {}),
      ...(cost.amount ? { maxCost: cost } : {}),
    }));
    if (result) setDisclosure(result);
  }
  return <Modal title="确认模型发送范围" onClose={close} busy={action.pending}>
    <div className="project-dialog-form">
      <form className="generation-preview-form" onSubmit={event => { event.preventDefault(); void preview(); }}>
        <label className="project-field">本任务调用次数上限
          <input type="number" min={1} max={10000} step={1} placeholder="可留空" value={limit}
            disabled={action.pending} onChange={event => { setLimit(event.target.value); setDisclosure(undefined); }} />
        </label>
        <MoneyLimitFields label="本任务金额上限" value={cost} disabled={action.pending}
          onChange={next => { setCost(next); setDisclosure(undefined); }} />
        <BackupPermission role="主模型" checked={allowBackup} disabled={action.pending}
          onChange={checked => { setAllowBackup(checked); setDisclosure(undefined); }} />
        <button type="submit" className="button secondary" disabled={action.pending}>查看发送范围</button>
      </form>
      {disclosure && <>
        <div className="generation-destination">
          <strong>{disclosure.connection.name} · {disclosure.connection.model}</strong>
          <code>{disclosure.connection.address}</code>
        </div>
        {disclosure.backup && <BackupRecipient connection={disclosure.backup} />}
        {disclosure.consent.maxCost && <p className="model-help">
          本任务金额上限 {disclosure.consent.maxCost.amount} {disclosure.consent.maxCost.currency}
        </p>}
        <dl className="generation-inputs" aria-label="将发送的目标字段">
          {disclosure.inputs.map(field => <div key={field.key}><dt>{field.label}</dt><dd>{field.value}</dd></div>)}
        </dl>
        <p className="model-help">后续准备课程时，选中的知识点信息也会随任务发送。发生服务故障时，已授权备用连接可收到相同范围的输入，调用共用额度。</p>
      </>}
      <ErrorNote message={action.error} />
      {action.error && <Link className="text-action" to="/settings/models">打开 API 与模型设置</Link>}
      <div className="project-dialog-actions">
        <button type="button" className="button secondary" disabled={action.pending} onClick={close}>取消</button>
        <button type="button" className="button primary" disabled={!disclosure || action.pending} onClick={() => {
          if (disclosure) void action.run(() => confirm(disclosure.consent));
        }}>{confirmationLabel}</button>
      </div>
    </div>
  </Modal>;
}

function DeleteTaskButton({ endpoint, mode, action, changed }: {
  endpoint: string; mode: 'model' | 'local'; action: ReturnType<typeof useAction>; changed: () => void;
}) {
  const [deletion, setDeletion] = useState<DeletePlan>();
  function preview() {
    void action.run(async () => setDeletion(await api<DeletePlan>(`${endpoint}/delete-preview`, 'POST')));
  }
  return <>
    <button type="button" className="text-action model-delete" disabled={action.pending} onClick={preview}>删除生成任务</button>
    {deletion && <Modal title="删除生成任务" onClose={() => setDeletion(undefined)} busy={action.pending}>
      <div className="project-dialog-form">
        {mode === 'model' ? <>
          <p>删除任务及 {deletion.drafts} 份课程草稿，取消 {deletion.pending} 个待处理步骤。</p>
          <p className="model-help">同时移除本任务的复核计划和未关联正式课程版本的核验结果。</p>
        </> : <p>删除课程准备任务，取消剩余 {deletion.pending} 节课的准备。</p>}
        <p className="model-help">保留学习路线的 {deletion.routeNodes} 个知识点、已保存的课程和笔记。{mode === 'model' && '任务日志保留已发生的用量记录。'}</p>
        {deletion.running > 0 && <p className="model-help">还有 {deletion.running} 个已发起的调用，服务商可能收取费用。</p>}
        <ErrorNote message={action.error} retry={preview} />
        <div className="project-dialog-actions">
          <button type="button" className="button secondary" disabled={action.pending} onClick={() => setDeletion(undefined)}>取消</button>
          <button type="button" className="button danger" disabled={action.pending} onClick={() => {
            void action.run(async () => {
              await api(`${endpoint}/delete`, 'POST', { confirmation: deletion.confirmation });
              setDeletion(undefined);
              changed();
            });
          }}>确认删除任务</button>
        </div>
      </div>
    </Modal>}
  </>;
}

export function LocalGenerationControls({ project, task, changed }: {
  project: string; task: Task; changed: () => void;
}) {
  const endpoint = `/projects/${project}/generation`;
  const action = useAction();
  const queued = task.status === 'queued';
  const canResume = ['failed', 'stopped', 'restore_pending'].includes(task.status);
  return <>
    <ErrorNote message={action.error || task.error || undefined} />
    {(queued || canResume) && <div className="action-row local-generation-actions">
      <button type="button" className={`button ${queued ? 'secondary' : 'soft'}`} disabled={action.pending} onClick={() => {
        void action.run(async () => {
          await api(`${endpoint}/control`, 'POST', { action: queued ? 'stop' : 'resume' });
          changed();
        });
      }}>{queued ? '停止准备' : task.status === 'failed' ? '重试' : '继续准备'}</button>
      <DeleteTaskButton endpoint={endpoint} mode="local" action={action} changed={changed} />
    </div>}
  </>;
}

export function GenerationControls({ project, value, changed }: {
  project: string; value: Study; changed: () => void;
}) {
  const endpoint = `/projects/${project}/generation`;
  const action = useAction();
  const [authorize, setAuthorize] = useState(false);
  const [budget, setBudget] = useState<string>();
  const [editingCost, setEditingCost] = useState(false);
  const canResume = value.state === 'blocked' || value.state === 'stopped' || value.state === 'restore_pending';
  const canStop = !canResume || value.state === 'blocked';
  const retrying = value.state === 'blocked' && value.steps.some(step => step.state === 'failed' || step.state === 'review_failed');
  const route = value.steps.find(step => step.kind === 'route');
  const draftCounts = new Map<Phase, number>();
  for (const step of value.steps) {
    if (step.kind === 'draft') draftCounts.set(step.state, (draftCounts.get(step.state) ?? 0) + 1);
  }
  async function control(next: 'stop' | 'resume', limit?: { maxCalls: number | null } | TaskCostChange) {
    const result = await action.run(() => api<Study>(`${endpoint}/control`, 'POST', {
      action: next, ...limit,
    }));
    if (result) {
      setBudget(undefined);
      setEditingCost(false);
      changed();
    }
  }
  return <section className="panel generation-controls" aria-label="模型生成任务">
    <div className="generation-summary" role="status">
      <strong>{TASK_NAMES[value.state]}</strong>
      {route && <span>路线 {stepLabel(route.state)}</span>}
      {value.window.length > 0 && <span>当前准备范围 {value.window.length} 节</span>}
      {DRAFT_PROGRESS.map(([state, label]) => {
        const count = draftCounts.get(state);
        return count ? <span key={state}>{label} {count} 节</span> : null;
      })}
    </div>
    <p className="model-help">已计入 {value.usage.calls} 次调用{value.maxCalls !== null && ` / 上限 ${value.maxCalls}`}</p>
    {value.maxCost && <p className="model-help">任务金额上限 {value.maxCost.amount} {value.maxCost.currency}
      {value.usesDefaultCost && ' · 默认上限'}</p>}
    <CostTotals costs={value.usage.costs} />
    {value.usage.reportedCalls > 0 && <p className="model-help">
      {value.usage.reportedCalls} 次有用量回报 · 输入 {value.usage.inputTokens ?? '—'} token · 输出 {value.usage.outputTokens ?? '—'} token
    </p>}
    {value.failovers?.map(item => <div key={item.role} className="backup-current">
      <strong>{item.role === 'main' ? '备用主模型' : '备用复核模型'} · {item.connection.name} · {item.connection.model}</strong>
      {item.message && <span className="model-help">{item.message}</span>}
    </div>)}
    <ErrorNote message={action.error || value.error || undefined} />
    {value.state !== 'deleted' && <>
      <div className="action-row">
        {canResume && <button type="button" className="button soft" disabled={action.pending || value.reason === 'project_paused'}
          onClick={() => value.authorizationCurrent ? void control('resume') : setAuthorize(true)}>
          {value.authorizationCurrent ? (retrying ? '重试任务' : '继续生成任务') : '重新确认发送范围'}
        </button>}
        {canStop && <button type="button" className="button secondary" disabled={action.pending}
          onClick={() => void control('stop')}>停止生成任务</button>}
        {(!canResume || value.authorizationCurrent) && <button type="button" className="text-action"
          disabled={action.pending || value.reason === 'project_paused'} onClick={() => setAuthorize(true)}>重新确认发送范围</button>}
        <DeleteTaskButton endpoint={endpoint} mode="model" action={action} changed={changed} />
      </div>
      {canResume && value.authorizationCurrent && value.reason !== 'project_paused' && <details>
        <summary>修改任务调用额度</summary>
        <form className="generation-preview-form" onSubmit={event => {
          event.preventDefault();
          void control('resume', { maxCalls: budget ? Number(budget) : null });
        }}>
          <label className="project-field">新的调用次数上限
            <input type="number" min={1} max={10000} step={1} placeholder="可留空"
              value={budget ?? (value.maxCalls === null ? '' : String(value.maxCalls))}
              onChange={event => setBudget(event.target.value)} disabled={action.pending} />
          </label>
          <button type="submit" className="button secondary" disabled={action.pending || budget === undefined}>应用额度并继续</button>
        </form>
      </details>}
      {canResume && value.authorizationCurrent && value.reason !== 'project_paused' &&
        <button type="button" className="text-action" disabled={action.pending} onClick={() => setEditingCost(true)}>修改任务金额上限</button>}
      {isMoneyHold(value.reason) && <Link className="text-action" to="/settings/budget">查看额度恢复</Link>}
    </>}
    {editingCost && <Modal title="修改任务金额上限" onClose={() => setEditingCost(false)} busy={action.pending}>
      <div className="project-dialog-form">
        <TaskCostForm limit={value.maxCost} inherited={value.usesDefaultCost} disabled={action.pending}
          submitLabel="应用金额上限并继续" save={limit => void control('resume', limit)} />
        <ErrorNote message={action.error} />
      </div>
    </Modal>}
    {authorize && <AuthorizationDialog
      previewPath={`${endpoint}/authorize/preview`} initialLimit={value.maxCalls} initialBackup={Boolean(value.backup)} close={() => setAuthorize(false)}
      confirmationLabel="确认并继续" confirm={async consent => {
        await api<Study>(`${endpoint}/authorize`, 'POST', consent);
        setAuthorize(false);
        changed();
      }} />}
  </section>;
}

export function DraftDialog({ project, lesson, close }: { project: string; lesson: string; close: () => void }) {
  const read = useRead<{ sections: { title: string; text: string; checks: string[] }[] }>(`/projects/${project}/generation/drafts/${lesson}`);
  return <Modal title="待核验草稿" onClose={close}>
    {!read.value ? <BackedStatus error={read.error} retry={read.refresh} /> : <div className="project-dialog-form draft-sections">
      {read.value.sections.map((section, index) => <article key={index}>
        <h3>{section.title}</h3><p>{section.text}</p>
        {section.checks.length > 0 && <details><summary>待核查事项</summary><ul>
          {section.checks.map((check, item) => <li key={item}>{check}</li>)}
        </ul></details>}
      </article>)}
    </div>}
  </Modal>;
}

export function GenerationSettings() {
  const read = useRead<SavedSettings>('/generation/settings');
  const action = useAction();
  const [draft, setDraft] = useState<SettingsDraft>();
  const [saved, setSaved] = useState(false);
  if (!read.value) return <BackedStatus error={read.error} retry={read.refresh} />;
  const fields = draft ?? settingsDraft(read.value);
  function update(values: Partial<SettingsDraft>) {
    setDraft({ ...fields, ...values });
    setSaved(false);
  }
  return <form className="model-generation-settings" onSubmit={event => {
    event.preventDefault();
    void action.run(async () => {
      const result = await api<SavedSettings>('/generation/settings', 'PUT', {
        dailyLimit: fields.daily ? Number(fields.daily) : null, concurrency: Number(fields.concurrency),
        autoContinueDailyBudget: fields.automatic,
        dailyCostLimit: fields.dailyCost.amount ? fields.dailyCost : null,
        defaultTaskCostLimit: fields.defaultTaskCost.amount ? fields.defaultTaskCost : null,
      });
      setDraft(settingsDraft(result));
      setSaved(true);
      read.refresh();
      refreshReminders();
    });
  }}>
    <h3>后台生成额度</h3>
    <p className="model-help">每日额度按北京时间计算，包含后台任务、聊天和连接测试。</p>
    <label className="project-field">每日生成调用上限
      <input type="number" min={1} max={10000} step={1} value={fields.daily} placeholder="可留空" disabled={action.pending}
        onChange={event => update({ daily: event.target.value })} />
    </label>
    <MoneyLimitFields label="每日金额上限" value={fields.dailyCost} disabled={action.pending}
      onChange={dailyCost => update({ dailyCost })} />
    <MoneyLimitFields label="默认单任务金额上限" value={fields.defaultTaskCost} disabled={action.pending}
      onChange={defaultTaskCost => update({ defaultTaskCost })} />
    <p className="model-help">金额留空可仅用次数控制。启用金额上限需配置同币种单价，发送前按请求大小预留，收到用量后更新。</p>
    <label className="project-field">同时发送的生成请求
      <select value={fields.concurrency} disabled={action.pending} onChange={event => update({ concurrency: event.target.value })}>
        {[1, 2, 3, 4].map(value => <option key={value} value={value}>{value}</option>)}</select>
    </label>
    <label className="reminder-switch">
      <input type="checkbox" checked={fields.automatic} disabled={action.pending}
        onChange={event => update({ automatic: event.target.checked })} />
      额度恢复后自动继续
    </label>
    <p className="model-help">默认由你手动继续。开启后，后台可在次日继续仅因每日上限暂停的已授权任务。</p>
    <ErrorNote message={action.error || read.error} />
    <div className="action-row">
      <button type="submit" className="button secondary" disabled={action.pending || !draft || saved}>保存生成额度</button>
      {saved && <span className="model-saved" role="status">生成额度已保存</span>}
    </div>
  </form>;
}
