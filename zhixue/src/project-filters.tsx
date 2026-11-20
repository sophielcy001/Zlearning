import { useState, type FormEvent } from 'react';
import { Plus, X } from 'lucide-react';
import type { ProjectPriority, ProjectStatus } from './api';
import { PRIORITIES, STATUSES, STATUS_LABELS, type Condition, type ViewDefinition } from './project-view';
import { ErrorNote, Loading, Modal } from './shared';

const FIELDS = {
  status: '项目状态', tag: '项目标签', review: '待复习情况', progress: '已学进度',
  priority: '项目优先级', learned: '最近学习', updated: '最近更新',
};
type Field = keyof typeof FIELDS;
type Op = Condition['op'];
const OPERATORS: Record<Field, [Op, string][]> = {
  status: [['in', '属于'], ['not_in', '不属于']],
  tag: [['in', '包含任一'], ['not_in', '不包含'], ['none', '无标签']],
  review: [['due', '有待复习'], ['not_due', '暂无待复习']],
  progress: [['lt', '小于'], ['gte', '大于等于'], ['between', '范围'], ['none', '尚无进度']],
  priority: [['is', '等于']],
  learned: [['recent', '最近 N 天'], ['between', '日期范围'], ['never', '从未学习']],
  updated: [['recent', '最近 N 天'], ['between', '日期范围']],
};

export function conditionLabel(condition: Condition) {
  const field = FIELDS[condition.field];
  switch (condition.op) {
    case 'in':
    case 'not_in': {
      const names = condition.field === 'status' ? condition.values.map(status => STATUS_LABELS[status]) : condition.values;
      const label = OPERATORS[condition.field].find(([op]) => condition.op === op)?.[1];
      return `${field} ${label} ${names.join('、')}`;
    }
    case 'due': return '有待复习';
    case 'not_due': return '暂无待复习';
    case 'none': return condition.field === 'progress' ? '尚无进度' : '无标签';
    case 'never': return '从未学习';
    case 'lt': return `进度 < ${condition.value}%`;
    case 'gte': return `进度 ≥ ${condition.value}%`;
    case 'is': return `优先级 ${condition.value ? PRIORITIES[condition.value] : '未设置'}`;
    case 'recent': return `${field} · 近 ${condition.days} 天`;
    case 'between': return condition.field === 'progress'
      ? `进度 ${condition.low}–${condition.high}%` : `${field} · ${condition.fromDate} 至 ${condition.toDate}`;
  }
}

type Draft = { id: string; field: Field; op: Op; values: string[]; first: string; second: string };
type DraftValue = Omit<Draft, 'id'>;

function emptyDraft(field: Field, op = OPERATORS[field][0][0]): DraftValue {
  return { field, op, values: [], first: op === 'recent' ? '30' : '', second: '' };
}

function toDraft(condition: Condition): Draft {
  const draft = { id: crypto.randomUUID(), ...emptyDraft(condition.field, condition.op) };
  if ('values' in condition) draft.values = condition.values;
  if ('value' in condition) draft.first = condition.value === null ? 'unset' : String(condition.value);
  if ('low' in condition) { draft.first = String(condition.low); draft.second = String(condition.high); }
  if ('fromDate' in condition) { draft.first = condition.fromDate; draft.second = condition.toDate; }
  if ('days' in condition) draft.first = String(condition.days);
  return draft;
}

function isInteger(value: string, min: number, max: number) {
  return /^\d+$/.test(value) && Number(value) >= min && Number(value) <= max;
}

function toCondition({ field, op, values, first, second }: Draft): Condition | null {
  switch (field) {
    case 'status':
      return values.length && (op === 'in' || op === 'not_in') ? { field, op, values: values as ProjectStatus[] } : null;
    case 'tag':
      if (op === 'none') return { field, op };
      return values.length && (op === 'in' || op === 'not_in') ? { field, op, values } : null;
    case 'review':
      return op === 'due' || op === 'not_due' ? { field, op } : null;
    case 'priority':
      return first ? { field, op: 'is', value: first === 'unset' ? null : first as ProjectPriority } : null;
    case 'progress':
      if (op === 'none') return { field, op };
      if (!isInteger(first, 0, 100)) return null;
      if (op === 'lt' || op === 'gte') return { field, op, value: Number(first) };
      return op === 'between' && isInteger(second, Number(first), 100)
        ? { field, op, low: Number(first), high: Number(second) } : null;
    case 'learned':
    case 'updated':
      if (field === 'learned' && op === 'never') return { field, op };
      if (op === 'recent' && isInteger(first, 1, 36500)) return { field, op, days: Number(first) };
      return op === 'between' && first && second && first <= second ? { field, op, fromDate: first, toDate: second } : null;
  }
}

function MultiValue({ label, options, selected, onChange }: {
  label: string; options: [string, string][]; selected: string[]; onChange: (values: string[]) => void;
}) {
  return options.length ? <fieldset className="filter-multiselect">
    <legend className="sr-only">{label}</legend>
    {options.map(([value, title]) => <label key={value}>
      <input type="checkbox" checked={selected.includes(value)} disabled={!selected.includes(value) && selected.length >= 30} onChange={event =>
        onChange(event.target.checked ? [...selected, value] : selected.filter(item => item !== value))} />
      {title}
    </label>)}
  </fieldset> : <small className="filter-help">可在项目详情添加标签，或选择「无标签」。</small>;
}

