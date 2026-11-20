import { useEffect, useState } from 'react';
import { Archive, CalendarClock, FolderOpen } from 'lucide-react';
import { Link } from 'react-router-dom';
import { api, type TimerState } from './api';
import { BackedStatus, Empty, ErrorNote, Loading, Modal, useAction, useRead } from './shared';
import { TimerValue } from './study-timer';

const STATUS = {
  ready: '创建完成',
  candidate: '待解锁校验',
  writing: '正在创建',
  failed: '备份失败',
  unavailable: '文件不可用',
  expiring: '正在清理',
};

const TABLES = {
  projects: '学习项目',
  lessons: '课程',
  reviews: '复习计划',
  attempts: '作答',
  timers: '计时',
  imports: '导入资料',
  library_ocr_pages: '已校对扫描页和图片',
  library_web_snapshots: '网页快照来源',
  note_versions: '笔记版本',
  note_conflicts: '笔记冲突',
  project_views: '项目视图',
  model_connections: '服务连接',
  model_roles: '模型分工',
  tasks: '课程准备任务',
  conversations: '历史对话',
  chat_messages: '对话消息',
  chat_attachments: '对话资料关联',
  goal_versions: '目标版本',
  route_versions: '路线版本',
  model_studies: '模型学习任务',
  generation_steps: '生成阶段',
  generation_settings: '生成额度',
  model_uses: '模型用量记录',
  course_review_plans: '课程复核授权',
  course_findings: '课程核验记录',
  course_versions: '课程正文版本',
  course_anchor_links: '课程摘录关联',
  course_dispositions: '课程争议处置',
  assessment_plans: '复习授权',
  assessment_objectives: '复习知识目标',
  assessment_rounds: '复习题组与作答',
  library_graph: '图谱布局与视角',
  library_graph_revisions: '图谱编辑历史',
  graph_recommendations: '图谱推荐请求',
  graph_candidates: '图谱候选与审核',
  library_wiki_aliases: '文件旧名和路径',
  library_file_tags: '知识库文件标签',
  library_file_activity: '文件收藏和访问时间',
  library_file_copies: '文件复制记录',
  library_classifications: '知识库 AI 整理请求',
  library_classification_candidates: '知识库分类与标签建议',
  assessment_grades: '复习评价版本',
  assessment_jobs: '复习后台任务',
  remediation_cards: '错因补学卡',
  stage_assessments: '阶段综合测评',
  stage_assessment_attempts: '阶段作答与成果',
  stage_reviews: '阶段成果评审授权',
  stage_review_calls: '阶段评审调用与回执',
  lesson_assistant_plans: '课内答疑授权',
  lesson_questions: '课内答疑',
  lesson_reading_positions: '课程阅读位置',
  lesson_consolidation_plans: '答疑整理授权',
  lesson_consolidations: '学完后的答疑整理',
  prerequisite_tasks: '前置知识讲解',
  prerequisite_calls: '前置讲解调用',
  study_plan_settings: '每周学习时段',
  study_day_overrides: '当日时段调整',
  study_plan_locks: '课程表锁定时段',
  timer_spans: '计时间隔',
  timer_corrections: '计时校正',
  study_plan_snapshots: '分日学习计划',
  reminder_settings: '提醒设置',
  reminder_receipts: '提醒与稍后记录',
};

const CHANGES = {
  missing: '当前路径缺少此文件',
  changed: '当前文件内容不同',
  same: '当前文件内容相同',
};

