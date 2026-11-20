import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, projectUrl } from './api';
import type { AnchorList } from './course-anchors';
import type { Money } from './money-limit';
import { SourcePicker, sourceLocators } from './source-picker';
import { BackedStatus, ErrorNote, Modal, useAction, useRead } from './shared';
import { citationUrl, verificationLabels, type Reviewer, type SourceExcerpt, type SourceLocator, type VerifiedSection } from './verification';

type Passage = Pick<VerifiedSection, 'title' | 'text' | 'checks'>;
type RevisionRequest = { baseVersion: number; section: number | null; instruction: string; sources?: SourceLocator[] };
export type RevisionSeed = { section: number | null; instruction: string; sources: SourceExcerpt[] };
type Scope = {
  main: Reviewer; backup: Reviewer | null; reviewer: Reviewer; reviewBackup: Reviewer | null;
  content: Passage[]; context: Passage[]; goal: Record<string, string>; sources: SourceExcerpt[]; shareSourcesWithAuthor: boolean;
  maxCalls: number | null; maxCost: Money | null; dailyCost: Money | null; confirmation: string;
};
type Finding = Pick<VerifiedSection, 'index' | 'state' | 'reason' | 'detail'>;
type Revision = {
  id?: string; baseVersion: number; state: string; instruction?: string; error?: string;
  publishedVersion?: number; confirmation?: string;
  seen?: boolean;
  canContinueRestore?: boolean; canRenewReview?: boolean;
  changes?: { index: number; before: VerifiedSection | null; after: VerifiedSection | null; beforeOrder: number | null; afterOrder: number | null }[];
  notes?: { id: string; title: string }[]; objectives?: { id: string; title: string }[];
  findings?: Finding[];
};
type ReviewScope = Pick<Scope, 'reviewer' | 'reviewBackup' | 'sources' | 'content' | 'confirmation'>;

function Recipient({ label, value }: { label: string; value: Reviewer }) {
  return <div className="generation-destination"><strong>{label} · {value.name} · {value.model}</strong>
    <code>{value.address}</code></div>;
}

function Passages({ title, items }: { title: string; items: Passage[] }) {
  return <details className="section-verification">
    <summary>{title}</summary>
    <ol>{items.map((item, index) => <li key={index}>
      <strong>{item.title}</strong><p className="evidence-text">{item.text}</p>
      {!!item.checks.length && <p className="model-help">{item.checks.join('、')}</p>}
    </li>)}</ol>
  </details>;
}

function EvidenceScope({ sources, both = false, expanded = both }: {
  sources: SourceExcerpt[]; both?: boolean; expanded?: boolean;
}) {
  return <details className="section-verification" open={expanded}>
    <summary>{both ? '起草与独立复核收到的资料' : '独立复核收到的资料'}</summary>
    <ul>{sources.map(item => <li key={item.id}>
      <Link to={citationUrl({ ...item, quote: item.excerpt })}>{item.title} · {item.label}</Link>
      <blockquote>{item.excerpt}</blockquote>
    </li>)}</ul>
  </details>;
}

