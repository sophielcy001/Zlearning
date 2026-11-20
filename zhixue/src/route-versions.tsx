import { useState } from 'react';
import { ArrowDown, ArrowUp } from 'lucide-react';
import { Link } from 'react-router-dom';
import { api, LEARNING_MARKS, type LearningMark } from './api';
import { chatUrl, type CourseAction } from './conversation';
import { ProjectGoalEditor, type GoalVersion } from './project-goal-editor';
import { BackedStatus, Empty, ErrorNote, Modal, useAction, useRead } from './shared';

type RouteNode = {
  id: string; title: string; phase: string; order: number; outcome: string;
  stageId?: string | null; branch?: string | null;
  learningMark?: LearningMark; kind?: 'lesson' | 'practice'; instructions?: string; required?: boolean;
};
type RouteVersion = { version: number; goalVersion: number; createdAt: string; nodes: RouteNode[] };
type Versions = { routes: RouteVersion[]; goals: GoalVersion[]; goalDateKey: string };
type PracticeInput = { afterId: string; title: string; outcome: string; instructions: string; required: boolean };
type PlacementInput = { stage: { anchorId: string } | { name: string }; branch: string | null };
type PlacementDraft = { stage: string; name: string; branch: string };
type StageOption = { id: string; label: string };
type StageScope = { phase: string; lessons: string[] };
type OrderRequest = {
  baseVersion: number; order: string[]; marks?: Record<string, LearningMark | null>;
  practice?: PracticeInput; placements?: Record<string, PlacementInput>;
};
type OrderPreview = {
  baseVersion: number; nextVersion: number; before: RouteNode[]; after: RouteNode[];
  readyLessons: number; learnedLessons: number; confirmation: string;
  stageImpact?: { before: StageScope[]; after: StageScope[] };
};
type OrderDisclosure = { input: OrderRequest; result: OrderPreview };

function stageKey(node: RouteNode) {
  return JSON.stringify([node.phase, node.stageId ?? null]);
}

function placementLabel(node: RouteNode, phase = node.phase) {
  return node.branch ? phase + ' / ' + node.branch : phase;
}

function VersionHistory({ versions }: { versions: Versions }) {
  return <div className="route-history">
    <details>
      <summary>路线版本记录（{versions.routes.length}）</summary>
      {versions.routes.map(route => <details key={route.version}>
        <summary>第 {route.version} 版 · 目标版本 {route.goalVersion}</summary>
        <ol>{route.nodes.map(node => <li key={node.id}>
          <span>{node.title}{node.kind === 'practice' && ' · 实战'}{node.learningMark && ` · ${LEARNING_MARKS[node.learningMark]}`}</span>
          <small className="route-history-placement">{placementLabel(node)}</small>
        </li>)}</ol>
      </details>)}
    </details>
    <details>
      <summary>目标版本记录（{versions.goals.length}）</summary>
      {versions.goals.map(goal => <details key={goal.version}>
        <summary>目标第 {goal.version} 版</summary>
        <dl>{goal.fields.filter(field => field.value).map(field => <div key={field.key}>
          <dt>{field.label}</dt><dd>{field.value}</dd>
        </div>)}</dl>
        {goal.conversationId && <Link className="text-action" to={chatUrl(goal.conversationId)}>打开来源对话</Link>}
      </details>)}
    </details>
  </div>;
}

function PracticeFields({ value, onChange, nodes, disabled }: {
  value: PracticeInput; onChange: (value: PracticeInput) => void; nodes: RouteNode[]; disabled: boolean;
}) {
  return <fieldset className="route-practice-fields" disabled={disabled}>
    <legend>新增实战</legend>
    <label className="project-field">插在知识点后
      <select value={value.afterId} onChange={event => onChange({ ...value, afterId: event.target.value })}>
        {nodes.map(item => <option key={item.id} value={item.id}>{item.title}</option>)}
      </select>
    </label>
    <label className="project-field">实战标题
      <input value={value.title} maxLength={160} onChange={event => onChange({ ...value, title: event.target.value })} />
    </label>
    <label className="project-field">实战预期成果
      <textarea value={value.outcome} rows={2} maxLength={500}
        onChange={event => onChange({ ...value, outcome: event.target.value })} />
    </label>
    <label className="project-field">实战说明
      <textarea value={value.instructions} rows={3} maxLength={10000}
        onChange={event => onChange({ ...value, instructions: event.target.value })} />
    </label>
    <label className="learned-check"><input type="checkbox" checked={value.required}
      onChange={event => onChange({ ...value, required: event.target.checked })} /><span>计入必学进度</span></label>
  </fieldset>;
}

