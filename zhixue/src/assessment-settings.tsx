import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from './api';
import { AssessmentNotePicker } from './assessment-note-picker';
import type { AssessmentNoteExcerpt, AssessmentNoteLocator, AssessmentSettings } from './assessment-types';
import { BackupDestinations, BackupOptions, backupSelection, FailoverRecord, type ModelBackups } from './model-backup';
import { BackedStatus, ErrorNote, Modal, useAction, useRead } from './shared';
import type { Citation, Reviewer } from './verification';

type Scope = {
  main: Reviewer; review: Reviewer | null; backups?: ModelBackups; maxCalls: number | null; confirmation: string; remediation: boolean;
  sections: { index: number; title: string; text: string; citations: Citation[] }[];
  noteExcerpts?: AssessmentNoteExcerpt[]; excludedNotes?: { key: string; locator: AssessmentNoteLocator; reason: string }[];
};

function AssessmentConsent({ path, initial, initialRemediation, initialBackups, initialNotes, close, saved }: {
  path: string; initial: number | null; initialRemediation: boolean; initialBackups?: ModelBackups;
  initialNotes?: AssessmentNoteExcerpt[]; close: () => void; saved: () => void;
}) {
  const [limit, setLimit] = useState(initial === null ? '' : String(initial));
  const [remediation, setRemediation] = useState(initialRemediation);
  const [backups, setBackups] = useState(() => backupSelection(initialBackups));
  const [notes, setNotes] = useState(initialNotes ?? []);
  const [scope, setScope] = useState<Scope>();
  const action = useAction();
  const noteExcerpts = notes.map(({ noteId, revision, start, end }) => ({ noteId, revision, start, end }));
  return <Modal title="确认复习发送范围" onClose={close} busy={action.pending}>
    <div className="project-dialog-form">
      <form className="assessment-consent-form" onSubmit={event => {
        event.preventDefault();
        void action.run(async () => {
          setScope(undefined);
          setScope(await api<Scope>(`${path}/preview`, 'POST', { maxCalls: limit ? Number(limit) : null, remediation, noteExcerpts, ...backups }));
        });
      }}>
        <label className="project-field">本节复习调用次数上限
          <input type="number" min={1} max={10000} step={1} value={limit} placeholder="可留空"
            disabled={action.pending} onChange={event => { setLimit(event.target.value); setScope(undefined); }} />
        </label>
        <label className="assessment-consent-option"><input type="checkbox" checked={remediation} disabled={action.pending}
          onChange={event => { setRemediation(event.target.checked); setScope(undefined); }} />自动生成并核验错因补学</label>
        <BackupOptions selection={backups} disabled={action.pending} onChange={value => {
          setBackups(value); setScope(undefined);
        }} />
        <AssessmentNotePicker path={path} value={notes} disabled={action.pending}
          onChange={value => { setNotes(value); setScope(undefined); }} />
        <button type="submit" className="button secondary" disabled={action.pending}>预览发送范围</button>
      </form>
      {scope && <>
        <div className="generation-destination"><strong>{scope.remediation ? '出题、评分与补学起草' : '出题与评分'} · {scope.main.name} · {scope.main.model}</strong><code>{scope.main.address}</code></div>
        {scope.review && <div className="generation-destination"><strong>{scope.remediation ? '重新核查与补学核验' : '重新核查'} · {scope.review.name} · {scope.review.model}</strong><code>{scope.review.address}</code></div>}
        <BackupDestinations backups={scope.backups} />
        <p className="model-help">出题使用以下核验片段和引文。评分只发送提交的本组答案、题目、标准和对应依据。重新核查由你发起。调用计入每日额度。</p>
        {!!scope.noteExcerpts?.length && <section className="assessment-note-disclosure" aria-label="拟发送的笔记选段">
          <h3>拟发送的笔记选段</h3>
          <p className="model-help">复核模型先对照下方依据核验选段。通过核验的片段供主模型为后续题组选取主题和问法。正式评分使用课程原始依据。</p>
          <ul className="assessment-note-list">{scope.noteExcerpts.map(item => <li key={item.key}>
            <strong>{item.title} · 第 {item.line} 行</strong><blockquote>{item.text}</blockquote>
          </li>)}</ul>
        </section>}
        {!!scope.excludedNotes?.length && <ul className="assessment-note-list" aria-label="暂缓使用的选段">
          {scope.excludedNotes.map(item => <li key={item.key}>{item.reason}</li>)}
        </ul>}
        {scope.remediation && <p className="model-help">补学处理本节已有及之后的有效评价。答错的小题、作答、评分理由和对应依据会发给主模型生成讲解。独立复核模型接收这些内容及补学草稿，通过核验后保存到知识库。</p>}
        <div className="assessment-scope">
          {scope.sections.map(section => <details key={section.index} open={scope.sections.length === 1}>
            <summary>{section.title}</summary><p className="evidence-text">{section.text}</p>
            {section.citations.map((item, index) => <blockquote key={index}>{item.quote}</blockquote>)}
          </details>)}
        </div>
      </>}
      <ErrorNote message={action.error} />
      {action.error && <Link className="text-action" to="/settings/models">打开 API 与模型设置</Link>}
      <div className="project-dialog-actions">
        <button type="button" className="button secondary" disabled={action.pending} onClick={close}>取消</button>
        <button type="button" className="button primary" disabled={!scope || action.pending} onClick={() => {
          if (scope) void action.run(async () => {
            await api(`${path}/authorize`, 'POST', {
              maxCalls: scope.maxCalls, remediation: scope.remediation, confirmation: scope.confirmation, noteExcerpts, ...backups,
            });
            saved();
          });
        }}>确认并启用复习</button>
      </div>
    </div>
  </Modal>;
}

