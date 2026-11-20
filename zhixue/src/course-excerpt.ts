import { unified } from 'unified';
import remarkParse from 'remark-parse';
import remarkMath from 'remark-math';
import type { CourseSelection } from './conversation';
import { fencedCode, maxNoteLength } from './note-content';

export const courseExcerptMime = 'application/x-zhixue-course-excerpt';

export type CourseExcerpt = { markdown: string; href: string; label: string };
export type ExcerptRequest = CourseExcerpt & { id: number; noteId?: string };

const parser = unified().use(remarkParse).use(remarkMath);

export function quoteText(text: string) {
  return text.trim().split(/\r\n|\n|\r/).map(line => {
    const escaped = line.replace(/[\\\x60*_{}[\]<>#+.!|~()$&=-]/g, '\\$&');
    return escaped ? '> ' + escaped : '>';
  }).join('\n');
}

export function inlineCode(text: string) {
  const length = (text.match(/\x60+/g) ?? []).reduce((longest, run) => Math.max(longest, run.length + 1), 1);
  const fence = '\x60'.repeat(length);
  // CommonMark trims one padding space per side when the content is not all spaces.
  const padding = /^[\x60 ]|[\x60 ]$/.test(text) && text.trim() ? ' ' : '';
  return fence + padding + text + padding + fence;
}

export function formulaMarkdown(latex: string, display: boolean) {
  // Keep any literal dollar signs in the expression distinct from the delimiters.
  const size = (latex.match(/\$+/g) ?? []).reduce((longest, part) => Math.max(longest, part.length + 1), display ? 2 : 1);
  const fence = '$'.repeat(size);
  return display ? fence + '\n' + latex + '\n' + fence : fence + latex + fence;
}

export function fileExcerpt(lesson: string, revision: string, line: number, markdown: string): CourseExcerpt {
  const query = new URLSearchParams({ file: 'course-' + lesson, at: String(line), revision });
  return { markdown, href: '#/library?' + query, label: '查看原文 · 第 ' + line + ' 行' };
}

type LocatedNode = {
  type: string;
  position?: { start: { offset?: number }; end: { offset?: number } };
  children?: LocatedNode[];
  url?: string;
  value?: string;
  lang?: string | null;
  meta?: string | null;
};

type Notation = { from: number; to: number; node: LocatedNode };
const notationTypes = new Set(['code', 'inlineCode', 'math', 'inlineMath']);

function notations(node: LocatedNode): Notation[] {
  const from = node.position?.start.offset;
  const to = node.position?.end.offset;
  if (notationTypes.has(node.type) && from !== undefined && to !== undefined) return [{ from, to, node }];
  return node.children?.flatMap(notations) ?? [];
}

function codeSlice(source: string, piece: Notation, start: number, end: number) {
  const raw = source.slice(piece.from, piece.to);
  const fence = /^(\x60{3,}|~{3,})/.exec(raw)?.[0];
  if (!fence) return source.slice(Math.max(start, piece.from), Math.min(end, piece.to));
  const breaks = Array.from(raw.matchAll(/\r\n|\r|\n/g));
  const firstBreak = breaks[0];
  if (!firstBreak) return '';
  const bodyStart = piece.from + firstBreak.index + firstBreak[0].length;
  const lastBreak = breaks.at(-1)!;
  const closingLine = raw.slice(lastBreak.index + lastBreak[0].length);
  const closingFence = new RegExp('^[ \\t]*(?:>[ \\t]*)*' + fence[0] + '{' + fence.length + ',}[ \\t]*$');
  const bodyEnd = closingFence.test(closingLine) ? piece.from + lastBreak.index : piece.to;
  return source.slice(Math.max(start, bodyStart), Math.min(end, bodyEnd));
}

export function verifiedExcerpt(lesson: string, passage: CourseSelection, source: string): CourseExcerpt {
  const chars = Array.from(source);
  let start = chars.slice(0, passage.start).join('').length;
  let end = start + passage.text.length;
  const matches = notations(parser.parse(source)).filter(piece => piece.to > start && piece.from < end);
  for (const piece of matches) {
    if (piece.node.type !== 'code') {
      start = Math.min(start, piece.from);
      end = Math.max(end, piece.to);
    }
  }
  const parts: string[] = [];
  function prose(text: string) { if (text.trim()) parts.push(quoteText(text)); }
  let offset = start;
  for (const piece of matches) {
    if (offset < piece.from) prose(source.slice(offset, piece.from));
    const { node } = piece;
    const value = node.value ?? '';
    if (node.type === 'code') {
      const body = start <= piece.from && end >= piece.to ? value : codeSlice(source, piece, start, end);
      if (body) parts.push(fencedCode(body, [node.lang, node.meta].filter(Boolean).join(' ')));
    } else if (node.type === 'inlineCode') {
      if (value) parts.push(inlineCode(value));
    } else if (value) parts.push(formulaMarkdown(value, node.type === 'math'));
    offset = Math.min(end, piece.to);
  }
  if (offset < end) prose(source.slice(offset, end));
  const query = new URLSearchParams({
    readingVersion: String(passage.version), section: String(passage.sectionIndex),
    selectionStart: String(Array.from(source.slice(0, start)).length),
    selectionEnd: String(Array.from(source.slice(0, end)).length),
  });
  return {
    markdown: parts.join('\n\n') || quoteText(passage.text), href: '#/lessons/' + lesson + '?' + query,
    label: '查看原文 · 第 ' + passage.version + ' 版',
  };
}

function hasNewLink(node: LocatedNode, href: string, appendedAt: number): boolean {
  if (node.type === 'link' && node.url === href && (node.position?.start.offset ?? -1) >= appendedAt) return true;
  return node.children?.some(child => hasNewLink(child, href, appendedAt)) ?? false;
}

export function appendCourseExcerpt(content: string, excerpt: CourseExcerpt) {
  const boundary = !content || content.endsWith('\n\n') ? '' : content.endsWith('\n') ? '\n' : '\n\n';
  const next = content + boundary + excerpt.markdown + '\n\n[' + excerpt.label + '](' + excerpt.href + ')\n';
  if (next.length > maxNoteLength) return { error: '笔记已接近 50,000 字符，腾出空间后即可追加摘录。' };
  try {
    if (hasNewLink(parser.parse(next), excerpt.href, content.length)) return { content: next };
  } catch {
    return { error: '笔记原文暂时无法解析，请检查其 Markdown 格式后重试。' };
  }
  return { error: '原文中的 Markdown 块尚未结束。结束该块后即可追加摘录。' };
}