function PlacementFields({ node, options, value, onChange, disabled }: {
  node: RouteNode; options: StageOption[]; value: PlacementDraft; onChange: (value: PlacementDraft) => void; disabled: boolean;
}) {
  return <fieldset className="route-placement-fields" aria-label={'课程归属 ' + node.title} disabled={disabled}>
    <label className="project-field">阶段
      <select aria-label={'阶段 ' + node.title} value={value.stage}
        onChange={event => onChange({ ...value, stage: event.target.value })}>
        {options.map(stage => <option key={stage.id} value={stage.id}>{stage.label}</option>)}
        <option value="new">新建阶段</option>
      </select>
    </label>
    {value.stage === 'new' && <label className="project-field">新阶段名称
      <input aria-label={'新阶段名称 ' + node.title} value={value.name} maxLength={160}
        onChange={event => onChange({ ...value, name: event.target.value })} />
    </label>}
    <label className="project-field">分支
      <input aria-label={'分支 ' + node.title} value={value.branch} maxLength={160} placeholder="留空表示未分组"
        onChange={event => onChange({ ...value, branch: event.target.value })} />
    </label>
  </fieldset>;
}

function markName(value?: LearningMark) {
  return value ? LEARNING_MARKS[value] : '未标注';
}

function StageImpact({ impact }: { impact: NonNullable<OrderPreview['stageImpact']> }) {
  return <section className="route-stage-impact" aria-label="阶段测评范围">
    <h4>阶段测评范围</h4>
    <div className="route-stage-scopes">
      {([{ title: '原范围', scopes: impact.before }, { title: '新范围', scopes: impact.after }]).map(({ title, scopes }) =>
        <div key={title}><strong>{title}</strong>
          {scopes.length ? <ul>{scopes.map((scope, index) => <li key={scope.phase + ':' + index}>
            <strong>{scope.phase}</strong> · {scope.lessons.join('、')}
          </li>)}</ul> : <p>无</p>}
        </div>)}
    </div>
    <p className="model-help">旧范围的测评记录保留。尚未发送的旧范围模型评审将停止。</p>
  </section>;
}

function OrderDifference({ plan, expanded, stages }: {
  plan: OrderPreview; expanded: boolean; stages?: ReadonlyMap<string, StageOption>;
}) {
  const prior = new Map(plan.before.map(node => [node.id, node]));
  function label(node: RouteNode) {
    return placementLabel(node, stages?.get(stageKey(node))?.label);
  }
  return <section className="route-order-body" aria-label="路线变更预览">
    <h3>{expanded ? `预览第 ${plan.nextVersion} 版路线` : `预览第 ${plan.nextVersion} 版顺序`}</h3>
    <ol className="route-order-list">
      {plan.after.map(node => {
        const before = prior.get(node.id);
        return <li key={node.id}><div className="route-difference-node">
          <div><span>{node.order}. </span><strong>{node.title}</strong></div>
          {before ? <>
            {before.order !== node.order && <small>原位置 {before.order}</small>}
            {before.learningMark !== node.learningMark && <small>{markName(before.learningMark)} → {markName(node.learningMark)}</small>}
            {(stageKey(before) !== stageKey(node) || before.branch !== node.branch) &&
              <small>阶段与分支 {label(before)} → {label(node)}</small>}
          </> : <>
            <small>{label(node)} · 新增实战 · {node.required ? '必学' : '选做'}</small>
            <p>{node.outcome}</p>
            <p className="route-practice-instructions">{node.instructions}</p>
          </>}
        </div></li>;
      })}
    </ol>
    {plan.stageImpact && <StageImpact impact={plan.stageImpact} />}
    <p className="model-help">现有路线有 {plan.readyLessons} 节已就绪课程，{plan.learnedLessons} 节已学完。</p>
  </section>;
}