function FilterValue({ row, tags, label, change }: {
  row: Draft; tags?: string[]; label: string; change: (value: Partial<DraftValue>) => void;
}) {
  if (row.op === 'none' || row.op === 'never' || row.field === 'review') return <span />;
  if (row.field === 'status') return <MultiValue label={`${label} 状态`} options={STATUSES.map(status => [status, STATUS_LABELS[status]])}
    selected={row.values} onChange={values => change({ values })} />;
  if (row.field === 'tag') {
    if (!tags && !row.values.length) return <span />;
    const options = Array.from(new Set([...row.values, ...(tags ?? [])])).map((name): [string, string] => [name, name]);
    return <MultiValue label={`${label} 标签`} options={options} selected={row.values} onChange={values => change({ values })} />;
  }
  if (row.field === 'priority') return <select aria-label={`${label} 优先级`} value={row.first} onChange={event => change({ first: event.target.value })}>
    <option value="">请选择</option>{Object.entries(PRIORITIES).map(([key, name]) => <option key={key} value={key}>{name}</option>)}
    <option value="unset">未设置</option>
  </select>;
  const dates = row.field !== 'progress' && row.op === 'between';
  const range = row.op === 'between';
  const unit = row.op === 'recent' ? '天' : row.field === 'progress' ? '%' : '';
  return <div className="filter-range">
    <input type={dates ? 'date' : 'number'} aria-label={`${label} ${dates ? '开始日期' : range ? '下限' : row.op === 'recent' ? '天数' : '进度值'}`}
      value={row.first} onChange={event => change({ first: event.target.value })} />
    {range && <><span>至</span><input type={dates ? 'date' : 'number'} aria-label={`${label} ${dates ? '结束日期' : '上限'}`}
      value={row.second} onChange={event => change({ second: event.target.value })} /></>}
    {unit && <span>{unit}</span>}
  </div>;
}

export function ProjectFilters({ view, tags, tagError, retryTags, onApply, onClose }: {
  view: ViewDefinition; tags?: string[]; tagError?: string; retryTags: () => void;
  onApply: (changes: Pick<ViewDefinition, 'combinator' | 'filters'>) => void; onClose: () => void;
}) {
  const [rows, setRows] = useState(() => view.filters.map(toDraft));
  const [combinator, setCombinator] = useState(view.combinator);
  const [error, setError] = useState('');
  const usingTags = rows.some(row => row.field === 'tag' && row.op !== 'none');

  function edit(id: string, updates: Partial<DraftValue>) {
    setError('');
    setRows(current => current.map(row => row.id === id ? { ...row, ...updates } : row));
  }

  function apply(event: FormEvent) {
    event.preventDefault();
    const filters = rows.map(toCondition);
    const invalid = filters.indexOf(null);
    if (invalid >= 0) {
      setError(`请补全或移除条件 ${invalid + 1}。检查数值和日期范围是否有效。`);
      return;
    }
    onApply({ combinator, filters: filters.filter((value): value is Condition => value !== null) });
  }

  return <Modal title="筛选学习项目" onClose={onClose}>
    <form className="project-dialog-form" onSubmit={apply} noValidate>
      <fieldset className="match-choice"><legend>匹配方式</legend>
        <label><input type="radio" name="match" checked={combinator === 'and'} onChange={() => setCombinator('and')} />满足全部条件</label>
        <label><input type="radio" name="match" checked={combinator === 'or'} onChange={() => setCombinator('or')} />满足任一条件</label>
      </fieldset>
      <div className="filter-entries">{rows.map((row, index) => {
        const label = `条件 ${index + 1}`;
        return <div key={row.id} className="filter-row">
          <select aria-label={`${label} 字段`} value={row.field} onChange={event => edit(row.id, emptyDraft(event.target.value as Field))}>
            {Object.entries(FIELDS).map(([value, title]) => <option key={value} value={value}>{title}</option>)}
          </select>
          <select aria-label={`${label} 运算符`} value={row.op} onChange={event => edit(row.id, emptyDraft(row.field, event.target.value as Op))}>
            {OPERATORS[row.field].map(([value, title]) => <option key={value} value={value}>{title}</option>)}
          </select>
          <FilterValue row={row} label={label} tags={tags} change={updates => edit(row.id, updates)} />
          <button type="button" className="icon-button" aria-label={`移除${label}`} onClick={() => {
            setError(''); setRows(current => current.filter(item => item.id !== row.id));
          }}><X size={17} /></button>
        </div>;
      })}</div>
      {usingTags && (tagError ? <ErrorNote message={tagError} retry={retryTags} /> : !tags && <Loading text="正在读取项目标签" />)}
      <button type="button" className="button secondary new-condition" disabled={rows.length >= 30} onClick={() => {
        setRows(current => [...current, { id: crypto.randomUUID(), ...emptyDraft('status') }]); setError('');
      }}><Plus size={17} />添加条件</button>
      <ErrorNote message={error} />
      <div className="project-dialog-actions">
        <button type="button" className="text-action clear-draft" disabled={!rows.length} onClick={() => { setRows([]); setError(''); }}>清空条件</button>
        <button type="button" className="button secondary" onClick={onClose}>取消</button>
        <button className="button primary">应用筛选</button>
      </div>
    </form>
  </Modal>;
}
