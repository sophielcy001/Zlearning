import { Extension } from '@tiptap/core';
import type { Node as ProseMirrorNode } from '@tiptap/pm/model';
import { Plugin, PluginKey } from '@tiptap/pm/state';
import { Decoration, DecorationSet } from '@tiptap/pm/view';
import type { NoteFinding } from './note-checks';

export type FindingRange = { start: number; end: number; finding: NoteFinding };

export function noteFindingRanges(text: string, findings: readonly NoteFinding[]): FindingRange[] {
  if (findings.length === 0) return [];
  const offsets = [0];
  let cursor = 0;
  for (const symbol of text) {
    cursor += symbol.length;
    offsets.push(cursor);
  }
  const ranges = [];
  for (const finding of findings) {
    const start = offsets[finding.start], end = offsets[finding.end];
    if (start !== undefined && end !== undefined && text.slice(start, end) === finding.quote) {
      ranges.push({ start, end, finding });
    }
  }
  return ranges;
}

/** The raw range remains authoritative when more than one rich-text position looks the same. */
export function noteDecorations(doc: ProseMirrorNode, text: string, ranges: FindingRange[]) {
  const decorations: Decoration[] = [];
  for (const range of ranges) {
    const quote = range.finding.quote;
    if (text.indexOf(quote) !== range.start || text.indexOf(quote, range.start + 1) !== -1) continue;
    let from: number | undefined;
    let duplicate = false;
    doc.descendants((node, position) => {
      if (!node.isTextblock || duplicate) return !duplicate;
      const visible = node.textBetween(0, node.content.size, '', '\ufffc');
      const match = visible.indexOf(quote);
      if (match >= 0) {
        duplicate = from !== undefined || visible.indexOf(quote, match + 1) >= 0;
        from = position + 1 + match;
      }
      return false;
    });
    if (from !== undefined && !duplicate) decorations.push(Decoration.inline(from, from + quote.length, {
      class: 'note-check-mark', title: range.finding.reason,
    }));
  }
  return DecorationSet.create(doc, decorations);
}

export const noteCheckHighlights = new PluginKey<DecorationSet>('noteCheckHighlights');
export const NoteCheckMarks = Extension.create({
  name: 'noteCheckMarks',
  addProseMirrorPlugins() {
    return [new Plugin({
      key: noteCheckHighlights,
      state: {
        init: () => DecorationSet.empty,
        apply: (transaction, previous) => transaction.getMeta(noteCheckHighlights)
          ?? (transaction.docChanged ? DecorationSet.empty : previous),
      },
      props: { decorations: state => noteCheckHighlights.getState(state) },
    })];
  },
});
