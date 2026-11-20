import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { dateLabel } from './api';
import type { RemediationCard } from './assessment-types';
import { chatUrl, type CourseSelection } from './conversation';
import { ExcerptButton, focusedExcerptControl, selectedRange, type ExcerptActions } from './course-excerpt-control';
import { verifiedExcerpt } from './course-excerpt';
import { CourseAnchors, type AnchorMatch, type PassageAnchor, type PassageSource } from './course-anchors';
import { CourseRevisionDialog, type RevisionSeed } from './course-revision';
import { CourseSourceUpdateDialog } from './course-source-update';
import { LectureMarkdown } from './lecture-markdown';
import { maxNoteLength } from './note-content';
import { BackedStatus, ErrorNote, Markdown, Modal, useRead } from './shared';
import { citationUrl, verificationLabels, type Citation, type Verification, type VerifiedSection } from './verification';
import { WikiText } from './wiki-text';

export function EvidenceLinks({ entries }: { entries: Citation[] }) {
  if (entries.length === 0) return null;
  return <ul className="evidence-links">{entries.map((reference, index) => <li key={`${reference.fileId}/${reference.at}/${index}`}>
    <Link to={citationUrl(reference)} title={reference.quote}>{reference.title} · {reference.label}</Link>
    <details><summary>查看引文</summary><blockquote>{reference.quote}</blockquote></details>
  </li>)}</ul>;
}

function numberParam(params: URLSearchParams, name: string) {
  const raw = params.get(name);
  if (raw === null || !/^\d+$/.test(raw)) return undefined;
  const number = Number(raw);
  return Number.isSafeInteger(number) ? number : undefined;
}

function VersionHistory({ lesson, initial, anchor, needsReassociation, onReassociate, close }: {
  lesson: string; initial: number; anchor?: PassageAnchor; needsReassociation?: boolean;
  onReassociate?: () => void; close: () => void;
}) {
  const versions = useRead<{ items: { version: number; publishedAt: string; revision: string }[] }>(`/lessons/${lesson}/versions`);
  const [number, setNumber] = useState(initial);
  const file = useRead<{ markdown: string; sections: VerifiedSection[] }>(`/lessons/${lesson}/versions/${number}`);
  const passage = number === initial && anchor ? file.value?.sections.find(section => section.index === anchor.section) : undefined;
  return <Modal title="已保存课程版本" onClose={close}>
    {needsReassociation && <div className="notice action-row">
      <span role="status">新版未找到可唯一定位的同一原文，待重新关联。</span>
      {onReassociate && <button type="button" className="text-action" onClick={onReassociate}>选择新版位置</button>}
    </div>}
    {versions.value ? <label className="project-field">课程版本
      <select value={number} onChange={event => setNumber(Number(event.target.value))}>
        {versions.value.items.map(item => <option key={item.version} value={item.version}>第 {item.version} 版</option>)}
      </select>
    </label> : <BackedStatus error={versions.error} retry={versions.refresh} />}
    <div className="course-version-preview">
      {file.value ? passage ? <>
        <h3>{passage.title}</h3>
        <p className="evidence-text"><WikiText text={passage.text} highlight={anchor} /></p>
        <details className="section-verification"><summary>查看完整版本</summary><Markdown>{file.value.markdown}</Markdown></details>
      </> : <Markdown>{file.value.markdown}</Markdown> : <BackedStatus error={file.error} retry={file.refresh} />}
    </div>
  </Modal>;
}

