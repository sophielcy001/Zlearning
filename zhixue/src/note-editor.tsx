import { Extension, type Editor, type JSONContent } from '@tiptap/core';
import Mathematics from '@tiptap/extension-mathematics';
import { Markdown } from '@tiptap/markdown';
import { NodeSelection, Plugin } from '@tiptap/pm/state';
import { EditorContent, useEditor, useEditorState } from '@tiptap/react';
import StarterKit from '@tiptap/starter-kit';
import { Bold, Code2, Heading2, Italic, Link2, List, ListOrdered, Quote, Sigma, Sparkles } from 'lucide-react';
import { Marked, type marked } from 'marked';
import { useLayoutEffect, useMemo, useRef, useState, type FormEvent, type Ref } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import type { WikiTarget } from './api';
import { libraryLinkUrl } from './library-views';
import { isCourseSourceRoute, workspaceMarkdownRoute } from './markdown-links';
import type { NoteAISelection } from './note-ai';
import { richAISelection, sourceAISelection } from './note-ai-selection';
import { noteCheckHighlights, NoteCheckMarks, noteDecorations, noteFindingRanges, type FindingRange } from './note-check-marks';
import type { NoteFinding } from './note-checks';
import { maxNoteLength, sourceOffset } from './note-content';
import { NoteCodeBlock, NoteLink, readRichNote, serializeRichNote, WikiReference } from './note-markdown';
import { ErrorNote } from './shared';
import { useWikiCompletion } from './wiki-links-ui';
import { sourceWikiDraft, stableWikiValue, wikiDraft, type WikiDraft } from './wiki-markdown';
import 'katex/dist/katex.min.css';

type Mode = 'rich' | 'source';
type Formula = { kind: 'formula'; latex: string; block: boolean; position?: number };
type LinkForm = { kind: 'link'; href: string };
type Insertion = Formula | LinkForm;

function linkAllowed(href: string) {
  if (workspaceMarkdownRoute(href)) return true;
  try { return ['https:', 'http:'].includes(new URL(href).protocol); }
  catch { return false; }
}

const emptyDocument: JSONContent = { type: 'doc', content: [{ type: 'paragraph' }] };
const NoteMarkdown = Markdown.extend({
  addOptions() {
    // Tiptap registers tokenizers on the instance. Each editor gets its own lexer.
    return { ...this.parent?.(), marked: new Marked() as unknown as typeof marked };
  },
});

function richWikiDraft(editor: Editor | null) {
  if (!editor || editor.isActive('code') || editor.isActive('codeBlock') || editor.isActive('link')) return undefined;
  const { $from: cursor, empty } = editor.state.selection;
  if (!empty || !cursor.parent.isTextblock) return undefined;
  const before = cursor.parent.textBetween(0, cursor.parentOffset, '', '\ufffc');
  const after = cursor.parent.textBetween(cursor.parentOffset, Math.min(cursor.parentOffset + 2, cursor.parent.content.size), '', '\ufffc');
  return wikiDraft(before, after, cursor.pos);
}

function SourceMarks({ text, ranges, backdrop }: { text: string; ranges: FindingRange[]; backdrop: Ref<HTMLDivElement> }) {
  const joined: { start: number; end: number }[] = [];
  for (const { start, end } of [...ranges].sort((a, b) => a.start - b.start)) {
    const last = joined.at(-1);
    if (last && start <= last.end) last.end = Math.max(last.end, end);
    else joined.push({ start, end });
  }
  let tail = 0;
  return <div className="note-source-backdrop" aria-hidden="true" ref={backdrop}>
    {joined.map(({ start, end }) => {
      const before = text.slice(tail, start);
      tail = end;
      return <span key={start}>{before}<mark data-note-check-start={start} data-note-check-end={end}>{text.slice(start, end)}</mark></span>;
    })}{text.slice(tail) + '\n'}
  </div>;
}

