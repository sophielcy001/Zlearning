import { Node, nodeInputRule, type Editor, type JSONContent } from '@tiptap/core';
import CodeBlock from '@tiptap/extension-code-block';
import Link from '@tiptap/extension-link';
import { unified } from 'unified';
import remarkParse from 'remark-parse';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';

import { fencedCode, maxNoteLength } from './note-content';
import { wikiParts, wikiReferences, wikiRoute } from './wiki-markdown';

const markdown = unified().use(remarkParse).use(remarkGfm).use(remarkMath);
const sourceOnly = new Set(['html', 'image', 'imageReference', 'linkReference', 'definition', 'footnoteReference', 'footnoteDefinition', 'table']);
const obsidianSyntax = /!?\[\[[^\r\n[\]]+\]\]|^[ \t]*(?:>[ \t]*)+\[![^\]\r\n]+\][+-]?|%%|==[^\r\n=]+==|[ \t]\^[\w-]+[ \t]*$/gm;

type SyntaxNode = { type: string; children?: SyntaxNode[] };

function hasSourceOnlySyntax(node: SyntaxNode): boolean {
  return sourceOnly.has(node.type) || node.children?.some(hasSourceOnlySyntax) === true;
}

function structure(tree: SyntaxNode) {
  return JSON.stringify(tree, (key, value: unknown) => key === 'position' || key === 'data' ? undefined : value);
}

function wikiAtoms(node: JSONContent): string[] {
  if (node.type === 'wikiReference') return ['[[' + node.attrs?.target + ']]'];
  return node.content?.flatMap(wikiAtoms) ?? [];
}

// Tiptap escapes Markdown delimiters but leaves the math extension's dollar delimiter in plain text.
export function serializeRichNote(editor: Editor, document: JSONContent = editor.getJSON()): string {
  const format = editor.markdown;
  if (!format) return '';
  const snapshot = JSON.stringify(document);
  if (!snapshot.includes('$')) return format.serialize(document);
  // A character absent from the document lets the standard serializer handle marks around ordinary text.
  let codePoint = 0xf0000;
  let marker = String.fromCodePoint(codePoint);
  if (snapshot.includes(marker)) {
    const used = new Set(snapshot);
    while (used.has(marker)) marker = String.fromCodePoint(++codePoint);
  }
  function protectText(node: JSONContent): JSONContent {
    if (node.type === 'codeBlock' || node.marks?.some(mark => mark.type === 'code')) return node;
    if (node.type === 'text' && node.text?.includes('$')) {
      return { ...node, text: node.text.replaceAll('$', marker) };
    }
    return node.content ? { ...node, content: node.content.map(protectText) } : node;
  }
  return format.serialize(protectText(document)).replaceAll(marker, '\\$');
}

export function readRichNote(editor: Editor, source: string): JSONContent | null {
  if (!editor.markdown || source.length > maxNoteLength || /^(?:\uFEFF)?---[ \t]*\r?\n/.test(source)) return null;
  try {
    const original = markdown.parse(source);
    if (hasSourceOnlySyntax(original)) return null;
    const document = editor.markdown.parse(source);
    if (JSON.stringify(wikiAtoms(document)) !== JSON.stringify(wikiReferences(source).map(link => link.raw))) return null;
    const serialized = serializeRichNote(editor, document);
    if (serialized.length > maxNoteLength) return null;
    // Compare a separate parser's trees so unsupported input cannot disappear during both conversions.
    if (serialized !== source && (structure(original) !== structure(markdown.parse(serialized)) ||
        JSON.stringify(source.match(obsidianSyntax)) !== JSON.stringify(serialized.match(obsidianSyntax)))) return null;
    return document;
  } catch {
    return null;
  }
}

export const NoteCodeBlock = CodeBlock.extend({
  renderMarkdown(node, helpers) {
    return fencedCode(helpers.renderChildren(node.content ?? []), node.attrs?.language ?? '');
  },
});

export const NoteLink = Link.extend({
  renderMarkdown(node, helpers) {
    const href = (node.attrs?.href ?? '').replace(/[<>\\]/g, '\\$&');
    const destination = /[\s()<>\\]/.test(href) ? '<' + href + '>' : href;
    const title = node.attrs?.title?.replace(/["\\]/g, '\\$&');
    return '[' + helpers.renderChildren(node) + '](' + destination + (title ? ' "' + title + '"' : '') + ')';
  },
});

export const WikiReference = Node.create({
  name: 'wikiReference',
  group: 'inline',
  inline: true,
  atom: true,

  addAttributes() {
    return { target: {
      default: '',
      parseHTML: element => element.getAttribute('data-wiki-reference') ?? '',
      renderHTML: attributes => ({ 'data-wiki-reference': attributes.target }),
    } };
  },

  parseHTML() {
    return [{ tag: 'a[data-wiki-reference]', priority: 1100 }, { tag: 'span[data-wiki-reference]' }];
  },

  renderHTML({ node, HTMLAttributes }) {
    const wiki = wikiParts(node.attrs.target);
    const attributes = { ...HTMLAttributes, class: 'note-wiki-reference' };
    return wiki
      ? ['a', { ...attributes, href: '#' + wikiRoute(wiki.target) }, '[[' + wiki.label + ']]']
      : ['span', attributes, '[[' + node.attrs.target + ']]'];
  },

  renderText({ node }) {
    return '[[' + node.attrs.target + ']]';
  },

  markdownTokenizer: {
    name: 'wikiReference',
    level: 'inline',
    start: text => text.indexOf('[['),
    tokenize: text => {
      const match = /^\[\[([^[\]\r\n]+)\]\]/.exec(text);
      return match && wikiParts(match[1]) && text[match[0].length] !== '('
        ? { type: 'wikiReference', raw: match[0], text: match[1] } : undefined;
    },
  },

  parseMarkdown(token, helpers) {
    return helpers.createNode('wikiReference', { target: token.text });
  },

  renderMarkdown(node) {
    return '[[' + node.attrs?.target + ']]';
  },

  addInputRules() {
    return [nodeInputRule({
      find: /(?<![!\\])\[\[([^[\]\r\n]+)\]\]$/,
      type: this.type,
      getAttributes: match => wikiParts(match[1]) ? { target: match[1] } : false,
    })];
  },
});
