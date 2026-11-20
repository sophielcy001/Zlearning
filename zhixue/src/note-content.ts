export const maxNoteLength = 50_000;

export function sourceOffset(text: string, caret: number) {
  // A textarea exposes CRLF as a single LF.
  let raw = caret;
  for (const match of text.matchAll(/\r\n/g)) {
    if (match.index >= raw) break;
    raw++;
  }
  return raw;
}

export function fencedCode(text: string, info = '') {
  const usesTildes = info.includes('\x60');
  const runs = text.match(usesTildes ? /~+/g : /\x60+/g) ?? [];
  const length = runs.reduce((longest, run) => Math.max(longest, run.length + 1), 3);
  const fence = (usesTildes ? '~' : '\x60').repeat(length);
  return fence + info + '\n' + text + '\n' + fence;
}