export function RouteVersionsDialog({ projectId, onClose, onChanged, editGoalInitially = false }: {
  projectId: string; onClose: () => void; onChanged: () => void; editGoalInitially?: boolean;
}) {
  const endpoint = `/projects/${projectId}`;
  const read = useRead<Versions>(`${endpoint}/versions`);
  const action = useAction();
  const [draft, setDraft] = useState<Pick<OrderRequest, 'baseVersion' | 'order'>>();
  const [marks, setMarks] = useState<Record<string, LearningMark | null>>({});
  const [editingPlacement, setEditingPlacement] = useState(false);
  const [placementDraft, setPlacementDraft] = useState<{ baseVersion: number; values: Record<string, PlacementDraft> }>();
  const [practice, setPractice] = useState<PracticeInput>();
  const [preview, setPreview] = useState<OrderDisclosure>();
  const [editingGoal, setEditingGoal] = useState(editGoalInitially);
  const latest = read.value?.routes[0];
  const goal = read.value?.goals.find(item => item.version === latest?.goalVersion);
  const date = goal?.fields.find(field => field.key === read.value?.goalDateKey)?.value;
  const current = latest?.nodes ?? [];
  const currentById = new Map(current.map(node => [node.id, node]));
  const order = draft?.baseVersion === latest?.version ? draft?.order ?? [] : current.map(node => node.id);
  const changedMarks = Object.fromEntries(Object.entries(marks).filter(([id, mark]) =>
    currentById.has(id) && (currentById.get(id)?.learningMark ?? null) !== mark));
  const stages = new Map<string, RouteNode>();
  const stageNameCounts = new Map<string, number>();
  for (const node of current) {
    const key = stageKey(node);
    if (stages.has(key)) continue;
    stages.set(key, node);
    stageNameCounts.set(node.phase, (stageNameCounts.get(node.phase) ?? 0) + 1);
  }
  const stageOptions = new Map([...stages].map(([key, node]) => [key, {
    id: node.id,
    label: stageNameCounts.get(node.phase) === 1 ? node.phase : node.phase + ' · ' + node.title,
  }]));
  const options = [...stageOptions.values()];
  const placements = placementDraft?.baseVersion === latest?.version ? placementDraft?.values ?? {} : {};
  const changedPlacements: Record<string, PlacementInput> = {};
  let validPlacement = true;
  for (const [id, value] of Object.entries(placements)) {
    const node = currentById.get(id);
    if (!node) continue;
    const branch = value.branch.trim() || null;
    if (value.stage === 'new') {
      const name = value.name.trim();
      validPlacement = validPlacement && Boolean(name);
      changedPlacements[id] = { stage: { name }, branch };
    } else if (value.stage !== stages.get(stageKey(node))?.id || branch !== (node.branch ?? null)) {
      changedPlacements[id] = { stage: { anchorId: value.stage }, branch };
    }
  }
  const expanded = Boolean(practice || Object.keys(changedMarks).length || Object.keys(changedPlacements).length);
  const changed = expanded || current.some((node, index) => node.id !== order[index]);
  const canPreview = changed && validPlacement && (!practice || Boolean(currentById.has(practice.afterId) && practice.title.trim()
    && practice.outcome.trim() && practice.instructions.trim()));
  const plan = preview?.input.baseVersion === latest?.version ? preview : undefined;

  function move(index: number, delta: number) {
    if (!latest) return;
    const target = index + delta;
    if (target < 0 || target >= order.length) return;
    const next = [...order];
    [next[target], next[index]] = [next[index], next[target]];
    setDraft({ baseVersion: latest.version, order: next });
    setPreview(undefined);
  }
  async function previewOrder() {
    if (!latest || !canPreview) return;
    const input = { baseVersion: latest.version, order, marks: changedMarks, practice, placements: changedPlacements };
    const proposed = await action.run(() => api<OrderPreview>(`${endpoint}/route-order/preview`, 'POST', input));
    if (proposed) setPreview({ input, result: proposed });
  }
  async function confirmOrder() {
    if (!plan) return;
    const confirmed = await action.run(() => api<RouteVersion>(`${endpoint}/route-order/confirm`, 'POST', {
      ...plan.input, confirmation: plan.result.confirmation,
    }));
    if (confirmed) {
      setDraft(undefined);
      setMarks({});
      setPractice(undefined);
      setEditingPlacement(false);
      setPlacementDraft(undefined);
      setPreview(undefined);
      read.refresh();
      onChanged();
    }
  }

  return <Modal title={editGoalInitially ? '修改学习目标' : '路线版本与顺序'} onClose={onClose} busy={action.pending}>
    <div className="project-dialog-form">
      {!read.value ? <BackedStatus error={read.error} retry={read.refresh} /> : latest ? <>
        <p className="route-current-version">
          <strong>当前路线第 {latest.version} 版</strong>{date && <span className="model-help"> · 目标日期 {date}</span>}
        </p>
        <VersionHistory versions={read.value} />
        {editingGoal && goal ? <ProjectGoalEditor endpoint={`${endpoint}/goal-revision`}
          goal={goal} route={latest.version} dateKey={read.value.goalDateKey} action={action} loadError={read.error}
          onRefresh={read.refresh}
          close={editGoalInitially ? onClose : () => setEditingGoal(false)} onSaved={() => {
            if (editGoalInitially) onClose();
            else { setEditingGoal(false); read.refresh(); }
            onChanged();
          }} /> : <>
        {plan ? <OrderDifference plan={plan.result} expanded={expanded} stages={stageOptions} /> : <div className="route-order-body">
          <h3>调整知识点顺序与标记</h3>
          <div className="action-row"><button type="button" className="button secondary"
            aria-expanded={editingPlacement} disabled={action.pending || !current.length}
            onClick={() => setEditingPlacement(value => !value)}>
            重组阶段与分支
          </button></div>
          <ol className="route-order-list">
            {order.map((identity, index) => {
              const node = currentById.get(identity)!;
              const mark = identity in marks ? marks[identity] : node.learningMark;
              return <li key={identity} className={editingPlacement ? 'route-placement-row' : undefined}>
                <span>{index + 1}. {node.title}{node.kind === 'practice' && ' · 实战'}
                  {editingPlacement && <small className="route-history-placement">{placementLabel(node, stageOptions.get(stageKey(node))?.label)}</small>}
                </span>
                <div className="route-order-actions">
                  <select aria-label={`标记 ${node.title}`} value={mark ?? ''} disabled={action.pending}
                    onChange={event => {
                      const value = event.target.value as LearningMark | '';
                      setMarks(previous => ({ ...previous, [identity]: value || null }));
                    }}>
                    <option value="">未标注</option>
                    {Object.entries(LEARNING_MARKS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                  </select>
                  <button type="button" className="icon-button" aria-label={`上移 ${node.title}`} disabled={action.pending || index === 0}
                    onClick={() => move(index, -1)}><ArrowUp size={17} /></button>
                  <button type="button" className="icon-button" aria-label={`下移 ${node.title}`} disabled={action.pending || index === order.length - 1}
                    onClick={() => move(index, 1)}><ArrowDown size={17} /></button>
                </div>
                {editingPlacement && <PlacementFields node={node} options={options}
                  value={placements[identity] ?? { stage: stages.get(stageKey(node))!.id, name: '', branch: node.branch ?? '' }}
                  disabled={action.pending} onChange={value => setPlacementDraft(previous => ({
                    baseVersion: latest.version,
                    values: { ...(previous?.baseVersion === latest.version ? previous.values : {}), [identity]: value },
                  }))} />}
              </li>;
            })}
          </ol>
          {practice ? <>
            <PracticeFields value={practice} onChange={setPractice} nodes={current} disabled={action.pending} />
            <button type="button" className="text-action" disabled={action.pending} onClick={() => setPractice(undefined)}>取消新增实战</button>
          </> : <button type="button" className="button secondary" disabled={action.pending || !order.length}
            onClick={() => setPractice({ afterId: order[order.length - 1], title: '', outcome: '', instructions: '', required: false })}>添加实战</button>}
        </div>}
        <ErrorNote message={action.error || read.error} retry={read.refresh} />
        <div className="project-dialog-actions">
          {!plan && goal && <button type="button" className="button secondary" disabled={action.pending}
            onClick={() => setEditingGoal(true)}>修改学习目标</button>}
          <button type="button" className="button secondary" disabled={action.pending} onClick={plan ? () => setPreview(undefined) : onClose}>
            {plan ? '返回调整' : '完成'}
          </button>
          {plan ? <button type="button" className="button primary" disabled={action.pending} onClick={() => void confirmOrder()}>
            {expanded ? '确认路线变更' : '确认新顺序'}
          </button> : <button type="button" className="button primary" disabled={!canPreview || action.pending} onClick={() => void previewOrder()}>
            {expanded ? '预览路线变化' : '预览顺序变化'}
          </button>}
        </div>
        </>}
      </> : <Empty title="暂无路线版本" />}
    </div>
  </Modal>;
}

export function CourseActionDialog({ request, onClose, onChanged }: {
  request: CourseAction; onClose: () => void; onChanged: () => void;
}) {
  return request.kind === 'goal'
    ? <RouteVersionsDialog projectId={request.projectId} onClose={onClose} onChanged={onChanged} editGoalInitially />
    : <CourseRouteChange request={request} onClose={onClose} onChanged={onChanged} />;
}

function CourseRouteChange({ request, onClose, onChanged }: {
  request: CourseAction; onClose: () => void; onChanged: () => void;
}) {
  const endpoint = '/projects/' + request.projectId;
  const read = useRead<Versions>(endpoint + '/versions');
  const action = useAction();
  const [preview, setPreview] = useState<OrderDisclosure>();
  const route = read.value?.routes[0];
  const index = route?.nodes.findIndex(node => node.id === request.lessonId) ?? -1;
  const lesson = route?.nodes[index];
  const marking = request.kind === 'known';
  const explanation = !lesson ? '这节课已不在当前路线。'
    : marking ? lesson.learningMark === 'known' ? '当前知识点已标为已会。' : '把当前知识点的个人标记改为「已会」。'
      : index === 0 ? '当前知识点已排在首位。' : '将当前知识点向前移动一位。';
  const canPreview = !!lesson && (marking ? lesson.learningMark !== 'known' : index > 0);
  const plan = preview?.input.baseVersion === route?.version ? preview : undefined;

  async function prepare() {
    if (!route || !canPreview) return;
    const order = route.nodes.map(node => node.id);
    const input: OrderRequest = { baseVersion: route.version, order };
    if (marking) input.marks = { [request.lessonId]: 'known' };
    else [order[index - 1], order[index]] = [order[index], order[index - 1]];
    const result = await action.run(() => api<OrderPreview>(endpoint + '/route-order/preview', 'POST', input));
    if (result) setPreview({ input, result });
  }
  async function confirm() {
    if (!plan) return;
    const result = await action.run(() => api<RouteVersion>(endpoint + '/route-order/confirm', 'POST', {
      ...plan.input, confirmation: plan.result.confirmation,
    }));
    if (result) {
      onChanged();
      onClose();
    }
  }

  return <Modal title={marking ? '标记当前知识点' : '提前当前知识点'} onClose={onClose} busy={action.pending}>
    {!read.value ? <BackedStatus error={read.error} retry={read.refresh} /> : <div className="route-order-body">
      {lesson && <strong>{lesson.title}</strong>}
      {plan ? <OrderDifference plan={plan.result} expanded /> : <p className="model-help">{explanation}</p>}
      <ErrorNote message={action.error || read.error} retry={() => { setPreview(undefined); read.refresh(); }} />
      {canPreview && <div className="project-dialog-actions">
        {plan && <button type="button" className="button secondary" disabled={action.pending}
          onClick={() => setPreview(undefined)}>返回</button>}
        <button type="button" className="button primary" disabled={action.pending}
          onClick={() => void (plan ? confirm() : prepare())}>{plan ? '确认路线变更' : '预览路线变化'}</button>
      </div>}
    </div>}
  </Modal>;
}
