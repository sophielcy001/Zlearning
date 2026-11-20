import { GripVertical } from 'lucide-react';
import { createPortal } from 'react-dom';
import { useEffect, useRef, useState, type DragEvent } from 'react';
import type { CourseExcerpt } from './course-excerpt';

export type ExcerptActions = {
  add: (excerpt: CourseExcerpt) => void;
  drag: (event: DragEvent, excerpt: CourseExcerpt) => void;
  endDrag: () => void;
};

export function selectedRange(root: HTMLElement | null) {
  const selection = window.getSelection();
  if (!root || !selection || selection.isCollapsed || selection.rangeCount !== 1) return undefined;
  const range = selection.getRangeAt(0);
  return root.contains(range.startContainer) && root.contains(range.endContainer) ? range : undefined;
}

export function focusedExcerptControl() {
  return document.activeElement?.classList.contains('course-excerpt-button') === true;
}

export function ExcerptButton({ onClick, onDrag, onDragEnd, onBlur }: {
  onClick: () => void; onDrag: (event: DragEvent<HTMLButtonElement>) => void; onDragEnd: () => void; onBlur: () => void;
}) {
  return <button type="button" className="button soft course-excerpt-button" draggable
    title="点击追加，或拖到笔记" onPointerDown={event => event.currentTarget.focus({ preventScroll: true })}
    onClick={onClick} onBlur={onBlur} onDragStart={event => { event.stopPropagation(); onDrag(event); }}
    onDragEnd={event => { event.stopPropagation(); onDragEnd(); }}>
    <GripVertical size={15} />摘录到笔记
  </button>;
}

export function ExcerptPopover({ root, excerpts, capture }: {
  root: HTMLElement; excerpts: ExcerptActions; capture: (range: Range) => CourseExcerpt | undefined;
}) {
  const dragging = useRef(false);
  const [position, setPosition] = useState<{ top: number; left: number; range: Range }>();

  useEffect(() => {
    function measure() {
      const range = selectedRange(root);
      const rect = range?.getBoundingClientRect();
      if (!range || !rect || rect.bottom <= 0 || rect.top >= window.innerHeight) {
        if (!dragging.current && !focusedExcerptControl()) setPosition(undefined);
        return;
      }
      const lower = rect.bottom + 8;
      setPosition({
        range: range.cloneRange(),
        top: Math.max(8, lower + 44 <= window.innerHeight ? lower : rect.top - 48),
        left: Math.max(8, Math.min(rect.left, window.innerWidth - 166)),
      });
    }
    document.addEventListener('selectionchange', measure);
    window.addEventListener('resize', measure);
    window.addEventListener('scroll', measure, { capture: true, passive: true });
    return () => {
      document.removeEventListener('selectionchange', measure);
      window.removeEventListener('resize', measure);
      window.removeEventListener('scroll', measure, true);
    };
  }, [root]);

  function clear() {
    window.getSelection()?.removeAllRanges();
    setPosition(undefined);
  }

  return position ? createPortal(<div className="course-excerpt-popover" style={{ top: position.top, left: position.left }}>
    <ExcerptButton onClick={() => {
      const excerpt = capture(position.range);
      if (excerpt) excerpts.add(excerpt);
      clear();
    }} onDrag={event => {
      const excerpt = capture(position.range);
      if (!excerpt) { event.preventDefault(); return; }
      dragging.current = true;
      excerpts.drag(event, excerpt);
    }} onDragEnd={() => {
      dragging.current = false;
      excerpts.endDrag();
      clear();
    }} onBlur={() => { if (!dragging.current && !selectedRange(root)) setPosition(undefined); }} />
  </div>, document.body) : null;
}