function SendingScope({ value }: { value: Scope }) {
  return <section className="project-dialog-form" aria-label="修订发送范围">
    <Recipient label="起草" value={value.main} />
    {value.backup && <Recipient label="起草备用" value={value.backup} />}
    <Recipient label="独立复核" value={value.reviewer} />
    {value.reviewBackup && <Recipient label="复核备用" value={value.reviewBackup} />}
    <p className="model-help">起草发送目标、修改要求和选定正文{value.shareSourcesWithAuthor && '及下列所选资料'}。独立复核接收草稿及下列资料，局部修订时也接收其余已发布小节。</p>
    <Passages title="主模型收到的原正文" items={value.content} />
    <details className="section-verification"><summary>发送的学习目标</summary>
      <dl>{Object.entries(value.goal).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl>
    </details>
    {!!value.context.length && <Passages title="独立复核的上下文" items={value.context} />}
    <EvidenceScope sources={value.sources} both={value.shareSourcesWithAuthor} />
    <p className="model-help">沿用课程准备额度{value.maxCalls !== null && ` · 最多 ${value.maxCalls} 次`}
      {value.maxCost && ` · 单任务 ${value.maxCost.amount} ${value.maxCost.currency}`}
      {value.dailyCost && ` · 每日 ${value.dailyCost.amount} ${value.dailyCost.currency}`}。</p>
  </section>;
}

const STATE_LABELS: Record<string, string> = {
  pending: '修订已排队', running: '正在生成修订稿', review_pending: '等待独立核验', reviewing: '正在独立核验',
  ready: '独立核验通过，等待确认', not_verified: '修订稿未通过独立核验', blocked: '修订等待处理',
  stopped: '修订已暂停', failed: '生成中断', review_failed: '核验中断',
};

function CourseChanges({ changes }: { changes: Revision['changes'] }) {
  return <>
    <h3>新版差异</h3>
    {changes?.length ? <ol className="course-revision-changes">{changes.map(item => <li key={item.index}>
      {item.before && <section><h4>原版 · 第 {item.beforeOrder} 节 · {item.before.title}</h4><p className="evidence-text">{item.before.text}</p>
        {item.before.citations.map((item, index) => <p key={index}>
          <Link to={citationUrl(item)} title={item.quote}>{item.title} · {item.label}</Link>
        </p>)}
      </section>}
      {item.after && <section><h4>新版 · 第 {item.afterOrder} 节 · {item.after.title}</h4><p className="evidence-text">{item.after.text}</p>
        {item.after.citations.map((item, index) => <p key={index}>
          <Link to={citationUrl(item)} title={item.quote}>{item.title} · {item.label}</Link>
        </p>)}
      </section>}
    </li>)}</ol> : <p>正文与核验依据一致。</p>}
  </>;
}

type SourceChoice = { custom: boolean; sources: SourceExcerpt[] };

function RevisionSources({ choice, onChange, disabled, renewing = false }: {
  choice: SourceChoice; onChange: (value: SourceChoice) => void; disabled: boolean; renewing?: boolean;
}) {
  return <>
    <label className="revision-source-choice">
      <input type="checkbox" checked={choice.custom} disabled={disabled}
        onChange={event => onChange({ ...choice, custom: event.target.checked })} />
      {renewing ? '为本次重新核验选择资料' : '为本次修订选择资料'}
    </label>
    {choice.custom && <>
      <p className="model-help">{renewing ? '所选片段将发送给独立复核模型。' : '所选片段将发送给起草与独立复核模型。'}</p>
      <SourcePicker sources={choice.sources} onChange={sources => onChange({ ...choice, sources })} disabled={disabled} allowImport />
    </>}
  </>;
}

export function CourseRevisionDialog({ lesson, version, project, sections, intact, seed, close, onAccepted, onReviewAnchors }: {
  lesson: string; version: number; project: string; sections: VerifiedSection[]; intact: boolean; seed?: RevisionSeed;
  close: () => void; onAccepted: () => void; onReviewAnchors: () => void;
}) {
  const endpoint = `/lessons/${lesson}/revision`;
  const { value, error, refresh } = useRead<Revision>(endpoint);
  const action = useAction();
  const [section, setSection] = useState(seed?.section?.toString() ?? '');
  const [instruction, setInstruction] = useState(seed?.instruction ?? '');
  const [choice, setChoice] = useState<SourceChoice>({ custom: Boolean(seed), sources: seed?.sources ?? [] });
  const [scope, setScope] = useState<Scope>();
  const [reviewScope, setReviewScope] = useState<ReviewScope>();
  const current = value?.id && value.state !== 'accepted' && value.state !== 'discarded' ? value : undefined;
  const anchorImpact = useRead<AnchorList>(current?.state === 'ready' ? endpoint + '/anchor-impact' : null);
  const waiting = current && ['pending', 'running', 'review_pending', 'reviewing'].includes(current.state);
  const sending = current?.state === 'running' || current?.state === 'reviewing';
  const selection = choice.custom ? { sources: sourceLocators(choice.sources) } : {};
  const missingSource = choice.custom && choice.sources.length === 0;
  const request: RevisionRequest = {
    baseVersion: version, section: section ? Number(section) : null, instruction: instruction.trim(), ...selection,
  };
  const renewal = { id: current?.id, ...selection };

  function changeSources(value: SourceChoice) {
    setChoice(value);
    setScope(undefined);
    setReviewScope(undefined);
  }

  useEffect(() => {
    if (!waiting) return;
    const timer = window.setInterval(refresh, 850);
    return () => window.clearInterval(timer);
  }, [waiting, refresh]);

  function afterAction() {
    setScope(undefined);
    setReviewScope(undefined);
    refresh();
  }
  async function start() {
    if (!scope) return;
    await api(endpoint, 'POST', { ...request, confirmation: scope.confirmation });
    afterAction();
  }

  return <Modal title="修订课程" onClose={close} busy={action.pending}>
    <div className="project-dialog-form">
      {!value ? <BackedStatus error={error} retry={refresh} /> : value.state === 'accepted' && !value.seen ? <>
        <p className="notice" role="status">第 {value.publishedVersion} 版已生效，待查看课程更新。</p>
        <p>{value.instruction}</p>
        <CourseChanges changes={value.changes} />
        <div className="action-row">
          <button type="button" className="button secondary" disabled={action.pending} onClick={() => void action.run(async () => {
            await api(endpoint + '/seen', 'POST', { id: value.id });
            refresh();
          })}>标为已知悉</button>
          <button type="button" className="text-action" disabled={action.pending} onClick={onReviewAnchors}>待重新关联</button>
        </div>
        <ErrorNote message={action.error || error} retry={refresh} />
      </> : current ? <>
        <p className="notice" role="status">{STATE_LABELS[current.state] ?? '修订等待处理'}</p>
        <p>基于第 {current.baseVersion} 版 · {current.instruction}</p>
        {current.state === 'ready' && <>
          <CourseChanges changes={current.changes} />
          <h3>关联笔记与复习</h3>
          {!!current.notes?.length && <ul aria-label="关联笔记">{current.notes.map(note => <li key={note.id}>{note.title}</li>)}</ul>}
          {!!current.objectives?.length && <ul aria-label="受影响的复习目标">{current.objectives.map(item => <li key={item.id}>{item.title}</li>)}</ul>}
          <details className="section-verification" open>
            <summary>新版中待重新关联的摘录</summary>
            {anchorImpact.value ? <>
              {anchorImpact.value.unavailable > 0 && <p className="model-help">有 {anchorImpact.value.unavailable} 篇笔记暂无法读取。</p>}
              {anchorImpact.value.items.length ? <ul aria-label="新版中待重新关联的摘录">{anchorImpact.value.items.map(item =>
                <li key={Object.values(item.source).join('/')}>
                  <p className="model-help">第 {item.source.version} 版 · {item.notes.map(note => note.title).join('、')}</p>
                  <blockquote className="evidence-text">{item.quote}</blockquote>
                </li>)}</ul> : <p className="model-help">当前可读笔记中没有待重新关联的摘录。</p>}
              <ErrorNote message={anchorImpact.error} retry={anchorImpact.refresh} />
            </> : <BackedStatus error={anchorImpact.error} retry={anchorImpact.refresh} />}
          </details>
          <p className="model-help">旧版和笔记原文保留。受影响的目标题组等待确认新的依据，已学状态和复习记录保留。</p>
        </>}
        {current.state === 'not_verified' && <ul aria-label="独立核验结论">
          {current.findings?.filter(item => item.state !== 'verified').map(item => <li key={item.index}>
            {verificationLabels[item.state]} · {item.detail ?? item.reason}
          </li>)}
        </ul>}
        <ErrorNote message={action.error || current.error || error} retry={refresh} />
        {current.error && <Link className="text-action" to={projectUrl(project)}>打开课程生成任务</Link>}
        {current.canRenewReview && <RevisionSources choice={choice} onChange={changeSources} disabled={action.pending} renewing />}
        {reviewScope && <section className="project-dialog-form" aria-label="重新核验发送范围">
          <Recipient label="独立复核" value={reviewScope.reviewer} />
          {reviewScope.reviewBackup && <Recipient label="复核备用" value={reviewScope.reviewBackup} />}
          <Passages title="已保存的草稿和上下文" items={reviewScope.content} />
          <EvidenceScope sources={reviewScope.sources} expanded />
          <button type="button" className="button primary" disabled={action.pending} onClick={() => void action.run(async () => {
            await api(`${endpoint}/review-authorize`, 'POST', { ...renewal, confirmation: reviewScope.confirmation });
            afterAction();
          })}>确认并继续核验</button>
        </section>}
        <div className="action-row">
          {current.canRenewReview && !reviewScope && <button type="button" className="button secondary" disabled={action.pending || missingSource}
            onClick={() => void action.run(async () => {
              setReviewScope(await api<ReviewScope>(`${endpoint}/review-preview`, 'POST', renewal));
            })}>预览新的复核范围</button>}
          {current.canContinueRestore && <button type="button" className="button secondary" disabled={action.pending}
            onClick={() => void action.run(async () => {
              await api(`/projects/${project}/generation/control`, 'POST', { action: 'resume' });
              refresh();
            })}>继续恢复的修订</button>}
          <button type="button" className="button secondary" disabled={action.pending || sending} onClick={() => void action.run(async () => {
            await api(`${endpoint}/discard`, 'POST', { id: current.id });
            afterAction();
          })}>保留旧版</button>
          {current.confirmation && <button type="button" className="button primary" disabled={action.pending}
            onClick={() => void action.run(async () => {
              await api(`${endpoint}/accept`, 'POST', { id: current.id, confirmation: current.confirmation });
              onAccepted();
              close();
            })}>接受新版</button>}
        </div>
      </> : !intact ? <p className="notice">本机正文已变更或暂不可用。处理文件后可发起新修订。</p> : <>
        <form className="project-dialog-form" onSubmit={event => {
          event.preventDefault();
          void action.run(async () => {
            setScope(undefined);
            setScope(await api<Scope>(`${endpoint}/preview`, 'POST', request));
          });
        }}>
          <label className="project-field">修订范围
            <select value={section} disabled={action.pending} onChange={event => { setSection(event.target.value); setScope(undefined); }}>
              <option value="">整节课程</option>
              {sections.map(item => <option key={item.index} value={item.index}>{item.title}</option>)}
            </select>
          </label>
          <label className="project-field">修改要求
            <textarea rows={3} maxLength={1200} value={instruction} disabled={action.pending}
              onChange={event => { setInstruction(event.target.value); setScope(undefined); }} placeholder="如补充一个有出处的例子" required />
          </label>
          <RevisionSources choice={choice} onChange={changeSources} disabled={action.pending} />
          <div className="action-row">
            <button className="button secondary" disabled={action.pending || !request.instruction || missingSource}>预览发送范围</button>
          </div>
        </form>
        {scope && <>
          <SendingScope value={scope} />
          <div className="action-row">
            <button type="button" className="button primary" disabled={action.pending} onClick={() => void action.run(start)}>确认并生成修订稿</button>
          </div>
        </>}
        <ErrorNote message={action.error || error} />
      </>}
    </div>
  </Modal>;
}
