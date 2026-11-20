import type { Editor } from '@tiptap/core';
import { Slice } from '@tiptap/pm/model';
import type { NoteAISelection } from './note-ai';
import { sourceOffset } from './note-content';
import { readRichNote, serializeRichNote } from './note-markdown';

export function sourceAISelection(content: string, field: HTMLTextAreaElement): NoteAISelection | undefined {
  const from = sourceOffset(content, field.selectionStart);
  const to = sourceOffset(content, field.selectionEnd);
  const markdown = content.slice(from, to);
  if (!markdown.trim()) return undefined;
  return { markdown, replace: next => content.slice(0, from) + next + content.slice(to) };
}

export function richAISelection(editor: Editor | null, content: string): NoteAISelection | undefined {
  if (!editor?.markdown) return undefined;
  const basis = editor.state.doc;
  const { from, to, empty, $from, $to } = editor.state.selection;
  if (empty) return undefined;
  const markdown = serializeRichNote(editor, { type: 'doc', content: basis.slice(from, to, true).content.toJSON() ?? [] });
  if (!markdown.trim()) return undefined;

  return {
    markdown,
    replace(next) {
      if (editor.isDestroyed || !editor.state.doc.eq(basis)) throw new Error('笔记选段已变更，请重新选择。');
      const parsed = readRichNote(editor, next);
      const incompatible = '建议的 Markdown 与所选位置的格式不兼容。可追加建议，或在原文模式中处理。';
      if (!parsed) throw new Error(incompatible);
      const replacement = editor.schema.nodeFromJSON(parsed);
      const block = replacement.childCount === 1 ? replacement.firstChild : null;
      const inline = $from.sameParent($to) && block && (block.type.name === 'paragraph' || block.type === $from.parent.type);
      const slice = inline ? new Slice(block.content, 0, 0) : Slice.maxOpen(replacement.content);
      let outside = false;
      try {
        const transaction = editor.state.tr.replaceRange(from, to, slice);
        for (const step of transaction.mapping.maps) step.forEach((a, b) => { outside ||= a < from || b > to; });
        if (transaction.docChanged && !outside) {
          return transaction.doc.eq(basis) ? content : serializeRichNote(editor, transaction.doc.toJSON());
        }
      } catch {
        throw new Error(incompatible);
      }
      throw new Error(incompatible);
    },
  };
}
