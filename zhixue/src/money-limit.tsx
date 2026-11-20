import { useState } from 'react';

export type Money = { amount: string; currency: string };
export type TaskCostChange = { maxCost: Money | null } | { useDefaultCost: true };

const AMOUNT = '(0|[1-9][0-9]{0,5})(\\.[0-9]{1,8})?';
const CURRENCY = '[A-Z]{3}';
const MONEY_HOLDS = ['daily_cost', 'task_cost', 'cost_unavailable', 'cost_stopped'];

export function isMoneyHold(reason?: string | null) {
  return reason != null && MONEY_HOLDS.includes(reason);
}

export function moneyFields(value?: Money | null): Money {
  return value ?? { amount: '', currency: '' };
}

export function MoneyLimitFields({ label, value, onChange, disabled }: {
  label: string; value: Money; onChange: (value: Money) => void; disabled: boolean;
}) {
  return <div className="money-limit-fields">
    <label className="project-field">{label}
      <input type="text" inputMode="decimal" pattern={AMOUNT} maxLength={15} placeholder="可留空"
        value={value.amount} disabled={disabled}
        onChange={event => onChange({ ...value, amount: event.target.value })} />
    </label>
    <label className="project-field money-currency">{label}币种
      <input type="text" pattern={CURRENCY} maxLength={3} placeholder="如 CNY" autoComplete="off"
        required={!!value.amount} disabled={disabled} value={value.currency}
        onChange={event => onChange({ ...value, currency: event.target.value.toUpperCase() })} />
    </label>
  </div>;
}

export function TaskCostForm({ limit, inherited, disabled, submitLabel, save }: {
  limit?: Money | null; inherited?: boolean; disabled: boolean;
  submitLabel: string; save: (change: TaskCostChange) => void;
}) {
  const initial = moneyFields(limit);
  const [draft, setDraft] = useState(initial);
  const changed = draft.amount !== initial.amount || draft.currency !== initial.currency;
  return <form className="task-cost-form" onSubmit={event => {
    event.preventDefault();
    save({ maxCost: draft.amount ? draft : null });
  }}>
    <MoneyLimitFields label="任务金额上限" value={draft} onChange={setDraft} disabled={disabled} />
    <p className="model-help">金额留空可关闭本任务的金额上限。</p>
    <div className="action-row">
      <button type="submit" className="button secondary" disabled={disabled || !changed}>{submitLabel}</button>
      {inherited === false && <button type="button" className="text-action" disabled={disabled}
        onClick={() => save({ useDefaultCost: true })}>使用默认金额上限</button>}
    </div>
  </form>;
}
