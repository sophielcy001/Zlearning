import { Fragment, useMemo } from 'react';
import { LibraryContextLink } from './library-views';
import { wikiReferences, wikiRoute } from './wiki-markdown';

export function WikiText({ text, highlight }: { text: string; highlight?: { start: number; end: number } }) {
  const links = useMemo(() => wikiReferences(text), [text]);
  const range = useMemo(() => {
    if (!highlight) return undefined;
    const points = [...text];
    if (highlight.end <= highlight.start || highlight.end > points.length) return undefined;
    const start = points.slice(0, highlight.start).join('').length;
    return { start, end: start + points.slice(highlight.start, highlight.end).join('').length };
  }, [text, highlight]);

  function passage(start: number, end: number) {
    if (!range || range.start >= end || range.end <= start) return text.slice(start, end);
    const a = Math.max(start, range.start);
    const b = Math.min(end, range.end);
    return <>{text.slice(start, a)}<mark>{text.slice(a, b)}</mark>{text.slice(b, end)}</>;
  }

  let tail = 0;
  return <>{links.map(({ start, end, target }) => {
    const textBefore = passage(tail, start);
    tail = end;
    return <Fragment key={start}>{textBefore}<LibraryContextLink to={wikiRoute(target)}>{passage(start, end)}</LibraryContextLink></Fragment>;
  })}{passage(tail, text.length)}</>;
}
