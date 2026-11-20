import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { ApiError, recordReadingPosition, type ReadingPoint, type ReadingPosition } from './api';

const visibleLine = () => Math.min(112, Math.round(window.innerHeight / 4));
const headingSelector = 'h1,h2,h3,h4,h5,h6';
const writes = new Map<string, Promise<unknown>>();

function contentBlocks(body: HTMLElement) {
  return Array.from(body.querySelectorAll<HTMLElement>(
    `[data-reading-block], :scope > .markdown > :is(${headingSelector},p,pre,ol,ul,blockquote,table)`,
  )).filter(block => !!block.textContent?.trim());
}

export function courseHeadings(body: HTMLElement) {
  return contentBlocks(body).filter(block => block.matches(headingSelector));
}

export function goToCourseHeading(heading: HTMLElement) {
  heading.tabIndex = -1;
  heading.focus({ preventScroll: true });
  scrollByPosition(heading.getBoundingClientRect().top, visibleLine());
}

function characterRects(block: HTMLElement) {
  const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT);
  const spans: { node: Text; start: number; end: number }[] = [];
  let length = 0;
  for (let node = walker.nextNode() as Text | null; node; node = walker.nextNode() as Text | null) {
    spans.push({ node, start: length, end: length + node.length });
    length += node.length;
  }
  const range = document.createRange();
  return (offset: number) => {
    const span = spans.find(item => item.end > offset);
    if (!span) return block.getBoundingClientRect();
    range.setStart(span.node, offset - span.start);
    range.setEnd(span.node, offset - span.start + 1);
    return range.getBoundingClientRect();
  };
}

function scrollByPosition(top: number, inset: number) {
  window.scrollTo({ top: window.scrollY + top - inset, behavior: 'instant' });
}

function capture(body: HTMLElement) {
  const line = visibleLine();
  if (window.scrollY < 12 || body.getBoundingClientRect().top > line + 40) return null;
  const blocks = contentBlocks(body);
  if (!blocks.length) return null;
  const firstVisible = blocks.findIndex(block => block.getBoundingClientRect().bottom > line);
  const block = firstVisible < 0 ? blocks.length - 1 : firstVisible;
  const text = blocks[block].textContent!;
  const rectFor = characterRects(blocks[block]);
  let start = 0;
  let end = text.length - 1;
  while (start < end) {
    const middle = Math.floor((start + end) / 2);
    if (rectFor(middle).bottom <= line) start = middle + 1;
    else end = middle;
  }
  const code = text.charCodeAt(start);
  const offset = code >= 0xdc00 && code <= 0xdfff && start > 0 ? start - 1 : start;
  const before = Array.from(text.slice(0, offset)).slice(-24).join('');
  const after = Array.from(text.slice(offset)).slice(0, 48).join('');
  return { point: { block, offset, excerpt: before + after, excerptOffset: before.length }, inset: rectFor(offset).top };
}

function reveal(body: HTMLElement, point: ReadingPoint, sameRevision: boolean, inset: number) {
  const blocks = contentBlocks(body);
  const expected = blocks[point.block];
  const excerptStart = point.offset - point.excerptOffset;
  if (sameRevision && expected?.textContent?.slice(excerptStart, excerptStart + point.excerpt.length) === point.excerpt) {
    scrollByPosition(characterRects(expected)(point.offset).top, inset);
    return true;
  }
  let match: { block: HTMLElement; offset: number } | undefined;
  for (const block of blocks) {
    const text = block.textContent!;
    if (point.excerpt.length < 12 && text !== point.excerpt) continue;
    const start = text.indexOf(point.excerpt);
    if (start < 0) continue;
    if (match || start !== text.lastIndexOf(point.excerpt)) return false;
    match = { block, offset: start + point.excerptOffset };
  }
  if (!match) return false;
  scrollByPosition(characterRects(match.block)(match.offset).top, inset);
  return true;
}