type BackupItem = {
  id: string;
  name: string;
  directory: string;
  status: keyof typeof STATUS;
  size: number | null;
  createdAt: string;
  error: string | null;
  pinned: boolean;
  automatic: boolean;
};
type AutomaticSchedule = {
  enabled: boolean;
  lastAt: string | null;
  lastState: 'saving' | 'ready' | 'failed' | null;
  error: string | null;
  credentialCleanupPending: boolean;
};
type Catalog = { directory: string; items: BackupItem[]; directoryIssue: string | null; automatic: AutomaticSchedule };
const AUTOMATIC_STATES = { saving: '正在创建今日备份', ready: '最近一次已完成', failed: '等待自动重试' };
type SavedFile = {
  path: string;
  name: string;
  size: number;
  change: keyof typeof CHANGES;
  currentSize: number | null;
  copyName: string | null;
  confirmation: string | null;
};
type DataCounts = Partial<Record<keyof typeof TABLES, number>>;
type Preview = {
  counts: { backup: DataCounts; current: DataCounts };
  databaseChanges: (keyof typeof TABLES)[];
  files: SavedFile[];
  currentOnly: { path: string; name: string; size: number }[];
  restore?: { available: boolean; activeTimer: boolean; confirmation: string | null };
};
type RestoreStep = 'quiescing' | 'verifying' | 'saving_current' | 'installing';
type RestoreState = { state: 'idle'; id?: undefined } | {
  id: string; backupId: string; state: 'running' | 'complete' | 'failed' | 'recovery_required';
  step?: RestoreStep; detail?: string; workerIssue?: string;
  rollbackBackupId?: string; heldTasks?: number; heldTimers?: number;
};
const RESTORE_STEPS: Record<RestoreStep, string> = {
  quiescing: '正在等待读写和后台任务停稳',
  verifying: '正在校验备份数据库和知识库文件',
  saving_current: '正在保存恢复前的加密备份',
  installing: '正在切换完整学习数据',
};
type Receipt = { id: string; name: string };
type Dialog = { mode: 'create' | 'destination' | 'automatic' } | { mode: 'preview'; item: BackupItem };
const PAGE_SIZE = 40;

function fileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KiB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
  return `${(bytes / 1024 ** 3).toFixed(1)} GiB`;
}

function DestinationDialog({ directory, close, saved }: {
  directory: string; close: () => void; saved: () => void;
}) {
  const [draft, setDraft] = useState(directory);
  const action = useAction();
  async function update(value: string | null) {
    const result = await action.run(() => api<{ directory: string }>('/backups/destination', 'PUT', { directory: value }));
    if (result) saved();
  }

  return <Modal title="更改备份文件夹" onClose={close} busy={action.pending}>
    <form className="project-dialog-form" onSubmit={event => { event.preventDefault(); void update(draft); }}>
      <label className="project-field">备份文件夹的绝对路径
        <input required maxLength={2048} value={draft} disabled={action.pending} onChange={event => setDraft(event.target.value)} />
      </label>
      <p className="model-help">可填本机磁盘或已连接的网络文件夹。换机时可在此选择存有备份的文件夹。</p>
      <ErrorNote message={action.error} />
      <div className="project-dialog-actions">
        <button type="button" className="text-action clear-draft" disabled={action.pending} onClick={() => void update(null)}>使用默认位置</button>
        <button type="button" className="button secondary" disabled={action.pending} onClick={close}>取消</button>
        <button className="button primary" disabled={action.pending}>保存位置</button>
      </div>
    </form>
  </Modal>;
}

function AutomaticPassword({ enabled, close, saved }: {
  enabled: boolean; close: () => void; saved: () => void;
}) {
  const [password, setPassword] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const action = useAction();
  const title = enabled ? '更改自动备份密码' : '启用每日自动备份';
  const mismatch = Boolean(confirmation) && password !== confirmation;

  async function save() {
    if (!password || password !== confirmation) return;
    const result = await action.run(() => api<AutomaticSchedule>('/backups/automatic', 'PUT', { password }));
    if (result) saved();
  }

  return <Modal title={title} onClose={close} busy={action.pending}>
    <form className="project-dialog-form" autoComplete="off" onSubmit={event => { event.preventDefault(); void save(); }}>
      <p className="model-help">密码保存在本机系统凭据库。既有归档仍使用创建时的密码，换机时也需要输入该密码。</p>
      <label className="project-field">自动备份密码
        <input type="password" required maxLength={512} autoComplete="new-password" value={password}
          disabled={action.pending} onChange={event => setPassword(event.target.value)} />
      </label>
      <label className="project-field">确认自动备份密码
        <input type="password" required maxLength={512} autoComplete="new-password" value={confirmation}
          disabled={action.pending} onChange={event => setConfirmation(event.target.value)} />
      </label>
      <ErrorNote message={mismatch ? '两次填写的密码不同。' : action.error} />
      <div className="project-dialog-actions">
        <button type="button" className="button secondary" disabled={action.pending} onClick={close}>取消</button>
        <button className="button primary" disabled={!password || password !== confirmation || action.pending}>{enabled ? '保存新密码' : '启用自动备份'}</button>
      </div>
    </form>
  </Modal>;
}

