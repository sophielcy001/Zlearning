import { useState } from 'react';
import { api } from './api';
import type { GraphData } from './graph-model';
import { ErrorNote, Modal, useAction } from './shared';

type ImageFormat = 'png' | 'svg';
type ImportMode = 'merge' | 'restore';
type Preview = {
  matched: number; skipped: number; confirmation: string;
  changes: { positions: number; links: number; visibility: number; groups: number; favorites: number; view: number };
};
type Selection = { payload: { revision: number; mode: ImportMode; archive: object }; preview: Preview };

const LIMIT = 12 * 1024 * 1024;
const LABELS = { positions: '节点位置', links: '连线设置', visibility: '节点可见性', groups: '分组', favorites: '收藏视角', view: '当前视角' };

function download(blob: Blob, format: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = '智学-知识图谱.' + format;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 2000);
}

export function GraphTransfer({ revision, close, capture, imported }: {
  revision: number; close: () => void; capture: (format: ImageFormat) => Promise<Blob>; imported: (data: GraphData) => void;
}) {
  const tasks = useAction();
  const [file, setFile] = useState<File>();
  const [mode, setMode] = useState<ImportMode>('merge');
  const [selection, setSelection] = useState<Selection>();
  const changeCount = Object.values(selection?.preview.changes ?? {}).reduce((total, count) => total + count, 0);

  function exportFile(format: ImageFormat | 'json') {
    void tasks.run(async () => {
      const blob = format === 'json'
        ? new Blob([JSON.stringify(await api('/library/graph/archive'), null, 2)], { type: 'application/json' })
        : await capture(format);
      download(blob, format);
    });
  }

  function previewFile() {
    if (!file) return;
    setSelection(undefined);
    void tasks.run(async () => {
      if (!file.size || file.size > LIMIT) throw new Error('请选择不超过 12 MiB 的图谱 JSON。');
      let archive: object;
      try {
        const raw = JSON.parse((await file.text()).replace(/^\uFEFF/, ''));
        if (!raw || raw.format !== 'zhixue-library-graph' || raw.version !== 1) throw new Error();
        archive = raw;
      } catch {
        throw new Error('请选择智学导出的图谱 JSON 文件。');
      }
      const payload = { revision, mode, archive };
      const preview = await api<Preview>('/library/graph/import/preview', 'POST', payload);
      setSelection({ payload, preview });
    });
  }

  function confirm() {
    if (!selection) return;
    void tasks.run(async () => {
      const data = await api<GraphData>('/library/graph/import', 'POST', {
        ...selection.payload, confirmation: selection.preview.confirmation,
      });
      imported(data);
      close();
    });
  }

  return <Modal title="图谱导入与导出" busy={tasks.pending} onClose={close}>
    <div className="graph-dialog-form">
      <div className="action-row">
        <button type="button" className="button secondary" disabled={tasks.pending} onClick={() => exportFile('png')}>导出 PNG</button>
        <button type="button" className="button secondary" disabled={tasks.pending} onClick={() => exportFile('svg')}>导出 SVG</button>
        <button type="button" className="button secondary" disabled={tasks.pending} onClick={() => exportFile('json')}>导出 JSON</button>
      </div>
      <p className="muted">图片使用当前画布视角。JSON 保存图谱设置和关联，原文件独立保留。</p>
      <label>选择图谱 JSON<input type="file" accept=".json,application/json" disabled={tasks.pending} onChange={event => {
        setFile(event.target.files?.[0]); setSelection(undefined);
      }} /></label>
      <label>导入方式<select value={mode} disabled={tasks.pending} onChange={event => {
        setMode(event.target.value as ImportMode); setSelection(undefined);
      }}>
        <option value="merge">合并到现有图谱</option><option value="restore">从文件恢复图谱</option>
      </select></label>
      <p className="muted">{mode === 'merge' ? '合并时保留已有的手动位置及设置，补入匹配节点的其他设置。'
        : '恢复会按文件应用图谱的位置、连线、分组、隐藏和视角设置。可以在图谱历史中回退编辑。'}</p>
      <div className="action-row"><button type="button" className="button secondary" disabled={tasks.pending || !file} onClick={previewFile}>预览导入</button></div>
      {selection && <div className="graph-import-preview" aria-label="图谱导入预览">
        <p role="status">匹配 {selection.preview.matched} 个节点 · 跳过 {selection.preview.skipped} 个未匹配节点</p>
        {changeCount ? <ul>{Object.entries(selection.preview.changes).filter(([, count]) => count > 0)
          .map(([kind, count]) => <li key={kind}>{LABELS[kind as keyof typeof LABELS]} · {count} 项</li>)}</ul>
          : <p className="muted">没有需要应用的图谱变更。</p>}
        <button type="button" disabled={!changeCount || tasks.pending} className="button primary" onClick={confirm}>确认导入</button>
      </div>}
      <ErrorNote message={tasks.error} />
    </div>
  </Modal>;
}
