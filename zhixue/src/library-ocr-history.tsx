import { useState } from 'react';
import { api } from './api';
import { NoteDiff } from './notes';
import { BackedStatus, Empty, ErrorNote, Modal, useAction, useRead } from './shared';

type Version = { sourceRevision: string; textRevision: string; savedAt: string | null };
type Versions = { currentTextRevision: string | null; versions: Version[] };
type PageCatalog = Versions & { pageCount: number; pages: number[] };
type Comparison = {
  selected: Version & { text: string };
  current: { textRevision: string; text: string } | null;
  diff: string[];
  canRestore: boolean;
};

const dateTime = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'short', timeStyle: 'medium', timeZone: 'Asia/Shanghai' });

export function OcrHistory({ fileId, initialPage, revision, close, restored }: {
  fileId: string; initialPage: number; revision: string; close: () => void;
  restored: (page: number, textRevision: string) => void;
}) {
  const [page, setPage] = useState(initialPage);
  const [chosen, choose] = useState<string>();
  const action = useAction();
  const base = `/files/${encodeURIComponent(fileId)}/pages`;
  const path = `${base}/${page}/ocr/versions`;
  const query = new URLSearchParams({ revision });
  const pageList = new URLSearchParams({ revision, includePages: 'true' });
  const catalog = useRead<PageCatalog>(`${base}/${initialPage}/ocr/versions?${pageList}`);
  const otherPage = useRead<Versions>(page === initialPage ? null : `${path}?${query}`);
  const history = page === initialPage ? catalog : otherPage;
  const pages = [...new Set([initialPage, ...(catalog.value?.pages ?? [])])].sort((a, b) => a - b);
  const pageCount = catalog.value?.pageCount ?? initialPage;
  const removed = page > pageCount;
  const versions = history.value?.versions ?? [];
  const current = history.value?.currentTextRevision;
  const selected = versions.find(version => version.textRevision === chosen)
    ?? versions.find(version => version.textRevision !== current) ?? versions[0];
  const compare = useRead<Comparison>(selected ? `${path}/${selected.textRevision}?${query}` : null);
  const detail = compare.value;

  function reload() {
    catalog.refresh();
    if (page !== initialPage) otherPage.refresh();
    compare.refresh();
  }

  function restore() {
    if (!detail?.canRestore) return;
    void action.run(async () => {
      const saved = await api<{ textRevision: string }>(`${path}/${detail.selected.textRevision}/restore`, 'POST', {
        revision, textRevision: detail.current?.textRevision ?? null,
      });
      restored(page, saved.textRevision);
    });
  }

  return <Modal title="校对版本" onClose={close} busy={action.pending}>
    <div className="project-dialog-form">
      {pages.length > 1 && <label className="project-field">选择校对页
        <select value={page} disabled={action.pending} onChange={event => {
          setPage(Number(event.target.value));
          choose(undefined);
        }}>
          {pages.map(number => <option key={number} value={number}>
            第 {number} 页{number > pageCount ? ' · 仅历史' : ''}
          </option>)}
        </select>
      </label>}
      {removed && <p className="notice">当前原件已没有第 {page} 页，可查看历史校对。</p>}
      {!history.value ? <BackedStatus error={history.error} retry={reload} />
        : !selected ? <Empty title="本页暂无已保存的校对版本" />
        : <>
          <ErrorNote message={history.error} retry={reload} />
          <label className="project-field">选择校对版本
            <select value={selected.textRevision} disabled={action.pending} onChange={event => choose(event.target.value)}>
              {versions.map((version, index) => <option key={version.textRevision} value={version.textRevision}>
                {versions.length - index} · {version.textRevision === current ? '当前校对' : version.sourceRevision !== revision ? '旧原件' : '历史校对'}
                {' · '}{version.savedAt ? dateTime.format(new Date(version.savedAt)) : '保存时间未记录'}
              </option>)}
            </select>
          </label>
          {!detail ? <BackedStatus error={compare.error} retry={reload} /> : <>
            {!removed && detail.selected.sourceRevision !== revision && <p className="notice">此版本来自旧原件，可查看文字。</p>}
            <details className="note-copy" open><summary>所选校对文字</summary><pre>{detail.selected.text}</pre></details>
            {!removed && <details className="note-copy"><summary>当前校对文字</summary>
              {detail.current ? <pre>{detail.current.text}</pre> : <p>当前原件还没有已保存的校对。</p>}
            </details>}
            <NoteDiff lines={detail.diff} />
          </>}
          <ErrorNote message={action.error || (detail ? compare.error : undefined)} />
        </>}
      <div className="project-dialog-actions">
        <button type="button" className="button secondary" disabled={action.pending} onClick={close}>关闭</button>
        {action.error && <button type="button" className="button secondary" onClick={reload}>刷新版本比较</button>}
        {detail?.canRestore && <button type="button" className="button primary" disabled={action.pending} onClick={restore}>
          {action.pending ? '正在恢复' : '恢复此校对版本'}
        </button>}
      </div>
    </div>
  </Modal>;
}