function CreateBackup({ directory, close, created }: {
  directory: string; close: () => void; created: () => void;
}) {
  const [password, setPassword] = useState('');
  const action = useAction();
  async function save() {
    const result = await action.run(() => api<BackupItem>('/backups', 'POST', { password }));
    if (result) created();
  }
  return <Modal title="创建加密备份" onClose={close} busy={action.pending}>
    <form className="project-dialog-form" autoComplete="off" onSubmit={event => { event.preventDefault(); void save(); }}>
      <p className="model-help">数据库和知识库文件保存到 <code>{directory}</code>。</p>
      <label className="project-field">备份密码
        <input type="password" required maxLength={512} value={password} autoComplete="new-password"
          disabled={action.pending} onChange={event => setPassword(event.target.value)} />
      </label>
      <p className="model-help">在本机或新设备上解锁这份备份时，请使用此密码。</p>
      <ErrorNote message={action.error} />
      {action.pending && <Loading text="正在生成并校验加密备份" />}
      <div className="project-dialog-actions">
        <button type="button" className="button secondary" disabled={action.pending} onClick={close}>取消</button>
        <button className="button primary" disabled={!password || action.pending}>开始备份</button>
      </div>
    </form>
  </Modal>;
}

function RestoreOverview({ preview }: { preview: Preview }) {
  const [limit, setLimit] = useState(PAGE_SIZE);
  const records = Object.entries(TABLES) as [keyof typeof TABLES, string][];
  const changes = [
    ...preview.files.filter(file => file.change !== 'same').map(file => ({ ...file, message: CHANGES[file.change] })),
    ...preview.currentOnly.map(file => ({ ...file, message: '只在当前知识库中' })),
  ];
  return <div className="backup-overview">
    <h3>整体恢复预览</h3>
    <table className="backup-counts">
      <thead><tr><th scope="col">数据</th><th scope="col">当前</th><th scope="col">备份</th><th scope="col">比较</th></tr></thead>
      <tbody>{records.map(([key, label]) => <tr key={key}>
        <th scope="row">{label}</th>
        <td>{preview.counts.current[key] ?? '—'}</td><td>{preview.counts.backup[key] ?? '—'}</td>
        <td>{preview.databaseChanges.includes(key) ? '有变化' : '相同'}</td>
      </tr>)}</tbody>
    </table>
    <details open={changes.length > 0}><summary>知识库文件差异（{changes.length}）</summary>
      {!changes.length && <p className="model-help">当前文件与备份相同。</p>}
      {changes.slice(0, limit).map(file => <div className="backup-difference" key={file.path}>
        <strong>{file.name}</strong><span>{file.message}</span><small>{file.path}</small>
      </div>)}
      {changes.length > limit && <button type="button" className="text-action" onClick={() => setLimit(limit + PAGE_SIZE)}>显示更多差异</button>}
    </details>
  </div>;
}

function FileChoices({ preview, select }: { preview: Preview; select: (file: SavedFile) => void }) {
  const [query, setQuery] = useState('');
  const [limit, setLimit] = useState(PAGE_SIZE);
  const needle = query.trim().toLocaleLowerCase();
  const choices = preview.files.filter(file => file.confirmation && `${file.name} ${file.path}`.toLocaleLowerCase().includes(needle));
  return <div className="backup-file-choices">
    <label className="project-field">搜索备份中的文件
      <input type="search" value={query} onChange={event => { setQuery(event.target.value); setLimit(PAGE_SIZE); }} />
    </label>
    {!choices.length && <Empty title="没有匹配的资料或笔记" />}
    {choices.slice(0, limit).map(file =>
      <button key={file.path} type="button" className="backup-file-choice" aria-label={`选取 ${file.name}`} onClick={() => select(file)}>
        <strong>{file.name}</strong><span>{fileSize(file.size)} · {CHANGES[file.change]}</span><small>{file.path}</small>
      </button>)}
    {choices.length > limit && <button type="button" className="text-action" onClick={() => setLimit(limit + PAGE_SIZE)}>显示更多文件</button>}
  </div>;
}