function RemediationMarker({ lesson, title, cards, requested, clear }: {
  lesson: string; title: string; cards: RemediationCard[]; requested: string | null; clear: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const description = useId();
  const opened = expanded || Boolean(requested);
  return <div className="remediation-marker" onMouseEnter={() => setDismissed(false)}>
    <button type="button" className="remediation-button"
      aria-label={`错因补学 · ${title} · ${cards.length} 张`} aria-haspopup="dialog" aria-describedby={description}
      onFocus={() => setDismissed(false)} onClick={() => setExpanded(true)}
      onKeyDown={event => {
        if (event.key === 'Escape') setDismissed(true);
      }}>错因补学 <span>{cards.length}</span></button>
    {!opened && !dismissed && <div className="panel remediation-preview" id={description} role="tooltip">
      <ul>{cards.map(card => <li key={card.id}>
        <strong>第 {card.round} 轮 · {dateLabel(card.scoredOn)}</strong>
        <span>{card.entries[0]?.cause}</span>
      </li>)}</ul>
    </div>}
    {opened && <Modal title="错因补学" onClose={() => { setExpanded(false); setDismissed(true); if (requested) clear(); }}>
      <p className="model-help">{title}</p>
      <div className="remediation-cards">
        {cards.map(card => <article className={`panel remediation-card ${card.id === requested ? 'remediation-target' : ''}`} key={card.id}>
          <h3>第 {card.round} 轮 · {dateLabel(card.scoredOn)}</h3>
          <p className="model-help">关联课程第 {card.courseVersion} 版</p>
          {card.sourcesChanged && <p className="notice">原核验依据已变更或移除。</p>}
          {card.entries.map(entry => <section key={entry.index}>
            <h4>{entry.question}</h4>
            <dl><dt>错因</dt><dd className="evidence-text">{entry.cause}</dd>
              <dt>补学讲解</dt><dd className="evidence-text">{entry.explanation}</dd>
            </dl>
          </section>)}
          <details><summary>核验依据</summary><EvidenceLinks entries={card.citations} /></details>
          <div className="action-row">
            <Link className="text-action" to={`/library?${new URLSearchParams({ file: card.fileId, revision: card.revision })}`}>在知识库打开讲解</Link>
            <Link className="text-action" to={`/reviews/model/${lesson}/${card.objectiveKey}?group=${card.assessmentId}`}>查看当轮评价</Link>
          </div>
        </article>)}
      </div>
    </Modal>}
  </div>;
}

function readPassage(range: Range | undefined, version: number): CourseSelection | undefined {
  if (!range) return undefined;
  const node = range.startContainer;
  const paragraph = (node instanceof Element ? node : node.parentElement)?.closest<HTMLElement>('p[data-course-section]');
  if (!paragraph || !paragraph.contains(range.endContainer)) return undefined;
  const text = range.toString();
  if (!text.trim() || text.length > maxNoteLength) return undefined;
  const prefix = range.cloneRange();
  prefix.selectNodeContents(paragraph);
  prefix.setEnd(range.startContainer, range.startOffset);
  const start = Array.from(prefix.toString()).length;
  return { version, sectionIndex: Number(paragraph.dataset.courseSection), start, end: start + Array.from(text).length, text };
}

export function ModelCourseBody({ lesson, project, body, version, intact, revision, excerpts, onQuestion, onPrerequisite, onReadingBody, onRevised }: {
  lesson: string; project: string; body: string; version: number; intact: boolean; revision: string | null; excerpts: ExcerptActions;
  onQuestion: (selection: CourseSelection) => void; onPrerequisite: (topic: string) => void;
  onReadingBody: (body: HTMLDivElement | null) => void;
  onRevised: () => void;
}) {
  const record = useRead<Verification>(`/lessons/${lesson}/verification?publishedVersion=${version}`);
  const remediation = useRead<{ cards: RemediationCard[] }>(intact ? `/lessons/${lesson}/remediation?version=${version}` : null);
  const [params, setParams] = useSearchParams();
  const [versions, setVersions] = useState(false);
  const [originalOpen, setOriginalOpen] = useState(false);
  const [anchorDialog, setAnchorDialog] = useState<{ source?: PassageSource }>();
  const [recentAnchor, setRecentAnchor] = useState<{ source: PassageSource; target: PassageSource; previous?: AnchorMatch }>();
  const [revising, setRevising] = useState(false);
  const [sourceUpdate, setSourceUpdate] = useState(false);
  const [revisionSeed, setRevisionSeed] = useState<RevisionSeed>();
  const update = params.get('courseUpdate');
  const requestedSourceUpdate = params.has('sourceUpdate');
  const showingSourceUpdate = sourceUpdate || requestedSourceUpdate;
  const [passage, setPassage] = useState<CourseSelection>();
  const draggingPassage = useRef(false);
  const reading = useRef<HTMLDivElement>(null);
  const verification = record.value;
  const refreshVerification = record.refresh;
  useEffect(() => {
    function refreshVisible() {
      if (document.visibilityState === 'visible') refreshVerification();
    }
    const timer = window.setInterval(refreshVisible, 45_000);
    window.addEventListener('focus', refreshVisible);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener('focus', refreshVisible);
    };
  }, [refreshVerification]);
  const hasDispute = verification?.sections.some(section => section.state !== 'verified');
  const requested = params.get('supplement');
  const selected = remediation.value?.cards.find(card => card.id === requested);
  const structured = intact && verification?.publishedVersion === version && Boolean(verification.published.length);
  const bodyReady = !intact || verification !== undefined;
  const readingRef = useCallback((element: HTMLDivElement | null) => {
    reading.current = element;
    onReadingBody(bodyReady ? element : null);
  }, [bodyReady, onReadingBody]);
  const requestedVersion = numberParam(params, 'readingVersion');
  const sectionIndex = numberParam(params, 'section');
  const start = numberParam(params, 'selectionStart');
  const end = numberParam(params, 'selectionEnd');
  const passageAnchor = sectionIndex !== undefined && start !== undefined && end !== undefined ? { section: sectionIndex, start, end } : undefined;
  const anchorSource = requestedVersion && passageAnchor ? { ...passageAnchor, version: requestedVersion } : undefined;
  const archivedLink = Boolean(requestedVersion && (requestedVersion !== version || !intact));
  const relocationPath = archivedLink && intact && passageAnchor
    ? `/lessons/${lesson}/revision-anchor?fromVersion=${requestedVersion}&version=${version}&section=${sectionIndex}&start=${start}&end=${end}`
    : null;
  const relocation = useRead<AnchorMatch>(relocationPath);
  const justSaved = recentAnchor && anchorSource && recentAnchor.target.version === version
    && recentAnchor.source.version === anchorSource.version && recentAnchor.source.section === anchorSource.section
    && recentAnchor.source.start === anchorSource.start && recentAnchor.source.end === anchorSource.end;
  const waitingForMatch = justSaved && !relocation.error && (!relocation.value || relocation.value === recentAnchor.previous);
  const match = relocationPath ? (waitingForMatch ? { target: recentAnchor.target, revision: true, manual: true } : relocation.value) : undefined;
  const moved = match?.target;
  const historical = archivedLink && (!relocationPath || (!!relocation.error && !moved) || moved === null);
  const oldPassage = historical || Boolean(originalOpen && anchorSource);
  useEffect(() => {
    if (recentAnchor && !waitingForMatch) setRecentAnchor(undefined);
  }, [recentAnchor, waitingForMatch]);
  const canLink = intact && structured && !!anchorSource;
  const currentLink = (requestedVersion === version && intact) || !!moved;
  const linkedPassage = moved ?? (currentLink ? passageAnchor : undefined);
  const anchor = selected?.sectionIndex ?? (currentLink ? linkedPassage?.section ?? sectionIndex : undefined);
  useEffect(() => {
    if (!structured || anchor === undefined && !currentLink) return;
    const element = anchor === undefined ? reading.current : document.getElementById(`course-proof-section-${anchor}`);
    (element?.querySelector('mark') ?? element)?.scrollIntoView({ block: 'center' });
  }, [anchor, currentLink, linkedPassage?.start, linkedPassage?.end, structured]);
  useEffect(() => {
    if (!structured) return;
    function capture() {
      const next = readPassage(selectedRange(reading.current), version);
      if (next || (!draggingPassage.current && !focusedExcerptControl())) setPassage(next);
    }
    document.addEventListener('selectionchange', capture);
    return () => document.removeEventListener('selectionchange', capture);
  }, [structured, version]);
  function clear(...keys: string[]) {
    const next = new URLSearchParams(params);
    for (const key of keys) next.delete(key);
    setParams(next, { replace: true });
  }
  function showAnchors(source?: PassageSource) {
    setAnchorDialog({ source });
    setOriginalOpen(false);
    setVersions(false);
    setRevising(false);
    if (update) clear('courseUpdate');
  }
  function didAssociate(source: PassageSource, target: PassageSource) {
    setRecentAnchor({ source, target, previous: relocation.value });
    setAnchorDialog(undefined);
    setParams(new URLSearchParams({
      readingVersion: String(source.version), section: String(source.section),
      selectionStart: String(source.start), selectionEnd: String(source.end),
    }), { replace: true });
    relocation.refresh();
  }
  const cardsBySection = new Map<number, RemediationCard[]>();
  for (const card of remediation.value?.cards ?? []) {
    const entries = cardsBySection.get(card.sectionIndex) ?? [];
    entries.push(card);
    cardsBySection.set(card.sectionIndex, entries);
  }
  function clearPassage() {
    window.getSelection()?.removeAllRanges();
    setPassage(undefined);
  }

  return <div className="course-evidence" ref={readingRef} onDragStart={event => {
    if (!structured) return;
    const range = selectedRange(reading.current);
    if (!range || !(event.target instanceof Node) || !range.intersectsNode(event.target)) return;
    const selected = readPassage(range, version);
    const section = verification?.published.find(entry => entry.index === selected?.sectionIndex);
    if (selected && section) excerpts.drag(event, verifiedExcerpt(lesson, selected, section.text));
  }} onDragEnd={excerpts.endDrag}>
    <div className="action-row course-evidence-actions">
      <button type="button" className="text-action" onClick={() => setVersions(true)}>课程版本 · 第 {version} 版</button>
      {verification && <button type="button" className="text-action" onClick={() => setRevising(true)}>修订课程</button>}
      {version > 1 && verification && <button type="button" className="text-action" onClick={() => showAnchors()}>待重新关联</button>}
      {hasDispute && <Link className="text-action" to={`/disputes?lesson=${lesson}`}>查看课程争议</Link>}
    </div>
    {match?.manual && moved && anchorSource && <div className="notice action-row">
      <span role="status">此摘录已关联到第 {version} 版。</span>
      <button type="button" className="text-action" onClick={() => setOriginalOpen(true)}>查看旧版原文</button>
      {canLink && <button type="button" className="text-action" onClick={() => showAnchors(anchorSource)}>更改关联</button>}
    </div>}
    {!intact && <div className="notice" role="status">{body ? '本地课程文件已有改动。核验记录仍对应保存的版本。' : '本机课程文件当前不可用，可查看已保存的版本。'}</div>}
    {verification?.sourcesChanged && <div className="notice action-row" role="status">
      <span>部分引文依据已变更或移除。</span>
      <button type="button" className="text-action" onClick={() => setSourceUpdate(true)}>核对来源变更</button>
    </div>}
    <ErrorNote message={record.error} retry={record.refresh} />
    <ErrorNote message={remediation.error} retry={remediation.refresh} />
    {requested && (!intact || remediation.value && !selected) && <div className="notice" role="status">这张补学卡在当前正文中未找到有效锚点。</div>}
    {structured ? verification.published.map(section => <section id={`course-proof-section-${section.index}`} key={section.index} className="course-section">
      <div className="course-anchor-heading"><h2 data-reading-block>{section.title}</h2>
        {cardsBySection.has(section.index) && <RemediationMarker lesson={lesson} title={section.title}
          cards={cardsBySection.get(section.index)!} requested={selected?.sectionIndex === section.index ? selected.id : null} clear={() => clear('supplement')} />}
      </div>
      {section.state !== 'verified' && <p className="notice" role="note">用户采纳，尚未核验通过</p>}
      <p className="evidence-text" data-reading-block data-course-section={section.index}>
        <WikiText text={section.text} highlight={linkedPassage?.section === section.index ? linkedPassage : undefined} />
      </p>
      {passage?.version === version && passage.sectionIndex === section.index && <div className="action-row course-selection-control">
        {Array.from(passage.text).length <= 2400 && <button type="button" className="button soft"
          onMouseDown={event => event.preventDefault()} onClick={() => { onQuestion(passage); clearPassage(); }}>问助手</button>}
        <ExcerptButton onClick={() => {
          excerpts.add(verifiedExcerpt(lesson, passage, section.text));
          clearPassage();
        }} onDrag={event => {
          draggingPassage.current = true;
          excerpts.drag(event, verifiedExcerpt(lesson, passage, section.text));
        }} onDragEnd={() => {
          draggingPassage.current = false;
          excerpts.endDrag();
          clearPassage();
        }} onBlur={() => {
          if (!draggingPassage.current) setPassage(readPassage(selectedRange(reading.current), version));
        }} />
        {[...passage.text.trim()].length <= 160 && <button type="button" className="button soft" aria-haspopup="dialog"
          onMouseDown={event => event.preventDefault()} onClick={() => {
            onPrerequisite(passage.text.trim());
            clearPassage();
          }}>补讲所选前置</button>}
      </div>}
      <details className="section-verification">
        <summary>{section.state === 'verified' ? '来源与独立复核记录' : verificationLabels[section.state]}</summary>
        {section.consolidation && <p className="model-help">整理自 {section.consolidation.questions.length} 轮课内问答
          {' · 独立复核 '}{section.consolidation.reviewer.name} · {section.consolidation.reviewer.model}</p>}
        {section.consolidation?.conversations?.length === 1 && <Link className="text-action" to={chatUrl(section.consolidation.conversations[0])}>打开原课内对话</Link>}
        <p className="model-help">{section.reason}</p>
        <EvidenceLinks entries={section.citations} />
      </details>
    </section>) : <LectureMarkdown lesson={lesson} body={body} revision={bodyReady || record.error ? revision : null} excerpts={excerpts} />}
    {!anchorDialog && !showingSourceUpdate && !(revising || update) && (versions || oldPassage) &&
      <VersionHistory key={oldPassage ? requestedVersion : version} lesson={lesson}
        initial={oldPassage ? requestedVersion! : version} anchor={oldPassage ? passageAnchor : undefined}
        needsReassociation={Boolean(historical && match?.revision && match.target === null)}
        onReassociate={historical && canLink ? () => showAnchors(anchorSource) : undefined}
        close={() => {
          setVersions(false);
          setOriginalOpen(false);
          if (historical) clear('readingVersion', 'section', 'selectionStart', 'selectionEnd');
        }} />}
    {anchorDialog && verification && <CourseAnchors lesson={lesson} version={version} sections={verification.published}
      intact={intact} initial={anchorDialog.source} close={() => setAnchorDialog(undefined)} onSaved={didAssociate} />}
    {!anchorDialog && showingSourceUpdate && verification && <CourseSourceUpdateDialog lesson={lesson} version={version} intact={intact}
      close={() => { setSourceUpdate(false); if (requestedSourceUpdate) clear('sourceUpdate'); record.refresh(); }}
      onRevise={seed => {
        setRevisionSeed(seed);
        setSourceUpdate(false);
        clear('sourceUpdate', 'courseUpdate');
        setRevising(true);
      }} />}
    {!anchorDialog && !showingSourceUpdate && (revising || update) && verification && <CourseRevisionDialog lesson={lesson} project={project} version={version}
      sections={verification.published} intact={intact} seed={revisionSeed} onReviewAnchors={() => showAnchors()}
      close={() => { setRevisionSeed(undefined); setRevising(false); if (update) clear('courseUpdate'); }} onAccepted={onRevised} />}
  </div>;
}
