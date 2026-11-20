import type { Nodes } from 'mdast';
import type { Extension as FromMarkdown } from 'mdast-util-from-markdown';
import type { Code, Extension as Syntax, Tokenizer } from 'micromark-util-types';
import remarkMath from 'remark-math';
import remarkFrontmatter from 'remark-frontmatter';
import remarkParse from 'remark-parse';
import { unified, type Processor } from 'unified';

declare module 'micromark-util-types' {
  interface TokenTypeMap {
    workspaceWiki: 'workspaceWiki';
  }
}

declare module 'mdast' {
  interface Data {
    wikiTarget?: string;
    wikiSource?: string;
  }
}

const maxLength = 512;

export function wikiParts(value: string) {
  if (/[\[\]\r\n]/.test(value) || [...value].length > maxLength) return undefined;
  const separator = value.indexOf('|');
  const target = (separator === -1 ? value : value.slice(0, separator)).trim();
  const label = separator === -1 ? target : value.slice(separator + 1).trim() || target;
  return target ? { target, label } : undefined;
}

export function wikiRoute(target: string) {
  return '/library?' + new URLSearchParams({ wiki: target });
}

export function stableWikiValue({ id, name }: { id: string; name: string }) {
  const label = name.replace(/[\[\]|]/g, char => ({ '[': '［', ']': '］', '|': '｜' })[char]!)
    .replace(/[\r\n]/g, ' ').trim();
  return id + '|' + [...label].slice(0, maxLength - id.length - 1).join('');
}

const tokenizeWiki: Tokenizer = function (effects, ok, nok) {
  const previous = this.previous;
  let value = '';

  function start(code: Code) {
    if (code !== 91 || previous === 33) return nok(code);
    effects.enter('workspaceWiki');
    effects.consume(code);
    return secondOpen;
  }

  function secondOpen(code: Code) {
    if (code !== 91) return nok(code);
    effects.consume(code);
    return inside;
  }

  function inside(code: Code) {
    if (code === 93 && wikiParts(value)) {
      effects.consume(code);
      return secondClose;
    }
    if (code === null || code < -2 || code === 91 || code === 93 || value.length >= maxLength * 2) return nok(code);
    if (code !== -1) value += code === -2 ? '\t' : String.fromCharCode(code);
    effects.consume(code);
    return inside;
  }

  function secondClose(code: Code) {
    if (code !== 93) return nok(code);
    effects.consume(code);
    return after;
  }

  function after(code: Code) {
    // Leave Markdown link labels such as [[label]](url) to the ordinary link parser.
    if (code === 40) return nok(code);
    effects.exit('workspaceWiki');
    return ok(code);
  }

  return start;
};

const syntax: Syntax = { text: { 91: { name: 'workspaceWiki', tokenize: tokenizeWiki } } };
const linkContainers = new Set(['link', 'linkReference', 'image', 'imageReference']);

const compiler: FromMarkdown = {
  enter: {
    workspaceWiki(token) {
      const raw = this.sliceSerialize(token);
      const wiki = wikiParts(raw.slice(2, -2))!;
      this.enter({
        type: 'link', url: '#' + wikiRoute(wiki.target),
        data: { wikiTarget: wiki.target, wikiSource: raw },
        children: [{ type: 'text', value: '[[' + wiki.label + ']]' }],
      }, token);
    },
  },
  exit: { workspaceWiki(token) { this.exit(token); } },
  transforms: [tree => {
    function flatten(node: Nodes, insideLink: boolean) {
      if (!('children' in node)) return;
      const suppress = insideLink || linkContainers.has(node.type);
      const children: Nodes[] = node.children;
      children.forEach((child, index) => {
        if (suppress && child.type === 'link' && child.data?.wikiSource) {
          children[index] = { type: 'text', value: child.data.wikiSource, position: child.position };
        } else {
          flatten(child, suppress);
        }
      });
    }
    flatten(tree, false);
  }],
};

export function remarkWiki(this: Processor) {
  const data = this.data();
  (data.micromarkExtensions ??= []).push(syntax);
  (data.fromMarkdownExtensions ??= []).push(compiler);
}

const markdown = unified().use(remarkParse).use(remarkFrontmatter).use(remarkMath).use(remarkWiki);

export function wikiReferences(text: string) {
  const links: { start: number; end: number; target: string; raw: string }[] = [];
  if (!text.includes('[[')) return links;
  function collect(node: Nodes) {
    if (node.type === 'link') {
      const { wikiTarget, wikiSource } = node.data ?? {};
      const start = node.position?.start.offset;
      const end = node.position?.end.offset;
      if (wikiTarget && wikiSource && start !== undefined && end !== undefined) {
        links.push({ start, end, target: wikiTarget, raw: wikiSource });
      }
    }
    if ('children' in node) node.children.forEach(collect);
  }
  // Micromark omits the leading BOM from positional offsets.
  const offset = text.charCodeAt(0) === 0xfeff ? 1 : 0;
  collect(markdown.parse(text.slice(offset)));
  return offset ? links.map(link => ({ ...link, start: link.start + offset, end: link.end + offset })) : links;
}

export type WikiDraft = { from: number; to: number; query: string };

export function wikiDraft(before: string, after: string, position: number): WikiDraft | undefined {
  const start = before.lastIndexOf('[[');
  if (start < 0 || before[start - 1] === '!') return undefined;
  const partial = before.slice(start + 2);
  const backslashes = /\\+$/.exec(before.slice(0, start))?.[0].length ?? 0;
  if (backslashes % 2 || /[\[\]|\r\n]/.test(partial) || [...partial].length > maxLength) return undefined;
  return { from: position - (before.length - start), to: position + (after.startsWith(']]') ? 2 : 0), query: partial.trim() };
}

export function sourceWikiDraft(source: string, position: number) {
  const current = wikiDraft(source.slice(0, position), source.slice(position), position);
  if (!current) return undefined;
  const probe = source.slice(0, current.from) + '[[链接补全]]' + source.slice(current.to);
  return wikiReferences(probe).some(link => link.start === current.from) ? current : undefined;
}
