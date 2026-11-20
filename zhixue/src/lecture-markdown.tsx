import { createElement, useCallback, useLayoutEffect, useRef, useState, type ComponentProps } from 'react';
import type { Components, ExtraProps } from 'react-markdown';
import remarkMath from 'remark-math';
import katex from 'katex';
import { ExcerptPopover, selectedRange, type ExcerptActions } from './course-excerpt-control';
import { fileExcerpt, formulaMarkdown, inlineCode, quoteText } from './course-excerpt';
import { fencedCode } from './note-content';
import { Markdown } from './shared';
import 'katex/dist/katex.min.css';

function sourceData(node: ExtraProps['node']) {
  return { 'data-course-source-line': node?.position?.start.line };
}

function LectureFormula({ latex, display, line }: { latex: string; display: boolean; line?: number }) {
  const target = useRef<HTMLSpanElement>(null);
  useLayoutEffect(() => {
    if (target.current) katex.render(latex, target.current, { displayMode: display, trust: false, throwOnError: false });
  }, [latex, display]);
  return <span ref={target} data-course-source-line={line} data-lecture-tex={latex} data-lecture-display={display ? 'true' : undefined}
    className="lecture-formula" draggable title="拖动公式到笔记" />;
}

const positionedTags = ['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'blockquote', 'td', 'th'] as const;
const components: Components = {
  ...Object.fromEntries(positionedTags.map(tag => [
    tag, ({ node, ...props }: ComponentProps<'p'> & ExtraProps) => createElement(tag, { ...props, ...sourceData(node) }),
  ])),
  pre: ({ node, children, ...props }) => {
    const code = node?.children[0];
    const classes = code?.type === 'element' && code.properties.className;
    if (code?.type === 'element' && Array.isArray(classes) && classes.includes('math-display')) {
      const content = code.children[0];
      const latex = content?.type === 'text' ? content.value : '';
      return <div {...sourceData(node)} className="lecture-display-formula" data-reading-block>
        <LectureFormula latex={latex} display line={node?.position?.start.line} />
      </div>;
    }
    return <pre {...props} {...sourceData(node)}>{children}</pre>;
  },
  code: ({ node, className, children, ...props }) => {
    if (className?.split(' ').includes('math-inline')) {
      return <LectureFormula latex={String(children ?? '')} display={false} line={node?.position?.start.line} />;
    }
    return <code {...props} {...sourceData(node)} className={className}>{children}</code>;
  },
};

function nearest(node: Node, selector: string) {
  return (node instanceof Element ? node : node.parentElement)?.closest<HTMLElement>(selector);
}

const lineSelector = '[data-course-source-line]';
const formulaSelector = '[data-lecture-tex]';

function formula(element: HTMLElement) {
  return formulaMarkdown(element.dataset.lectureTex!, element.dataset.lectureDisplay === 'true');
}

function codeInfo(code: HTMLElement, originalLines: string[], headerLines: number) {
  const openingLine = originalLines[Number(code.dataset.courseSourceLine) + headerLines - 1] ?? '';
  const fence = /(?:^|\s)(?:\x60{3,}|~{3,})([^\r\n]*)$/.exec(openingLine);
  return fence ? fence[1].trim() : code.className.match(/(?:^|\s)language-(\S+)/)?.[1] ?? '';
}

function prefixOf(element: HTMLElement, node: Node, offset: number) {
  const prefix = document.createRange();
  prefix.selectNodeContents(element);
  prefix.setEnd(node, offset);
  return prefix.toString();
}

function formatCodeClip(code: HTMLElement, text: string, originalLines: string[], headerLines: number, fullEnd: boolean) {
  // react-markdown appends a display newline to fenced code.
  return fencedCode(fullEnd ? text.replace(/\n$/, '') : text, codeInfo(code, originalLines, headerLines));
}

function contentInRange(range: Range, originalLines: string[], headerLines: number) {
  const startCode = nearest(range.startContainer, 'code');
  if (startCode?.contains(range.endContainer)) {
    const text = range.toString();
    if (!text.trim()) return '';
    if (startCode.parentElement?.tagName !== 'PRE') return inlineCode(text);
    const fullEnd = prefixOf(startCode, range.endContainer, range.endOffset).length === startCode.textContent?.length;
    return formatCodeClip(startCode, text, originalLines, headerLines, fullEnd);
  }
  const startFormula = nearest(range.startContainer, formulaSelector);
  const endFormula = nearest(range.endContainer, formulaSelector);
  if (startFormula && startFormula === endFormula) return formula(startFormula);
  const endCode = nearest(range.endContainer, 'pre > code');
  const normalized = range.cloneRange();
  if (startFormula) normalized.setStartBefore(startFormula);
  if (endFormula) normalized.setEndAfter(endFormula);

  const parts: string[] = [];
  let text = '';
  function flush() {
    if (text.trim()) parts.push(quoteText(text));
    text = '';
  }
  function collect(node: Node) {
    if (!(node instanceof HTMLElement)) {
      if (node.nodeType === Node.TEXT_NODE) text += node.textContent ?? '';
      return;
    }
    if (node.matches(formulaSelector)) {
      flush();
      parts.push(formula(node));
      return;
    }
    if (node.tagName === 'PRE') {
      flush();
      const code = node.querySelector<HTMLElement>('code');
      if (code?.textContent?.trim()) {
        const clippedEnd = endCode && endCode.dataset.courseSourceLine === code.dataset.courseSourceLine &&
          prefixOf(endCode, range.endContainer, range.endOffset).length < (endCode.textContent?.length ?? 0);
        parts.push(formatCodeClip(code, code.textContent, originalLines, headerLines, !clippedEnd));
      }
      return;
    }
    if (node.tagName === 'CODE') {
      flush();
      if (node.textContent) parts.push(inlineCode(node.textContent));
      return;
    }
    const paragraph = node.matches('p,br,h1,h2,h3,h4,h5,h6,li,blockquote,td,th');
    if (paragraph && text && !text.endsWith('\n')) text += '\n';
    node.childNodes.forEach(collect);
    if (paragraph && text && !text.endsWith('\n')) text += '\n';
  }
  normalized.cloneContents().childNodes.forEach(collect);
  flush();
  return parts.join('\n\n');
}

function sourceLine(range: Range, root: HTMLElement, originalLines: string[], headerLines: number) {
  const element = nearest(range.startContainer, lineSelector);
  if (element && root.contains(element)) {
    let line = headerLines + Number(element.dataset.courseSourceLine);
    if (element.matches('pre > code')) {
      const codeLine = prefixOf(element, range.startContainer, range.startOffset).split('\n').length - 1;
      const openingLine = originalLines[line - 1] ?? '';
      line += codeLine + (/(?:^|\s)(?:\x60{3,}|~{3,})/.test(openingLine) ? 1 : 0);
    }
    return line;
  }
  // Element-boundary selections may start between Markdown blocks.
  const first = Array.from(root.querySelectorAll<HTMLElement>(lineSelector)).find(node => range.intersectsNode(node));
  return first ? headerLines + Number(first.dataset.courseSourceLine) : undefined;
}

export function OfflineLecture({ body }: { body: string }) {
  return <Markdown components={components} remarkPlugins={[remarkMath]}>{body}</Markdown>;
}

export function LectureMarkdown({ lesson, body, revision, excerpts, onReadingBody }: {
  lesson: string; body: string; revision: string | null; excerpts: ExcerptActions;
  onReadingBody?: (element: HTMLDivElement | null) => void;
}) {
  const [root, setRoot] = useState<HTMLDivElement | null>(null);
  const attach = useCallback((element: HTMLDivElement | null) => {
    setRoot(element);
    onReadingBody?.(element);
  }, [onReadingBody]);
  const heading = /^(?:\uFEFF)?# [^\r\n]*(?:\r\n|\n|\r)(?:[ \t]*(?:\r\n|\n|\r))*/.exec(body)?.[0] ?? '';
  const markdown = body.slice(heading.length);

  function excerptFrom(range: Range) {
    if (!root || !revision) return undefined;
    const headerLines = heading.split(/\r\n|\n|\r/).length - 1;
    const originalLines = body.split(/\r\n|\n|\r/);
    const line = sourceLine(range, root, originalLines, headerLines);
    if (!line) return undefined;
    const content = contentInRange(range, originalLines, headerLines);
    return content ? fileExcerpt(lesson, revision, line, content) : undefined;
  }

  return <div ref={attach} onDragStart={event => {
    if (!root || !revision) return;
    const candidate = selectedRange(root);
    const target = event.target instanceof Element ? event.target : null;
    const equation = target?.closest<HTMLElement>(formulaSelector);
    if (equation && root.contains(equation) && !candidate?.intersectsNode(equation)) {
      const line = Number(equation.dataset.courseSourceLine) + heading.split(/\r\n|\n|\r/).length - 1;
      if (Number.isInteger(line) && line > 0) excerpts.drag(event, fileExcerpt(lesson, revision, line, formula(equation)));
    } else if (candidate && target && candidate.intersectsNode(target)) {
      const excerpt = excerptFrom(candidate);
      if (excerpt) excerpts.drag(event, excerpt);
    }
  }} onDragEnd={excerpts.endDrag}>
    <Markdown components={components} remarkPlugins={[remarkMath]}>{markdown}</Markdown>
    {root && revision && <ExcerptPopover root={root} excerpts={excerpts} capture={excerptFrom} />}
  </div>;
}
