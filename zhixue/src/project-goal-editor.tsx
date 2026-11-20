import { useState } from 'react';
import { api } from './api';
import type { GoalField } from './conversation';
import { ErrorNote, type useAction } from './shared';

export type GoalVersion = { version: number; conversationId: string | null; fields: GoalField[] };

type GoalRevision = { baseGoalVersion: number; baseRouteVersion: number; values: Record<string, string> };
type GoalPreview = {
  nextGoalVersion: number;
  nextRouteVersion: number;
  changes: { key: string; label: string; before: string; after: string }[];
  requiresModelConsent: boolean;
  confirmation: string;
};

const DATE_FIELD: GoalField = {
  key: 'deadline', label: '目标日期', limit: 10, type: 'date', value: '', origin: 'suggestion', evidenceIds: [],
};

export function ProjectGoalEditor({ endpoint, goal, route, dateKey, action, loadError, close, onSaved, onRefresh }: {
  endpoint: string; goal: GoalVersion; route: number; dateKey: string; action: ReturnType<typeof useAction>; loadError?: string;
  close: () => void; onSaved: () => void; onRefresh: () => void;
}) {
  const [values, setValues] = useState(() => new Map<string, string>());
  const [checked, setChecked] = useState<{ input: GoalRevision; preview: GoalPreview }>();
  const fields = goal.fields.some(field => field.key === dateKey) ? goal.fields : [...goal.fields, DATE_FIELD];
  const changes = Object.fromEntries(fields.flatMap(field => {
    const edited = values.get(field.key);
    return edited !== undefined && edited.trim() !== field.value ? [[field.key, edited]] : [];
  }));
  const disclosure = checked?.input.baseGoalVersion === goal.version && checked.input.baseRouteVersion === route
    ? checked : undefined;

  async function preview() {
    const input = { baseGoalVersion: goal.version, baseRouteVersion: route, values: changes };
    const result = await action.run(() => api<GoalPreview>(`${endpoint}/preview`, 'POST', input));
    if (result) setChecked({ input, preview: result });
  }

  async function confirm() {
    if (!disclosure) return;
    const result = await action.run(() => api(`${endpoint}/confirm`, 'POST', {
      ...disclosure.input, confirmation: disclosure.preview.confirmation,
    }));
    if (result) onSaved();
  }

  return <form className="goal-revision-form" onSubmit={event => {
    event.preventDefault();
    void (disclosure ? confirm() : preview());
  }}>
    {disclosure ? <>
      <h3>预览目标变化</h3>
      <p className="model-help">确认后保存目标第 {disclosure.preview.nextGoalVersion} 版、路线第 {disclosure.preview.nextRouteVersion} 版，并沿用现有课程和学习记录。</p>
      <ul className="goal-revision-changes" aria-label="目标字段变化">
        {disclosure.preview.changes.map(change => <li key={change.key}>
          <strong>{change.label}</strong>
          <dl>
            <div><dt>原内容</dt><dd>{change.before || '未设置'}</dd></div>
            <div><dt>新内容</dt><dd>{change.after || '未设置'}</dd></div>
          </dl>
        </li>)}
      </ul>
      {disclosure.preview.requiresModelConsent && <p className="model-help">
        后续草稿使用新目标前，需要重新确认模型发送范围。已进入发送阶段的请求继续使用当时确认的目标。
      </p>}
    </> : <>
      <h3>修改学习目标</h3>
      {fields.map(field => {
        const props = {
          value: values.get(field.key) ?? field.value,
          maxLength: field.limit,
          disabled: action.pending,
          required: field.key === 'topic' || field.key === 'outcome',
          onChange: (event: { target: { value: string } }) => {
            const value = event.target.value;
            setValues(current => new Map(current).set(field.key, value));
          },
        };
        return <label key={field.key} className="project-field">{field.label}
          {field.type === 'text' && field.limit > 180 && field.key !== dateKey ? <textarea {...props} rows={2} />
            : <input {...props} type={field.type} placeholder={field.key === dateKey && field.type === 'text' ? 'YYYY-MM-DD' : undefined} />}
        </label>;
      })}
    </>}
    <ErrorNote message={loadError || action.error} />
    {(action.error || loadError) && <button type="button" className="text-action" disabled={action.pending} onClick={() => {
      setChecked(undefined);
      onRefresh();
    }}>刷新版本</button>}
    <div className="project-dialog-actions">
      <button type="button" className="button secondary" disabled={action.pending}
        onClick={disclosure ? () => setChecked(undefined) : close}>{disclosure ? '返回编辑' : '取消'}</button>
      <button className="button primary" disabled={action.pending || (!disclosure && !Object.keys(changes).length)}>
        {disclosure ? '确认目标变更' : '预览目标变化'}
      </button>
    </div>
  </form>;
}
