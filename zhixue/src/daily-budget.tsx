import { useEffect, useId, useState } from 'react';
import { Gauge } from 'lucide-react';
import { Link } from 'react-router-dom';
import { api, courseUrl, dateLabel, projectUrl } from './api';
import { chatUrl } from './conversation';
import { isMoneyHold, TaskCostForm, type Money, type TaskCostChange } from './money-limit';
import { refreshReminders } from './reminders';
import { BackedStatus, Empty, ErrorNote, Modal, useAction, useRead } from './shared';

const PATH = '/generation/daily-budget';
const KINDS = {
  generation: '路线、课程生成与独立核验',
  assessment: '复习与错因补学',
  answer: '课内答疑',
  chat: '自由聊天',
  goal_authoring: '学习目标整理',
  consolidation: '学完后的答疑整理',
  prerequisite: '前置知识讲解',
  stage_review: '阶段成果评审',
  graph: '知识图谱',
  classification: '知识库整理',
  note_ai: '笔记 AI 建议',
  note_check: '笔记后台核对',
};
const PHASES: Record<string, string> = {
  draft: '起草', review: '独立核验', objectives: '编排复习目标', questions: '准备题组',
  grade: '评分', recheck: '重新核查', remediation: '生成错因讲解', remediation_check: '核验错因讲解',
};
type Task = {
  id: string; kind: keyof typeof KINDS; title: string; projectId: string | null; projectTopic: string;
  lessonId: string | null; stageId?: string; conversationId?: string; noteId?: string; blockedOn: string | null; phase: string | null;
  reason: string; canResume: boolean; error?: string; maxCost?: Money | null; usesDefaultCost?: boolean;
};
type BudgetState = {
  billingDay: string; dailyLimit: number | null; used: number; autoContinueDailyBudget: boolean; tasks: Task[];
  dailyCost: { limit: Money; settled: string; reserved: string; unknownCalls: number } | null;
};

function targetUrl(item: Task) {
  if (item.kind === 'graph') return '/library?view=graph&review=1';
  if (item.kind === 'classification') return '/library?classify=1';
  if (item.conversationId) return chatUrl(item.conversationId);
  if (item.noteId) return `/library?file=note-${item.noteId}`;
  if (item.lessonId) return courseUrl(item.lessonId);
  return projectUrl(item.projectId!) + (item.stageId ? `?stageAssessment=${item.stageId}` : '');
}