export function LessonAssessmentPanel({ lesson, learned, onChange }: { lesson: string; learned: boolean; onChange: () => void }) {
  const path = `/lessons/${lesson}/assessment`;
  const { value, error, refresh } = useRead<AssessmentSettings>(path);
  const [open, setOpen] = useState(false);
  const action = useAction();
  const running = learned && !!value?.jobs.some(job => job.state === 'queued' || job.state === 'running');
  const blocking = value?.jobs.find(job => job.state === 'blocked');
  useEffect(() => {
    if (!learned) return;
    refresh();
  }, [learned, refresh]);
  useEffect(() => {
    if (!running) return;
    const interval = window.setInterval(refresh, 850);
    return () => window.clearInterval(interval);
  }, [running, refresh]);
  return <section className="assessment-panel" aria-label="本节模型复习">
    {value ? <>
      <div className="action-row">
        <button type="button" className="button secondary" onClick={() => setOpen(true)}>
          {value.authorized ? '更新复习发送范围与额度' : '启用本节模型复习'}
        </button>
        {blocking && value.authorized && <button type="button" className="button secondary" disabled={action.pending}
          onClick={() => void action.run(async () => { await api(`${path}/retry`, 'POST'); refresh(); })}>重试复习任务</button>}
        {value.granted && <button type="button" className="text-action" disabled={action.pending}
          onClick={() => void action.run(async () => { await api(`${path}/revoke`, 'POST'); refresh(); onChange(); })}>撤销复习授权</button>}
      </div>
      {value.authorized && <p className="model-help" role="status">
        {running ? '正在处理复习任务' : !learned ? '标为已学完后准备复习' : '复习已启用'}
        {' · 已计入 '}{value.calls} 次调用{value.maxCalls === null ? '' : ` / 上限 ${value.maxCalls}`}
      </p>}
      {!!value.noteExcerpts?.length && <details className="assessment-note-picker">
        <summary>笔记核验记录</summary>
        <ul className="assessment-note-list">{value.noteExcerpts.map(item => <li key={item.key}>
          {item.route && <Link to={item.route}>{item.title} · 第 {item.line} 行</Link>}
          <p>{item.issue || (item.verified ? '核验通过，可供出题参考' : value.noteState === 'blocked' ? '核验待处理' : '等待核验')}</p>
        </li>)}</ul>
      </details>}
      <FailoverRecord items={value.jobs.find(job => job.failovers?.length)?.failovers} />
      <ErrorNote message={action.error || blocking?.error || error} />
    </> : <BackedStatus error={error} retry={refresh} />}
    {open && value && <AssessmentConsent path={path} initial={value.maxCalls} initialRemediation={value.remediation} initialBackups={value.backups}
      initialNotes={value.noteExcerpts} close={() => setOpen(false)}
      saved={() => { setOpen(false); refresh(); onChange(); }} />}
  </section>;
}
