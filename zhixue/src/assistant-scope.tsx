import { useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from './api';
import { BackupDestinations, BackupOptions, backupSelection, type ModelBackups } from './model-backup';
import { ErrorNote, Modal, useAction } from './shared';
import type { Citation, Reviewer } from './verification';

export type AssistantSettings = {
  granted: boolean; authorized: boolean; version: number | null; maxCalls: number | null; calls: number; backups?: ModelBackups;
};
type Preview = {
  main: Reviewer; backups?: ModelBackups; version: number; maxCalls: number | null; confirmation: string;
  sections: { index: number; title: string; text: string; citations: Citation[] }[];
};

export function AssistantScope({ endpoint, existing, existingBackups, close, saved }: {
  endpoint: string; existing: number | null; existingBackups?: ModelBackups; close: () => void; saved: (settings: AssistantSettings) => void;
}) {
  const [limit, setLimit] = useState(existing === null ? '' : String(existing));
  const [backups, setBackups] = useState(() => backupSelection(existingBackups));
  const [preview, setPreview] = useState<Preview>();
  const action = useAction();
  return <Modal title="确认课内答疑发送范围" onClose={close} busy={action.pending}>
    <div className="project-dialog-form">
      <form className="generation-preview-form" onSubmit={event => {
        event.preventDefault();
        void action.run(async () => {
          setPreview(undefined);
          setPreview(await api<Preview>(`${endpoint}/preview`, 'POST', { maxCalls: limit ? Number(limit) : null, ...backups }));
        });
      }}>
        <label className="project-field">本节答疑调用次数上限
          <input type="number" min={1} max={10000} step={1} value={limit} placeholder="可留空"
            disabled={action.pending} onChange={event => { setLimit(event.target.value); setPreview(undefined); }} />
        </label>
        <BackupOptions selection={backups} mainOnly disabled={action.pending} onChange={value => {
          setBackups(value); setPreview(undefined);
        }} />
        <button type="submit" className="button secondary" disabled={action.pending}>预览发送范围</button>
      </form>
      {preview && <>
        <div className="generation-destination"><strong>{preview.main.name} · {preview.main.model}</strong><code>{preview.main.address}</code></div>
        <BackupDestinations backups={preview.backups} />
        <p>授权课程第 {preview.version} 版的课内答疑。提交时发送当前问题、主动选择的原文、最多三个相关核验段落及引文，以及本对话同版本上一轮有可定位来源的问答。调用计入每日额度。</p>
        <p className="model-help">选中的争议文字作为待辨析的内容。笔记、附件和其他对话留在本地。</p>
        <div className="assessment-scope">{preview.sections.map(section => <details key={section.index} open={preview.sections.length === 1}>
          <summary>{section.title}</summary><p className="evidence-text">{section.text}</p>
          {section.citations.map((entry, index) => <blockquote key={index}>{entry.quote}</blockquote>)}
        </details>)}</div>
      </>}
      <ErrorNote message={action.error} />
      {action.error && <Link to="/settings/models" className="text-action">打开 API 与模型设置</Link>}
      <div className="project-dialog-actions">
        <button type="button" className="button secondary" disabled={action.pending} onClick={close}>取消</button>
        <button type="button" className="button primary" disabled={!preview || action.pending} onClick={() => {
          if (preview) void action.run(async () => {
            const next = await api<AssistantSettings>(`${endpoint}/authorize`, 'POST', {
              maxCalls: preview.maxCalls, confirmation: preview.confirmation, ...backups,
            });
            saved(next);
          });
        }}>确认并启用课内答疑</button>
      </div>
    </div>
  </Modal>;
}
