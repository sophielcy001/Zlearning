import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from './api';
import { ErrorNote, Modal, useAction, useRead } from './shared';

export type NoteFinding = {
  id: string; quote: string; reason: string; start: number; end: number; line: number;
  evidence: { route: string; title: string; quote: string } | null;
};

type Recipient = { name: string; model: string; address: string };
export type NoteCheck = {
  id?: string; revision: string | null; recipient?: Recipient; lessonId?: string | null;
  state: 'off' | 'waiting' | 'checking' | 'empty' | 'paused' | 'reviewed' | 'partial';
  findings: NoteFinding[]; completed?: number; total?: number; unlocated?: number;
  dailyBlocked?: boolean; costBlocked?: boolean; error?: string | null;
};

type ReviewScope = {
  recipient: Recipient; text: string; confirmation: string; currentCalls: number; charactersPerCall: number;
  course: { version: number; sections: { index: number; title: string; text: string }[] } | null;
};

const statusText: Record<NoteCheck['state'], string> = {
  off: '后台检查已关闭',
  waiting: '等待笔记稳定后检查',
  checking: '正在核对笔记',
  empty: '保存正文后自动检查',
  paused: '检查等待处理',
  reviewed: '本版检查完成',
  partial: '本版检查部分完成',
};

export function useNoteCheck(endpoint: string, visible: boolean) {
  const read = useRead<NoteCheck>(visible ? endpoint : null);
  const { value, error, refresh } = read;
  useEffect(() => {
    if (!visible || !value?.id || error) return;
    const delay = value.state === 'waiting' || value.state === 'checking' ? 690 : 2200;
    const timer = window.setTimeout(refresh, delay);
    return () => window.clearTimeout(timer);
  }, [visible, value, error, refresh]);
  return read;
}

function CheckConsent({ endpoint, lessonId, oldLesson, close, refresh }: {
  endpoint: string; lessonId?: string; oldLesson?: string | null; close: () => void; refresh: () => void;
}) {
  const [includeLesson, setIncludeLesson] = useState(Boolean(oldLesson));
  const [scope, setScope] = useState<ReviewScope>();
  const action = useAction();
  const payload = { lessonId: includeLesson ? lessonId : null };

  function disclose() {
    void action.run(async () => setScope(await api<ReviewScope>(endpoint + '/preview', 'POST', payload)));
  }

  function permit() {
    if (!scope) return;
    void action.run(async () => {
      await api(endpoint + '/authorize', 'POST', { ...payload, confirmation: scope.confirmation });
      refresh();
      close();
    });
  }

  return <Modal title="笔记后台检查" onClose={close} busy={action.pending}>
    <div className="project-dialog-form note-check-consent">
      <p>开启后，本篇笔记保存新版本并停顿时，会把正文分段发给所示复核模型。可随时停止后续发送。</p>
      {lessonId && !scope && <label className="note-ai-check">
        <input type="checkbox" checked={includeLesson} disabled={action.pending}
          onChange={event => setIncludeLesson(event.target.checked)} />附带本节已核验讲稿
      </label>}
      {scope && <>
        <div className="note-ai-recipient"><strong>{scope.recipient.name} · {scope.recipient.model}</strong>
          <small>{scope.recipient.address}</small></div>
        <details open><summary>本次笔记范围</summary>
          <pre>{scope.text || '（当前笔记为空）'}</pre>
        </details>
        {scope.course && <details><summary>将使用的第 {scope.course.version} 版已核验讲稿</summary>
          {scope.course.sections.map(section => <section key={section.index}><h3>{section.title}</h3><pre>{section.text}</pre></section>)}
        </details>}
        <p className="file-status">每段最多 {scope.charactersPerCall.toLocaleString()} 字。本版最多 {scope.currentCalls} 次调用，使用每日额度。</p>
      </>}
      <ErrorNote message={action.error} />
      {action.error && !scope && <Link to="/settings/models" className="text-action">打开 API 与模型设置</Link>}
      <div className="action-row">
        <button type="button" className="button primary" disabled={action.pending} onClick={scope ? permit : disclose}>
          {scope ? '确认开启后台检查' : '查看检查范围'}
        </button>
        {scope && <button type="button" className="button secondary" disabled={action.pending}
          onClick={() => setScope(undefined)}>重新查看范围</button>}
        <button type="button" className="text-action" disabled={action.pending} onClick={close}>取消</button>
      </div>
    </div>
  </Modal>;
}

export function NoteCheckPanel({ endpoint, lessonId, read, findings, dirty, syncing, onLocate }: {
  endpoint: string; lessonId?: string; read: ReturnType<typeof useNoteCheck>; findings: readonly NoteFinding[];
  dirty: boolean; syncing: boolean; onLocate: (finding: NoteFinding) => void;
}) {
  const [consent, setConsent] = useState(false);
  const action = useAction();
  const check = read.value;
  const current = !dirty && !syncing;

  function change(method: 'DELETE' | 'POST') {
    if (!check?.id) return;
    void action.run(async () => {
      await api(endpoint + '/' + check.id + (method === 'POST' ? '/resume' : ''), method);
      read.refresh();
    });
  }

  return <section className="note-check-panel" aria-label="笔记检查">
    <div className="note-check-head">
      <h3>笔记检查</h3>
      {check?.id ? <button type="button" className="text-action" disabled={action.pending}
        onClick={() => change('DELETE')}>停止后台检查</button>
        : <button type="button" className="text-action" onClick={() => setConsent(true)}>开启后台检查</button>}
    </div>
    <ErrorNote message={action.error || read.error} retry={read.error ? read.refresh : undefined} />
    {check?.id && <>
      <p className="note-check-status" role="status">
        {dirty ? '当前编辑待保存，保存后检查新版本' : syncing ? '正在同步本机版本' : statusText[check.state]}
        {current && !!check.total && ' · ' + check.completed + '/' + check.total + ' 段'}
      </p>
      {current && check.error && <p className="note-check-status">{check.error}</p>}
      {check.costBlocked ? <Link className="text-action" to="/settings/budget">查看额度恢复</Link>
        : check.dailyBlocked ? <button type="button" className="text-action" disabled={action.pending}
        onClick={() => change('POST')}>继续检查</button>
        : (check.state === 'partial' || check.state === 'paused') && <button type="button" className="text-action"
          onClick={() => setConsent(true)}>重新确认范围</button>}
      {current && !!check.unlocated && <p className="note-check-status">另有 {check.unlocated} 条提示需要可定位的原文。</p>}
      {findings.length > 0 && <ul className="note-check-findings" aria-label="待核对的笔记标注">
        {findings.map(finding => <li key={finding.id}>
          <button type="button" className="text-action" onClick={() => onLocate(finding)}>定位第 {finding.line} 行</button>
          <blockquote>{finding.quote}</blockquote>
          <p>{finding.reason}</p>
          {finding.evidence && <Link to={finding.evidence.route} title={finding.evidence.quote}>{finding.evidence.title} · 核验讲稿</Link>}
        </li>)}
      </ul>}
      {current && check.state === 'reviewed' && findings.length === 0 &&
        <p className="note-check-status">本次未发现可以定位的疑点。</p>}
    </>}
    {consent && <CheckConsent endpoint={endpoint} lessonId={lessonId ?? check?.lessonId ?? undefined}
      oldLesson={check?.lessonId} close={() => setConsent(false)} refresh={read.refresh} />}
  </section>;
}
