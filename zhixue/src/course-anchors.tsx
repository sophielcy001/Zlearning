import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from './api';
import { sourceOffset } from './note-content';
import { BackedStatus, ErrorNote, Modal, useAction, useRead } from './shared';
import type { VerifiedSection } from './verification';

export type PassageAnchor = { section: number; start: number; end: number };
export type PassageSource = PassageAnchor & { version: number };
export type AnchorMatch = { target: PassageAnchor | null; revision: boolean; manual?: boolean };

type Pending = {
  source: PassageSource;
  title: string;
  quote: string;
  notes: { id: string; title: string }[];
};
export type AnchorList = { items: Pending[]; version: number | null; unavailable: number };
type AnchorText = PassageSource & { title: string; quote: string };
type Preview = { source: AnchorText; target: AnchorText; confirmation: string };

function AnchorEditor({ endpoint, version, source, sections, intact, action, onSaved }: {
  endpoint: string; version: number; source: PassageSource; sections: VerifiedSection[]; intact: boolean;
  onSaved: (source: PassageSource, target: PassageSource) => void;
  action: ReturnType<typeof useAction>;
}) {
  const archived = useRead<{ sections: VerifiedSection[] }>(endpoint + '/versions/' + source.version);
  const original = archived.value?.sections.find(item => item.index === source.section);
  const [sectionIndex, setSectionIndex] = useState(() => sections.find(item => item.index === source.section)?.index ?? sections[0]?.index);
  const [target, setTarget] = useState<PassageSource>();
  const [preview, setPreview] = useState<Preview>();
  const selected = sections.find(item => item.index === sectionIndex);
  const quote = original ? [...original.text].slice(source.start, source.end).join('') : '';
  const savePath = endpoint + '/anchor-links';

  function select(start: number, end: number) {
    if (!selected) return;
    const text = selected.text;
    const from = sourceOffset(text, start);
    const to = sourceOffset(text, end);
    const point = text.slice(from, to).trim() ? {
      version, section: selected.index, start: [...text.slice(0, from)].length, end: [...text.slice(0, to)].length,
    } : undefined;
    if (point?.start === target?.start && point?.end === target?.end && point?.section === target?.section) return;
    setPreview(undefined);
    setTarget(point);
  }

  return <>
    {original ? <section className="anchor-card">
      <h3>旧版原文</h3>
      <p className="model-help">第 {source.version} 版 · {original.title}</p>
      <blockquote className="evidence-text">{quote}</blockquote>
    </section> : archived.value ? <p className="notice">找不到旧版选区。</p>
      : <BackedStatus error={archived.error} retry={archived.refresh} />}
    {intact && selected && quote.trim() ? <>
      <label className="project-field">新版小节
        <select value={sectionIndex} disabled={action.pending} onChange={event => {
          setSectionIndex(Number(event.currentTarget.value));
          setTarget(undefined);
          setPreview(undefined);
        }}>
          {sections.map(item => <option key={item.index} value={item.index}>{item.title}</option>)}
        </select>
      </label>
      <label className="project-field">选择新版文字
        <textarea className="anchor-selector" rows={5} readOnly value={selected.text} disabled={action.pending}
          onSelect={event => select(event.currentTarget.selectionStart, event.currentTarget.selectionEnd)} />
      </label>
      <div className="action-row">
        <button type="button" className="button secondary" disabled={action.pending}
          onClick={() => select(0, selected.text.replace(/\r\n/g, '\n').length)}>选中整个小节</button>
        <button type="button" className="button secondary" disabled={action.pending || !target} onClick={() => void action.run(async () => {
          if (!target) return;
          setPreview(undefined);
          setPreview(await api<Preview>(savePath + '/preview', 'POST', { source, target }));
        })}>预览关联</button>
        {target && <span className="model-help" role="status">已选 {target.end - target.start} 字</span>}
      </div>
      {preview && target && <section className="anchor-card" aria-label="关联预览">
        <h3>关联到新版</h3>
        <p className="model-help">第 {preview.target.version} 版 · {preview.target.title}</p>
        <blockquote className="evidence-text">{preview.target.quote}</blockquote>
        <p className="model-help">指向同一旧版选区的链接会定位到所选文字，旧版原文仍可查看。</p>
        <div className="action-row"><button type="button" className="button primary" disabled={action.pending}
          onClick={() => void action.run(async () => {
            const saved = await api<{ target: PassageSource }>(savePath, 'PUT', { source, target, confirmation: preview.confirmation });
            onSaved(source, saved.target);
          })}>确认关联</button></div>
      </section>}
      <ErrorNote message={action.error} />
    </> : !intact && <p className="notice">本机正文已变更或暂不可用。打开可用的正式版本后可以关联新版。</p>}
  </>;
}

export function CourseAnchors({ lesson, version, sections, intact, initial, close, onSaved }: {
  lesson: string; version: number; sections: VerifiedSection[]; intact: boolean; initial?: PassageSource;
  close: () => void; onSaved: (source: PassageSource, target: PassageSource) => void;
}) {
  const endpoint = '/lessons/' + lesson;
  const [source, setSource] = useState(initial);
  const action = useAction();
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  const list = useRead<AnchorList>(source ? null : endpoint + '/anchor-links');
  return <Modal title={source ? '重新关联摘录' : '待重新关联'} onClose={close} busy={action.pending}>
    <div className="project-dialog-form course-anchors">
      {source ? <>
        {!initial && <button type="button" className="text-action anchor-back" disabled={action.pending}
          onClick={() => setSource(undefined)}>返回待关联清单</button>}
        <AnchorEditor key={version} endpoint={endpoint} version={version} source={source} sections={sections}
          intact={intact} action={action} onSaved={(origin, target) => { if (mounted.current) onSaved(origin, target); }} />
      </> : list.value ? <>
        <ErrorNote message={list.error} retry={list.refresh} />
        {list.value.unavailable > 0 && <p className="model-help">有 {list.value.unavailable} 篇笔记暂无法读取。</p>}
        {list.value.version !== version ? <p className="notice">课程已换版。请刷新后查看待关联清单。</p>
          : list.value.items.length ? <ul className="anchor-list">
            {list.value.items.map(item => <li className="anchor-card" key={Object.values(item.source).join('/')}>
              <h3>{item.title} · 第 {item.source.version} 版</h3>
              <blockquote className="evidence-text">{item.quote}</blockquote>
              <div className="action-row" aria-label="引用笔记">
                {item.notes.map(note => <Link className="text-action" key={note.id} to={'/library?file=note-' + note.id}>{note.title}</Link>)}
              </div>
              <div className="action-row"><button type="button" className="button secondary"
                onClick={() => setSource(item.source)}>重新关联此摘录</button></div>
            </li>)}
          </ul> : <p className="notice" role="status">当前没有待重新关联的已保存摘录。</p>}
      </> : <BackedStatus error={list.error} retry={list.refresh} />}
    </div>
  </Modal>;
}