function saveInOrder(lesson: string, revision: string, point: ReadingPoint | null) {
  const previous = writes.get(lesson) ?? Promise.resolve();
  const saved = previous.catch(() => undefined)
    .then(() => recordReadingPosition(lesson, revision, point))
    .finally(() => { if (writes.get(lesson) === saved) writes.delete(lesson); });
  writes.set(lesson, saved);
  return saved;
}

type Reader = {
  lesson: string;
  body: HTMLElement | null;
  revision: string | null;
  initial: ReadingPosition | null;
  targeted: boolean;
  layout: string;
};

export type ReadingViewport = { y: number; visible: ReturnType<typeof capture>; revision: string | null };

export function useReadingPosition({ lesson, body, revision, initial, targeted, layout }: Reader) {
  const opened = useRef<{ body: HTMLElement; revision: string } | undefined>(undefined);
  const held = useRef<ReadingViewport | undefined>(undefined);
  const placedScroll = useRef<number | undefined>(undefined);
  const retry = useRef<(() => void) | undefined>(undefined);
  const [failure, setFailure] = useState<{ revision: string; message: string; changed: boolean }>();
  const remember = useCallback((): ReadingViewport => ({
    y: window.scrollY, visible: body ? capture(body) : null, revision,
  }), [body, revision]);
  const restore = useCallback((position: ReadingViewport) => {
    if (!body || !position.visible || !reveal(body, position.visible.point, position.revision === revision, position.visible.inset)) {
      window.scrollTo({ top: position.y, behavior: 'instant' });
    }
    placedScroll.current = window.scrollY;
  }, [body, revision]);

  useLayoutEffect(() => {
    if (!body || !revision || (opened.current?.body === body && opened.current.revision === revision)) return;
    opened.current = { body, revision };
    if (targeted || !initial) return;
    if (reveal(body, initial.point, initial.revision === revision, visibleLine())) {
      placedScroll.current = window.scrollY;
    }
  }, [body, initial, revision, targeted]);

  useLayoutEffect(() => {
    const position = held.current;
    held.current = undefined;
    if (!position) return;
    restore(position);
  }, [layout, restore]);

  useEffect(() => {
    const article = body;
    const bodyRevision = revision;
    if (!article || !bodyRevision) return;
    let checkpoint: ReadingPoint | null = null;
    let sampledAt = -Infinity;
    let latest = '';
    let touched = false;
    let timer: number;
    let current = true;

    function sample() {
      if (!article?.isConnected) return;
      checkpoint = capture(article)?.point ?? null;
      sampledAt = performance.now();
    }
    function save() {
      if (!touched) return;
      const key = JSON.stringify(checkpoint);
      if (latest === key) return;
      latest = key;
      void saveInOrder(lesson, bodyRevision!, checkpoint).then(() => {
        if (current) setFailure(undefined);
      }).catch(error => {
        if (!current || latest !== key) return;
        latest = '';
        setFailure({ revision: bodyRevision!, message: error instanceof Error ? error.message : '阅读位置暂未保存。',
          changed: error instanceof ApiError && error.status === 409 });
      });
    }
    function finishScroll() {
      if (!touched) return;
      sample();
      save();
    }
    function onScroll() {
      const placed = placedScroll.current;
      placedScroll.current = undefined;
      if (placed === window.scrollY) return;
      const now = performance.now();
      touched = true;
      if (now - sampledAt > 90) sample();
      window.clearTimeout(timer);
      timer = window.setTimeout(finishScroll, 350);
    }
    function onHidden() {
      if (document.visibilityState === 'hidden') finishScroll();
    }
    retry.current = finishScroll;
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('pagehide', finishScroll);
    document.addEventListener('visibilitychange', onHidden);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener('scroll', onScroll);
      window.removeEventListener('pagehide', finishScroll);
      document.removeEventListener('visibilitychange', onHidden);
      save();
      retry.current = undefined;
      current = false;
    };
  }, [body, lesson, revision]);

  return {
    hold: () => { held.current = remember(); },
    remember, restore,
    failure: failure?.revision === revision ? failure : undefined,
    retry: () => retry.current?.(),
  };
}