export default function NoteEditor({ content, disabled, onChange, label, onAI, findings, location }: {
  content: string; disabled: boolean; label: string; onChange: (content: string) => void;
  onAI?: (selected?: NoteAISelection) => void;
  findings: readonly NoteFinding[];
  location?: { id: string };
}) {
  const navigate = useNavigate();
  const routeLocation = useLocation();
  const [mode, setMode] = useState<Mode>('rich');
  const [sourceOnly, setSourceOnly] = useState(false);
  const [limitError, setLimitError] = useState('');
  const [insertion, setInsertion] = useState<Insertion>();
  const [insertionError, setInsertionError] = useState('');
  const [focused, setFocused] = useState(false);
  const [sourceCaret, setSourceCaret] = useState<number>();
  const [hasSourceSelection, setHasSourceSelection] = useState(false);
  const source = useRef<HTMLTextAreaElement>(null);
  const completedLinkCaret = useRef<{ content: string; offset: number } | null>(null);
  const sourceBackdrop = useRef<HTMLDivElement>(null);
  const located = useRef<{ id: string } | undefined>(undefined);
  const reflected = useRef<string | undefined>(undefined);
  const baseline = useRef<{ text: string; serialized: string } | undefined>(undefined);

  const extensions = useMemo(() => [
    StarterKit.configure({
      codeBlock: false, underline: false, trailingNode: false, link: false,
    }),
    NoteCodeBlock,
    NoteCheckMarks,
    NoteLink.configure({ openOnClick: false, autolink: false, isAllowedUri: linkAllowed }),
    WikiReference,
    Mathematics.configure({
      katexOptions: { throwOnError: false, trust: false },
      inlineOptions: { onClick: (node, position) => {
        setInsertionError('');
        setInsertion({ kind: 'formula', latex: node.attrs.latex, block: false, position });
      } },
      blockOptions: { onClick: (node, position) => {
        setInsertionError('');
        setInsertion({ kind: 'formula', latex: node.attrs.latex, block: true, position });
      } },
    }),
    NoteMarkdown,
    Extension.create({
      name: 'noteLengthLimit',
      addProseMirrorPlugins() {
        const editor = this.editor;
        return [new Plugin({
          filterTransaction(transaction) {
            if (!transaction.docChanged) return true;
            const output = serializeRichNote(editor, transaction.doc.toJSON());
            if (output.length <= maxNoteLength) {
              setLimitError('');
              return true;
            }
            setLimitError('笔记最多可保存 50,000 个字符。请分段编辑。');
            return false;
          },
        })];
      },
    }),
  ], []);

  const editor = useEditor({
    extensions,
    content: emptyDocument,
    immediatelyRender: false,
    injectCSS: false,
    shouldRerenderOnTransaction: false,
    editorProps: {
      attributes: {
        'aria-label': label,
        'aria-multiline': 'true',
        role: 'textbox',
      },
      handleDOMEvents: {
        click: (_view, event) => {
          const link = event.target instanceof Element && event.target.closest('a');
          if (!link) return false;
          event.preventDefault();
          const href = link.getAttribute('href') ?? '';
          const route = workspaceMarkdownRoute(href);
          const open = event.metaKey || event.ctrlKey || event.detail === 0;
          if (route && (open || link.hasAttribute('data-wiki-reference') || isCourseSourceRoute(route))) {
            navigate(libraryLinkUrl(route, routeLocation));
          } else if (open && linkAllowed(href)) {
            window.open(href, '_blank', 'noopener,noreferrer');
          }
          return true;
        },
      },
    },
    onUpdate: ({ editor }) => {
      setInsertion(undefined);
      const serialized = serializeRichNote(editor);
      const output = baseline.current?.serialized === serialized ? baseline.current.text : serialized;
      reflected.current = output;
      onChange(output);
    },
  });

  useLayoutEffect(() => {
    if (!editor || mode !== 'rich') return;
    if (content === reflected.current) {
      setSourceOnly(false);
      return;
    }
    const document = readRichNote(editor, content);
    try {
      if (!document) throw new Error('Markdown cannot be represented in the rich editor');
      editor.chain().setContent(document.content?.length ? document : emptyDocument,
        { emitUpdate: false, errorOnInvalidContent: true }).setMeta('addToHistory', false).run();
      reflected.current = content;
      baseline.current = { text: content, serialized: serializeRichNote(editor) };
      setSourceOnly(false);
    } catch {
      setSourceOnly(true);
      setMode('source');
    }
  }, [editor, content, mode]);

  useLayoutEffect(() => {
    editor?.setEditable(!disabled, false);
  }, [editor, disabled]);

  useLayoutEffect(() => {
    const pending = completedLinkCaret.current;
    if (!pending) return;
    completedLinkCaret.current = null;
    const field = source.current;
    // Move the caret as the new text is committed, before another edit can change the selection.
    if (field && field === document.activeElement && pending.content === content) {
      field.setSelectionRange(pending.offset, pending.offset);
      rememberCaret(field);
    }
  }, [content, mode]);

  const ranges = useMemo(() => noteFindingRanges(content, findings), [content, findings]);
  useLayoutEffect(() => {
    if (mode === 'source' && source.current && sourceBackdrop.current) {
      sourceBackdrop.current.scrollTop = source.current.scrollTop;
    }
  }, [mode, ranges]);
  useLayoutEffect(() => {
    if (!editor || mode !== 'rich' || reflected.current !== content) return;
    const { state } = editor;
    editor.view.dispatch(state.tr.setMeta(noteCheckHighlights, noteDecorations(state.doc, content, ranges)));
  }, [editor, mode, content, ranges]);

  useLayoutEffect(() => {
    if (disabled || !location || located.current === location) return;
    const range = ranges.find(item => item.finding.id === location.id);
    if (!range) return;
    if (mode !== 'source') {
      setMode('source');
      return;
    }
    const field = source.current;
    if (!field) return;
    located.current = location;
    const offset = (index: number) => content.slice(0, index).replace(/\r\n/g, '\n').length;
    field.focus({ preventScroll: true });
    field.setSelectionRange(offset(range.start), offset(range.end));
    const backdrop = sourceBackdrop.current;
    const mark = Array.from(backdrop?.querySelectorAll<HTMLElement>('mark') ?? []).find(node =>
      Number(node.dataset.noteCheckStart) <= range.start && Number(node.dataset.noteCheckEnd) >= range.end);
    if (mark && backdrop) {
      field.scrollTop = Math.max(0, mark.offsetTop - field.clientHeight / 2);
      backdrop.scrollTop = field.scrollTop;
    }
    field.scrollIntoView({ block: 'nearest' });
    setSourceCaret(undefined);
    setHasSourceSelection(true);
  }, [content, ranges, disabled, location, mode]);

  const state = useEditorState({
    editor,
    selector: ({ editor }) => ({
      bold: editor?.isActive('bold'),
      italic: editor?.isActive('italic'),
      heading: editor?.isActive('heading', { level: 2 }),
      quote: editor?.isActive('blockquote'),
      bullet: editor?.isActive('bulletList'),
      ordered: editor?.isActive('orderedList'),
      code: editor?.isActive('codeBlock'),
      link: editor?.isActive('link'),
      linkTarget: editor?.getAttributes('link').href as string | undefined,
      selection: editor?.state.selection.empty === false,
      wiki: richWikiDraft(editor),
    }),
  });

  const sourceDraft = useMemo(() => mode === 'source' && focused && sourceCaret !== undefined
    ? sourceWikiDraft(content, sourceOffset(content, sourceCaret)) : undefined, [mode, focused, sourceCaret, content]);
  const draft = focused && !disabled && !insertion ? (mode === 'source' ? sourceDraft : state?.wiki) : undefined;
  const completion = useWikiCompletion(draft, insertWiki);

  function rememberCaret(field: HTMLTextAreaElement) {
    setSourceCaret(field.selectionStart === field.selectionEnd ? field.selectionStart : undefined);
    setHasSourceSelection(field.selectionStart !== field.selectionEnd);
  }

  function insertWiki(entry: WikiTarget, range: WikiDraft) {
    const target = stableWikiValue(entry);
    if (mode === 'rich') {
      editor?.chain().focus().insertContentAt({ from: range.from, to: range.to }, {
        type: 'wikiReference', attrs: { target },
      }).run();
      return;
    }
    const link = '[[' + target + ']]';
    const next = content.slice(0, range.from) + link + content.slice(range.to);
    if (next.length > maxNoteLength) {
      setLimitError('笔记最多可保存 50,000 个字符。请分段编辑。');
      return;
    }
    setLimitError('');
    const offset = next.slice(0, range.from + link.length).replace(/\r\n/g, '\n').length;
    completedLinkCaret.current = { content: next, offset };
    onChange(next);
  }

  function chooseMode(next: Mode) {
    setFocused(false);
    if (next !== mode) setHasSourceSelection(false);
    completion.reset();
    setMode(next);
    setInsertion(undefined);
    setInsertionError('');
    setLimitError('');
  }

  function openSelectionAI() {
    try {
      const selection = mode === 'source' && source.current
        ? sourceAISelection(content, source.current) : richAISelection(editor, content);
      if (selection) onAI?.(selection);
    } catch {
      setLimitError('暂时无法读取这段 Markdown，请重新选择。');
    }
  }

  function openFormula() {
    const selection = editor?.state.selection;
    const selectedMath = selection instanceof NodeSelection && ['inlineMath', 'blockMath'].includes(selection.node.type.name);
    setInsertionError('');
    setInsertion(selectedMath ? {
      kind: 'formula', block: selection.node.type.name === 'blockMath', latex: selection.node.attrs.latex, position: selection.from,
    } : { kind: 'formula', latex: '', block: false });
  }

  function applyInsertion(event: FormEvent) {
    event.preventDefault();
    if (!editor || !insertion) return;
    const chain = editor.chain().focus();
    if (insertion.kind === 'link') {
      const href = insertion.href.trim();
      if (!linkAllowed(href)) {
        setInsertionError('请输入 http 或 https 链接。');
        return;
      }
      const address = workspaceMarkdownRoute(href) ? href : new URL(href).href;
      if (!chain.extendMarkRange('link').setLink({ href: address }).run()) return;
    } else {
      const latex = insertion.latex.trim();
      if (!latex || latex.includes('$') || (!insertion.block && /[\r\n]/.test(latex))) {
        setInsertionError('请输入 LaTeX 内容。公式定界符由编辑器添加。');
        return;
      }
      const { block, position } = insertion;
      if (position !== undefined) {
        if (block) chain.updateBlockMath({ latex, pos: position }).run();
        else chain.updateInlineMath({ latex, pos: position }).run();
      } else if (block) chain.insertBlockMath({ latex }).run();
      else chain.insertInlineMath({ latex }).run();
    }
    setInsertion(undefined);
    setInsertionError('');
  }

  const controls = [
    { label: '标题', Icon: Heading2, active: state?.heading, apply: () => editor?.chain().focus().toggleHeading({ level: 2 }).run() },
    { label: '加粗', Icon: Bold, active: state?.bold, apply: () => editor?.chain().focus().toggleBold().run() },
    { label: '斜体', Icon: Italic, active: state?.italic, apply: () => editor?.chain().focus().toggleItalic().run() },
    { label: '项目列表', Icon: List, active: state?.bullet, apply: () => editor?.chain().focus().toggleBulletList().run() },
    { label: '编号列表', Icon: ListOrdered, active: state?.ordered, apply: () => editor?.chain().focus().toggleOrderedList().run() },
    { label: '引用', Icon: Quote, active: state?.quote, apply: () => editor?.chain().focus().toggleBlockquote().run() },
    { label: '代码块', Icon: Code2, active: state?.code, apply: () => editor?.chain().focus().toggleCodeBlock().run() },
  ];

  return <div className="note-editor" onKeyDownCapture={event => {
    if (event.target instanceof HTMLElement && event.target.matches('.tiptap, .note-source')) completion.onKeyDown(event);
  }} onFocus={event => {
    setFocused(event.target instanceof HTMLElement && event.target.matches('.tiptap, .note-source'));
  }} onBlur={() => { setFocused(false); completion.reset(); }}>
    <div className="note-editor-modes" role="group" aria-label="笔记编辑模式">
      <button type="button" aria-pressed={mode === 'rich'} onClick={() => chooseMode('rich')}>富文本</button>
      <button type="button" aria-pressed={mode === 'source'} onClick={() => chooseMode('source')}>Markdown 原文</button>
    </div>
    {onAI && <div className="note-editor-ai" role="group" aria-label="笔记 AI">
      <button type="button" className="text-action" disabled={disabled} aria-haspopup="dialog"
        onMouseDown={event => event.preventDefault()} onClick={() => onAI()}><Sparkles size={15} />AI 草稿</button>
      <button type="button" className="text-action" aria-haspopup="dialog"
        disabled={disabled || !(mode === 'source' ? hasSourceSelection : state?.selection)}
        onMouseDown={event => event.preventDefault()} onClick={openSelectionAI}>AI 选段处理</button>
    </div>}
    {mode === 'source' ? <>
      {sourceOnly && <p className="note-source-hint">此笔记含有需在 Markdown 原文中编辑的格式。</p>}
      <div className="note-source-sheet">
        {ranges.length > 0 && <SourceMarks text={content} ranges={ranges} backdrop={sourceBackdrop} />}
        <textarea ref={source} className="note-source" aria-label={label} placeholder="写下例子、疑问或你自己的理解。" maxLength={maxNoteLength}
          disabled={disabled} value={content} onFocus={event => rememberCaret(event.currentTarget)}
          onScroll={event => { if (sourceBackdrop.current) sourceBackdrop.current.scrollTop = event.currentTarget.scrollTop; }}
          onSelect={event => rememberCaret(event.currentTarget)} onChange={event => {
            setLimitError('');
            rememberCaret(event.currentTarget);
            onChange(event.currentTarget.value);
          }} />
      </div>
      {completion.menu}
      <ErrorNote message={limitError} />
    </> : <>
      <div className="note-editor-toolbar" role="group" aria-label="笔记格式">
        {controls.map(({ label, Icon, active, apply }) => <button type="button" key={label} aria-label={label} title={label}
          aria-pressed={!!active} disabled={!editor || disabled} onMouseDown={event => event.preventDefault()} onClick={apply}><Icon size={16} /></button>)}
        <button type="button" aria-label="链接" title="链接" disabled={!editor || disabled || !(state?.selection || state?.link)}
          aria-expanded={insertion?.kind === 'link'} onMouseDown={event => event.preventDefault()} onClick={() => {
            setInsertionError('');
            setInsertion({ kind: 'link', href: state?.linkTarget ?? '' });
          }}><Link2 size={16} /></button>
        <button type="button" aria-label="公式" title="公式" disabled={!editor || disabled || state?.code}
          aria-expanded={insertion?.kind === 'formula'} onMouseDown={event => event.preventDefault()} onClick={openFormula}><Sigma size={16} /></button>
      </div>
      {insertion && <form className="note-editor-insertion" key={insertion.kind}
        aria-label={insertion.kind === 'formula' ? '公式设置' : '链接设置'} onSubmit={applyInsertion}>
        {insertion.kind === 'formula' ? <>
          <label>公式位置<select value={insertion.block ? 'block' : 'inline'} disabled={disabled || insertion.position !== undefined}
            onChange={event => setInsertion({ ...insertion, block: event.target.value === 'block' })}>
            <option value="inline">行内</option><option value="block">独立一行</option>
          </select></label>
          <label>LaTeX<textarea rows={insertion.block ? 3 : 1} autoFocus disabled={disabled} value={insertion.latex}
            onChange={event => setInsertion({ ...insertion, latex: event.target.value })} /></label>
        </> : <label>链接地址<input autoFocus disabled={disabled} value={insertion.href}
          onChange={event => setInsertion({ ...insertion, href: event.target.value })} /></label>}
        <ErrorNote message={insertionError} />
        <div className="action-row">
          <button className="button primary" type="submit" disabled={disabled}>应用</button>
          {insertion.kind === 'link' && state?.link && <button className="button secondary" type="button" disabled={disabled}
            onClick={() => { editor?.chain().focus().extendMarkRange('link').unsetLink().run(); setInsertion(undefined); }}>移除链接</button>}
          <button className="button secondary" type="button" onClick={() => { setInsertion(undefined); editor?.commands.focus(); }}>取消</button>
        </div>
      </form>}
      <EditorContent editor={editor} className="note-rich-editor" />
      {completion.menu}
      <ErrorNote message={limitError} />
    </>}
  </div>;
}
