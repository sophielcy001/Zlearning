import { useState, type FormEvent } from 'react';
import { Link2 } from 'lucide-react';
import { api } from './api';
import { ErrorNote, Modal, useAction } from './shared';

type WebImportResult =
  | { status: 'created'; id: string; name: string }
  | { status: 'duplicate'; id: string; name: string; revision: string };

function ImportWebDialog({ onImport, close }: { onImport: (id: string) => void; close: () => void }) {
  const [address, setAddress] = useState('');
  const [duplicate, setDuplicate] = useState<Extract<WebImportResult, { status: 'duplicate' }>>();
  const action = useAction();

  function imported(id: string) {
    close();
    onImport(id);
  }

  function fetchPage(event: FormEvent) {
    event.preventDefault();
    setDuplicate(undefined);
    void action.run(async () => {
      const result = await api<WebImportResult>('/files/web', 'POST', { url: address.trim() });
      if (result.status === 'duplicate') setDuplicate(result);
      else imported(result.id);
    });
  }

  return <Modal title="导入公开网页" onClose={close} busy={action.pending}>
    <form onSubmit={fetchPage} className="web-import-form">
      <label className="project-field">网页地址
        <input type="url" autoFocus value={address} placeholder="https://…" required maxLength={2048}
          disabled={action.pending} onChange={event => { setAddress(event.target.value); setDuplicate(undefined); }} />
      </label>
      <p className="file-status">保存当前 HTML 快照及来源地址，从网页正文提取可搜索的文字。</p>
      <ErrorNote message={action.error} />
      {duplicate ? <div className="notice web-duplicate">
        <strong>已有相同的网页快照</strong>
        <span>{duplicate.name}</span>
        <div className="action-row">
          <button type="button" className="button secondary" disabled={action.pending}
            onClick={() => imported(duplicate.id)}>打开已有快照</button>
          <button type="button" className="button primary" disabled={action.pending} onClick={() => void action.run(async () => {
            const copy = await api<{ id: string }>('/files/web/copy', 'POST', { fileId: duplicate.id, revision: duplicate.revision });
            imported(copy.id);
          })}>保留新副本</button>
        </div>
      </div> : <div className="action-row">
        <button className="button primary" disabled={action.pending || !address.trim()}>
          {action.pending ? '正在获取网页' : '保存网页快照'}
        </button>
        <button type="button" className="button secondary" disabled={action.pending} onClick={close}>取消</button>
      </div>}
    </form>
  </Modal>;
}

export function WebImportButton({ onImport }: { onImport: (id: string) => void }) {
  const [show, setShow] = useState(false);
  return <>
    <button type="button" className="button secondary" onClick={() => setShow(true)}><Link2 size={17} />导入网页</button>
    {show && <ImportWebDialog onImport={onImport} close={() => setShow(false)} />}
  </>;
}