function BackupPreview({ item, close, started, onChange }: {
  item: BackupItem; close: () => void; started: (status: RestoreState) => void; onChange: () => void;
}) {
  const [password, setPassword] = useState('');
  const [preview, setPreview] = useState<Preview>();
  const [showFiles, setShowFiles] = useState(false);
  const [selected, setSelected] = useState<SavedFile>();
  const [confirmingWhole, setConfirmingWhole] = useState(false);
  const [restored, setRestored] = useState<Receipt>();
  const timer = useRead<TimerState>(preview?.restore?.activeTimer ? '/timer' : null);
  const activeTimer = timer.value?.active;
  const action = useAction();
  const path = `/backups/${item.id}`;

  async function unlock() {
    const next = await action.run(() => api<Preview>(`${path}/preview`, 'POST', { password }));
    if (next) {
      setPreview(next);
      setSelected(undefined);
    }
  }

  async function settleTimer(kind: 'finish' | 'stash') {
    if (!activeTimer) return;
    const result = await action.run(async () => {
      await api('/timer', 'POST', { action: kind, timerId: activeTimer.id });
      return true;
    });
    if (result) {
      onChange();
      timer.refresh();
      await unlock();
    }
  }

  async function restore() {
    if (!selected?.confirmation) return;
    const result = await action.run(() => api<Receipt>(`${path}/files/restore`, 'POST', {
      password, path: selected.path, confirmation: selected.confirmation,
    }));
    if (result) {
      setRestored(result);
      setPassword('');
    }
  }

  async function restoreWhole() {
    const confirmation = preview?.restore?.confirmation;
    if (!confirmation) return;
    const job = await action.run(() => api<RestoreState>(`${path}/restore`, 'POST', { password, confirmation }));
    if (job) {
      setPassword('');
      started(job);
    }
  }

  return <Modal title={confirmingWhole ? '确认整体恢复' : selected ? '找回文件副本' : '备份预览'} onClose={close} busy={action.pending}>
    {!preview ? <form className="project-dialog-form" autoComplete="off" onSubmit={event => { event.preventDefault(); void unlock(); }}>
      <p className="model-help">{item.name}</p>
      <label className="project-field">备份密码
        <input type="password" required maxLength={512} autoComplete="off" value={password} disabled={action.pending}
          onChange={event => setPassword(event.target.value)} />
      </label>
      <ErrorNote message={action.error} />
      {action.pending && <Loading text="正在解锁并核对备份内容" />}
      <div className="project-dialog-actions">
        <button type="button" className="button secondary" disabled={action.pending} onClick={close}>取消</button>
        <button className="button primary" disabled={!password || action.pending}>解锁并比较</button>
      </div>
    </form> : <div className="project-dialog-form">
      {restored ? <>
        <p className="notice success" role="status">已找回副本 · {restored.name}</p>
        <Link className="button primary" to={`/library?file=${encodeURIComponent(restored.id)}`} onClick={close}>打开找回副本</Link>
      </> : confirmingWhole ? <>
        <strong>{item.name}</strong>
        <p>当前课程、知识库文件、学习记录和应用配置将切换到所选备份。</p>
        <p>切换前用刚输入的备份密码保存当前数据，并永久保留这份恢复前备份。恢复出的后台任务等待你继续，模型密钥按设备配置。</p>
        <p>已经发生的模型调用及费用记录会保留，恢复后继续计入相关额度。</p>
        <ErrorNote message={action.error} />
        <div className="project-dialog-actions">
          <button type="button" className="button secondary" disabled={action.pending} onClick={() => setConfirmingWhole(false)}>返回差异预览</button>
          <button type="button" className="button primary" disabled={action.pending} onClick={() => void restoreWhole()}>确认整体恢复</button>
        </div>
      </> : selected ? <>
        <strong>{selected.name}</strong>
        <p className="model-help">{selected.path}</p>
        <p className="model-help">{CHANGES[selected.change]}</p>
        <p className="model-help">备份文件 {fileSize(selected.size)}{selected.currentSize !== null && ` · 当前文件 ${fileSize(selected.currentSize)}`}</p>
        <p>将作为「{selected.copyName}」添加到知识库。</p>
        <ErrorNote message={action.error} retry={!action.pending ? () => void unlock() : undefined} />
        {action.pending && <Loading text="正在校验备份" />}
        <div className="project-dialog-actions">
          <button type="button" className="button secondary" disabled={action.pending} onClick={() => setSelected(undefined)}>选择其他文件</button>
          <button type="button" className="button primary" disabled={action.pending} onClick={() => void restore()}>确认找回副本</button>
        </div>
      </> : <>
        <div className="segmented backup-preview-tabs" role="group" aria-label="恢复预览类型">
          <button type="button" aria-pressed={!showFiles} disabled={action.pending} onClick={() => setShowFiles(false)}>整体恢复预览</button>
          <button type="button" aria-pressed={showFiles} disabled={action.pending} onClick={() => setShowFiles(true)}>找回文件</button>
        </div>
        {showFiles ? <FileChoices preview={preview} select={setSelected} /> : <>
          <RestoreOverview preview={preview} />
          {preview.restore?.activeTimer && <div className="backup-overview">
            <p className="notice">请选择结束或暂存当前计时，然后重新核对差异。</p>
            {activeTimer && <>
              <p>{activeTimer.title} · <TimerValue timer={activeTimer} /></p>
              <div className="action-row">
                <button type="button" className="button secondary" disabled={action.pending}
                  onClick={() => void settleTimer('finish')}>结束并保存</button>
                <button type="button" className="button secondary" disabled={action.pending}
                  onClick={() => void settleTimer('stash')}>暂存计时</button>
              </div>
            </>}
            <ErrorNote message={timer.error} retry={timer.refresh} />
            <button type="button" className="text-action" disabled={action.pending} onClick={() => void unlock()}>重新预览</button>
          </div>}
          <ErrorNote message={action.error} />
          <div className="project-dialog-actions">
            <button type="button" className="button primary" disabled={!preview.restore?.confirmation || action.pending}
              onClick={() => setConfirmingWhole(true)}>继续整体恢复</button>
          </div>
        </>}
      </>}
    </div>}
  </Modal>;
}

