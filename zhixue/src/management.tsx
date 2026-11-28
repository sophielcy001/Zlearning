import { Archive, BookOpen, Bot, Gauge, HardDrive, Layers3, Plug } from 'lucide-react';
import { Link, NavLink } from 'react-router-dom';
import { api, dateLabel, timeLabel, type DemoState } from './api';
import { DailyBudgetPanel } from './daily-budget';
import { DeviceTimerReview } from './device-timer-review';
import { BackupSettings } from './backup-settings';
import { ModelSettings } from './model-settings';
import { ModelUsageLedger } from './model-usage';
import { ReminderPreferences } from './reminders';
import { CapabilityCatalog } from './skill-library';
import { ProfessionalAgents } from './professional-agents';
import { LocalMemory } from './local-memory';
import { ErrorNote, useAction, useRead } from './shared';

function SystemSettings({ state, onChange, libraryPath }: { state: DemoState; onChange: () => void; libraryPath?: string }) {
  const action = useAction();
  return <>
    <section className="panel settings"><h2><HardDrive size={21} />本地运行</h2><dl>
      <div><dt>课程与评分</dt><dd>当前使用本地示例。SQLite 提供内置讲义与题组，其他主题使用通用学习方法模板。</dd></div>
      <div><dt>存储</dt><dd>学习状态保存到 SQLite，课程与笔记保存为 Markdown。</dd></div>
      {libraryPath && <div><dt>知识库位置</dt><dd className="setup-location"><code>{libraryPath}</code></dd></div>}
      <div><dt>当前日期</dt><dd>上海自然日 {dateLabel(state.realToday)} · 试用日期 {dateLabel(state.today)}</dd></div>
    </dl></section>
    <section className="panel settings"><h2>学习计时</h2><dl>
      {state.timer.active?.device === 'phone' && <div><dt>手机正在计时</dt><dd>{state.timer.active.title} <button
        className="button soft" disabled={action.pending} onClick={() => {
          const id = state.timer.active?.id;
          if (id) void action.run(async () => { await api('/timer', 'POST', { action: 'finish', timerId: id }); onChange(); });
        }}>结束手机计时</button></dd></div>}
      {state.timer.lastSaved && <div><dt>最近保存的原始计时</dt><dd>{state.timer.lastSaved.title} · {timeLabel(state.timer.lastSaved.elapsed)}</dd></div>}
      {state.timer.stashed.map(timer => <div key={timer.id}><dt>待核对</dt><dd>{timer.title} · {timeLabel(timer.elapsed)} <button className="button soft" disabled={action.pending} onClick={() => {
        void action.run(async () => { await api('/timer', 'POST', { action: 'confirm', timerId: timer.id }); onChange(); });
      }}>确认保存</button></dd></div>)}
      {!state.timer.stashed.length && !state.timer.lastSaved && <div><dt>计时记录</dt><dd>在课程页手动开始计时。</dd></div>}
    </dl><Link to="/study-report" className="text-action">查看和校正计时记录</Link><ErrorNote message={action.error} /></section>
    <DeviceTimerReview onChange={onChange} />
    <ReminderPreferences />
  </>;
}

export function Management({ state, onChange, libraryPath, area = 'system' }: {
  state?: DemoState;
  onChange: () => void;
  area?: 'system' | 'models' | 'backups' | 'budget' | 'skills' | 'agents' | 'memory';
  libraryPath?: string;
}) {
  const memory = useRead<{ waiting: number }>('/memories/summary');
  const waiting = memory.value?.waiting ?? 0;
  return <section className="page management-page">
    <div className="page-head"><h1>管理中心</h1></div>
    <div className="management-layout">
      <nav className="panel management-nav" aria-label="管理中心分类">
        <NavLink to="/settings/models"><Plug size={18} aria-hidden="true" />API与模型</NavLink>
        <NavLink to="/settings/budget"><Gauge size={18} aria-hidden="true" />额度恢复</NavLink>
        <NavLink to="/settings/agents"><Bot size={18} aria-hidden="true" />专业 Agent</NavLink>
        <NavLink to={waiting ? '/settings/memory?view=pending' : '/settings/memory'}><BookOpen size={18} aria-hidden="true" />
          {waiting ? '记忆待确认（' + waiting + '）' : '记忆'}</NavLink>
        <NavLink to="/settings/skills"><Layers3 size={18} aria-hidden="true" />能力库</NavLink>
        <NavLink to="/settings/backups"><Archive size={18} aria-hidden="true" />备份与恢复</NavLink>
        <NavLink to="/settings" end><HardDrive size={18} aria-hidden="true" />系统设置</NavLink>
      </nav>
      <div className="management-content">
        {area === 'models' && <ModelSettings />}
        {area === 'backups' && <BackupSettings onChange={onChange} />}
        {area === 'skills' && <CapabilityCatalog />}
        {area === 'agents' && <ProfessionalAgents />}
        {area === 'memory' && <LocalMemory onChange={memory.refresh} />}
        {area === 'budget' && <><DailyBudgetPanel onChange={onChange} /><ModelUsageLedger /></>}
        {area === 'system' && state && <SystemSettings state={state} onChange={onChange} libraryPath={libraryPath} />}
      </div>
    </div>
  </section>;
}