export function DailyBudgetPanel({ onChange }: { onChange: () => void }) {
  const heading = useId();
  const { value, error, refresh } = useRead<BudgetState>(PATH);
  const action = useAction();
  const [result, setResult] = useState('');
  const [editing, setEditing] = useState<Task>();
  const watching = Boolean(value?.tasks.length || value?.dailyCost?.reserved && value.dailyCost.reserved !== '0');
  useEffect(() => {
    window.addEventListener('focus', refresh);
    const timer = watching ? window.setInterval(() => {
      if (document.visibilityState === 'visible') refresh();
    }, 4000) : undefined;
    return () => {
      window.removeEventListener('focus', refresh);
      if (timer !== undefined) window.clearInterval(timer);
    };
  }, [refresh, watching]);

  if (!value) return <section className="panel settings" aria-labelledby={heading}><h2 id={heading}><Gauge size={20} />额度恢复</h2>
    <BackedStatus error={error} retry={refresh} /></section>;
  const { billingDay, dailyLimit, used, dailyCost, tasks } = value;
  const full = dailyLimit !== null && used >= dailyLimit;
  const ready = tasks.filter(item => item.canResume && item.reason !== 'cost_stopped');

  function changed(message: string) {
    setResult(message);
    refresh();
    refreshReminders();
    onChange();
  }

  async function resume(items: Task[]) {
    const saved = await action.run(() => api<{ resumed: { id: string }[]; skipped: number }>(PATH, 'POST', {
      billingDay, tasks: items.map(({ kind, id }) => ({ kind, id })),
    }));
    if (saved) {
      let summary = saved.resumed.length ? `${saved.resumed.length} 项任务已排队。` : '当前没有可继续的选中任务。';
      if (saved.skipped) summary += ` ${saved.skipped} 项的状态或额度已有变化。`;
      changed(summary);
    } else {
      refresh();
    }
  }

  async function save(item: Task, limit: TaskCostChange) {
    const saved = await action.run(() => api(`${PATH}/tasks/${item.kind}/${item.id}/amount`, 'PUT', limit));
    if (saved) {
      setEditing(undefined);
      changed('任务金额上限已保存。');
    }
  }

  async function stop(item: Task) {
    const saved = await action.run(() => api(`${PATH}/tasks/${item.kind}/${item.id}/stop`, 'POST'));
    if (saved) changed('任务已停止，已保存的进度可继续查看。');
  }

  return <section className="panel settings daily-budget" aria-labelledby={heading}>
    <h2 id={heading}><Gauge size={20} />额度恢复</h2>
    <p className="model-help">北京时间 {dateLabel(billingDay)} · 今日已计入 {used} 次调用{dailyLimit === null ? ' · 未设每日次数上限' : ` / 上限 ${dailyLimit} 次`}</p>
    {dailyCost && <div className="budget-amounts">
      <span>每日金额上限 {dailyCost.limit.amount} {dailyCost.limit.currency}</span>
      <span>实计与估算合计 {dailyCost.settled} {dailyCost.limit.currency}</span>
      {dailyCost.reserved !== '0' && <span>已预留 {dailyCost.reserved} {dailyCost.limit.currency}</span>}
      {dailyCost.unknownCalls > 0 && <p className="model-help">
        {dailyCost.unknownCalls} 次调用缺少可用金额或币种不同。可调整每日金额上限设置，或按次数控制。
      </p>}
    </div>}
    <ErrorNote message={action.error || error} retry={error ? refresh : undefined} />
    {result && <p className="notice success" role="status">{result}</p>}
    {tasks.length ? <>
      <ul className="budget-tasks">
        {tasks.map(item => <li key={`${item.kind}/${item.id}`}>
          <div><Link className="text-action" to={targetUrl(item)}>{item.title}</Link>
            <small>{KINDS[item.kind]}{item.phase && PHASES[item.phase] && ` · ${PHASES[item.phase]}`}
              {item.lessonId && ` · ${item.projectTopic}`}
              {item.blockedOn && ` · 暂停于 ${dateLabel(item.blockedOn)}`}</small>
            {item.maxCost && <small>任务金额上限 {item.maxCost.amount} {item.maxCost.currency}
              {item.usesDefaultCost && ' · 默认上限'}</small>}
            {(!item.canResume || item.reason === 'cost_stopped') && item.error && <small>{item.error}</small>}
          </div>
          <div className="action-row">
            <button type="button" className="button soft" disabled={action.pending || !item.canResume}
              aria-label={`继续 ${item.title} ${KINDS[item.kind]}`} onClick={() => void resume([item])}>继续此任务</button>
            {isMoneyHold(item.reason) && <>
              <button type="button" className="text-action" disabled={action.pending}
                onClick={() => setEditing(item)}>修改任务金额上限</button>
              {item.reason !== 'cost_stopped' && <button type="button" className="text-action" disabled={action.pending}
                onClick={() => void stop(item)}>停止此任务</button>}
            </>}
          </div>
        </li>)}
      </ul>
      {ready.length > 1 && <button type="button" className="button primary" disabled={action.pending}
        onClick={() => void resume(ready)}>继续全部可恢复任务</button>}
    </> : <Empty title={full ? '今日调用次数已用完' : '当前没有待处理的额度暂停任务'} />}
    <Link className="text-action" to="/settings/models">查看模型额度设置</Link>
    {editing && <Modal title="修改任务金额上限" onClose={() => setEditing(undefined)} busy={action.pending}>
      <div className="project-dialog-form">
        <p>{editing.title}</p>
        <TaskCostForm limit={editing.maxCost} inherited={editing.usesDefaultCost} disabled={action.pending}
          submitLabel="保存任务金额上限" save={limit => void save(editing, limit)} />
        <ErrorNote message={action.error} />
      </div>
    </Modal>}
  </section>;
}