function RestoreMessage({ status }: { status?: RestoreState }) {
  if (!status || status.state === 'idle') return null;
  if (status.state === 'running') {
    return <Loading text={RESTORE_STEPS[status.step ?? 'quiescing']} />;
  }
  if (status.state !== 'complete') {
    return <ErrorNote message={status.detail} />;
  }
  return <>
    <p className="notice success" role="status">整体恢复完成。恢复前的数据已保存为加密备份。</p>
    {Boolean(status.heldTasks) && <p className="model-help">{status.heldTasks} 项旧任务待恢复，可在原页面选择继续。</p>}
    {Boolean(status.heldTimers) && <p className="model-help">恢复的计时已暂存，可在系统设置核对。</p>}
    <ErrorNote message={status.workerIssue} />
  </>;
}

export function BackupSettings({ onChange }: { onChange: () => void }) {
  const read = useRead<Catalog>('/backups');
  const statusRead = useRead<RestoreState>('/backups/restore-status');
  const automaticRead = useRead<AutomaticSchedule>('/backups/automatic');
  const action = useAction();
  const [submitted, setSubmitted] = useState<{ job: RestoreState; previousId?: string }>();
  const [dialog, setDialog] = useState<Dialog>();
  const [notice, setNotice] = useState('');
  const catalog = read.value;
  const automatic = automaticRead.value ?? catalog?.automatic;
  const status = submitted && submitted.previousId === statusRead.value?.id ? submitted.job : statusRead.value;
  const busy = status?.state === 'running' || status?.state === 'recovery_required';
  const refreshStatus = statusRead.refresh;
  const refreshCatalog = read.refresh;
  const refreshAutomatic = automaticRead.refresh;
  useEffect(() => {
    if (!automatic?.enabled || busy) return;
    const interval = window.setInterval(refreshAutomatic, 2500);
    return () => window.clearInterval(interval);
  }, [automatic?.enabled, busy, refreshAutomatic]);
  useEffect(() => {
    if (automatic?.lastState === 'ready' || automatic?.lastState === 'failed') refreshCatalog();
  }, [automatic?.lastAt, automatic?.lastState, refreshCatalog]);
  useEffect(() => {
    if (status?.state !== 'running') return;
    const interval = window.setInterval(refreshStatus, 650);
    return () => window.clearInterval(interval);
  }, [status?.state, refreshStatus]);
  useEffect(() => {
    if (status?.state === 'complete' || status?.state === 'failed') {
      refreshCatalog();
      onChange();
    }
  }, [status?.id, status?.state, refreshCatalog, onChange]);
  const close = () => { setDialog(undefined); refreshCatalog(); };
  const automaticSaved = () => { close(); refreshAutomatic(); };
  return <section className="panel settings backup-settings" aria-labelledby="backup-settings-heading">
    <div className="backup-toolbar">
      <h2 id="backup-settings-heading">备份与恢复</h2>
      <button type="button" className="button primary" disabled={!catalog || busy}
        onClick={() => { setNotice(''); setDialog({ mode: 'create' }); }}><Archive size={17} />创建加密备份</button>
    </div>
    <RestoreMessage status={status} />
    {busy && <ErrorNote message={statusRead.error} retry={refreshStatus} />}
    {!busy && (!catalog ? <BackedStatus error={read.error} retry={read.refresh} /> : <>
      <div className="backup-destination">
        <FolderOpen size={17} aria-hidden="true" />
        <div><strong>保存位置</strong><p>{catalog.directory}</p></div>
        <button type="button" className="text-action" onClick={() => setDialog({ mode: 'destination' })}>更改位置</button>
      </div>
      {automatic && <>
        <div className="backup-destination">
          <CalendarClock size={17} aria-hidden="true" />
          <div>
            <strong>每日自动备份</strong>
            <p role="status">{automatic.enabled ? (automatic.lastState ? AUTOMATIC_STATES[automatic.lastState] : '等待下一次备份') : '已关闭'}
              {automatic.lastAt && <> · <time dateTime={automatic.lastAt}>{new Date(automatic.lastAt).toLocaleString('zh-CN')}</time></>}
            </p>
            <p className="model-help">智学运行时按上海日期备份。近 30 天保留所有备份，更早每月保留一份，并保留永久标记。清理限于当前文件夹里本机创建且已验证的归档。</p>
          </div>
          <span className="action-row">
            <button type="button" className="text-action" disabled={action.pending} onClick={() => setDialog({ mode: 'automatic' })}>
              {automatic.enabled ? '更改自动备份密码' : '设置备份密码'}
            </button>
            {automatic.enabled && <button type="button" className="text-action" disabled={action.pending} onClick={() => void action.run(async () => {
              await api('/backups/automatic', 'DELETE');
              refreshAutomatic();
            })}>关闭自动备份</button>}
          </span>
        </div>
        <ErrorNote message={automatic.error ?? automaticRead.error} />
        {automatic.credentialCleanupPending && <p className="model-help">系统凭据库中有待清理的旧密码，后台会重试。</p>}
      </>}
      <ErrorNote message={read.error || catalog.directoryIssue || undefined} retry={read.refresh} />
      <ErrorNote message={action.error} />
      {notice && <p className="notice success" role="status">{notice}</p>}
      {!catalog.items.length && <Empty title="暂无备份"><span>创建加密备份，或选择存有既有备份的文件夹。</span></Empty>}
      <div className="backup-list">
        {catalog.items.map(item => {
          const available = item.status === 'ready' || item.status === 'candidate';
          return <article className="backup-record" key={item.id} aria-label={item.name}>
            <div className="backup-record-head">
              <strong><time dateTime={item.createdAt}>{new Date(item.createdAt).toLocaleString('zh-CN')}</time></strong>
              <span className={`model-status ${item.status === 'failed' || item.status === 'unavailable' ? 'failed' : ''}`}>{STATUS[item.status]}{item.automatic && ' · 自动'}</span>
            </div>
            <p className="model-help">{item.name}</p>
            {status?.state === 'complete' && status.rollbackBackupId === item.id && <p className="model-help">恢复前快照</p>}
            {item.directory !== catalog.directory && <p className="model-help">{item.directory}</p>}
            {item.size !== null && <p className="model-help">{fileSize(item.size)}{item.pinned && ' · 永久保留'}</p>}
            {item.error && <p className="model-help">{item.error}</p>}
            {available && <div className="action-row">
              <button type="button" className="button soft" onClick={() => setDialog({ mode: 'preview', item })}>预览备份</button>
              <button type="button" className="text-action" disabled={action.pending} onClick={() => void action.run(async () => {
                await api(`/backups/${item.id}/retention`, 'PATCH', { pinned: !item.pinned });
                read.refresh();
              })}>{item.pinned ? '取消永久保留' : '永久保留'}</button>
            </div>}
          </article>;
        })}
      </div>
      {dialog?.mode === 'destination' && <DestinationDialog directory={catalog.directory} close={close} saved={automaticSaved} />}
      {dialog?.mode === 'automatic' && automatic && <AutomaticPassword enabled={automatic.enabled} close={close} saved={automaticSaved} />}
      {dialog?.mode === 'create' && <CreateBackup directory={catalog.directory} close={close} created={() => {
        setNotice('已创建加密备份');
        close();
      }} />}
      {dialog?.mode === 'preview' && <BackupPreview item={dialog.item} close={close} onChange={onChange} started={job => {
        setDialog(undefined);
        setNotice('');
        setSubmitted({ job, previousId: statusRead.value?.id });
        refreshStatus();
      }} />}
    </>)}
  </section>;
}
