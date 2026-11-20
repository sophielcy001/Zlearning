import { useState } from 'react';
import { Link } from 'react-router-dom';
import { api, type LibraryItem } from './api';
import { LIBRARY_ROOT, type LibraryFolder } from './library-folders';
import { BackedStatus, Empty, ErrorNote, Modal, useAction } from './shared';

export const CLASSIFICATION = '/files/classification';
export const CLASSIFICATION_FILE_LIMIT = 16;
const FOLDER_LIMIT = 12;
const PAGE_SIZE = 12;
const KINDS: Record<LibraryItem['kind'], string> = { course: '课程', note: '笔记', import: '导入资料', supplement: '补充讲解' };

type RequestFile = Pick<LibraryItem, 'id' | 'name'>;
type Inputs = { id: string; fileIds: string[]; folders: string[]; withExcerpts: boolean; autoApply: boolean };
type Recipient = { name: string; model: string; address: string };
type Scope = {
  consent: { connectionId: string; revision: number; maxCalls: number; token: string };
  recipient: Recipient;
  autoApply: boolean;
  files: { key: string; title: string; kind: LibraryItem['kind']; format: string; tags: string[];
    excerpts: { label: string; text: string }[] }[];
  folders: { key: string; name: string }[];
};
type JobState = 'queued' | 'running' | 'blocked' | 'ready' | 'failed' | 'cancelled';
type ClassificationJob = { id: string; state: JobState; recipient: Recipient; count: number; fileCount: number;
  autoApply: boolean; error: string | null; usage?: { inputTokens?: number; outputTokens?: number } };
type Candidate = {
  id: string; title: string; path: string; destination: string | null; tags: string[]; reason: string;
  state: 'pending' | 'approved' | 'applied' | 'partial' | 'stale' | 'rejected';
  result: { tags: string[]; path: string | null; issue: string | null } | null;
};
export type ClassificationQueue = { jobs: ClassificationJob[]; pending: Candidate[]; history: Candidate[] };
type Choice = 'apply' | 'tags' | 'move' | 'reject' | 'resume';

function folderLabel(path: string) {
  return path === LIBRARY_ROOT ? '知识库根目录' : path.slice(LIBRARY_ROOT.length + 1);
}

export function ClassificationRequest({ files, folders, close, queued }: {
  files: RequestFile[]; folders: LibraryFolder[]; close: () => void; queued: () => void;
}) {
  const [destinations, setDestinations] = useState<string[]>([]);
  const [search, setSearch] = useState('');
  const [withExcerpts, setWithExcerpts] = useState(true);
  const [autoApply, setAutoApply] = useState(false);
  const [preview, setPreview] = useState<{ data: Inputs; scope: Scope }>();
  const action = useAction();
  const matchingFolders = folders.filter(folder => folderLabel(folder.path).toLowerCase().includes(search.trim().toLowerCase()));

  function inspect() {
    const data = { id: crypto.randomUUID(), fileIds: files.map(file => file.id), folders: destinations, withExcerpts, autoApply };
    void action.run(async () => {
      const scope = await api<Scope>(CLASSIFICATION + '/preview', 'POST', data);
      setPreview({ data, scope });
    });
  }

  function send() {
    if (!preview) return;
    void action.run(async () => {
      await api(CLASSIFICATION, 'POST', { ...preview.data, consent: preview.scope.consent });
      queued();
    });
  }

  return <Modal title="AI 整理文件" onClose={close} busy={action.pending}>
    <div className="project-dialog-form">
      {preview ? <>
        <div className="classification-recipient">
          <strong>{preview.scope.recipient.name} · {preview.scope.recipient.model}</strong>
          <small>{preview.scope.recipient.address}</small>
          <small>本批最多调用模型 {preview.scope.consent.maxCalls} 次</small>
        </div>
        <p className="dialog-hint">{preview.scope.autoApply
          ? '收到结果后自动添加标签并尝试移入所选目录。原件变化或目标重名时，逐项记录结果。'
          : '结果保存到 AI 整理记录，审核后即可采用标签和推荐目录。'}</p>
        <h3>将发送的文件资料</h3>
        <ul className="classification-material">
          {preview.scope.files.map(file => <li key={file.key}>
            <strong>{file.title}</strong>
            <small>{KINDS[file.kind]} · {file.format.toUpperCase()}{file.tags.length ? ' · 标签 ' + file.tags.join('、') : ''}</small>
            {file.excerpts.map((part, index) => <div className="classification-excerpt" key={index}>
              <small>{part.label}</small><p>{part.text}</p>
            </div>)}
          </li>)}
        </ul>
        {preview.scope.folders.length > 0 && <div>
          <h3>将发送的目标目录名称</h3>
          <ul className="file-tags">{preview.scope.folders.map(folder => <li key={folder.key}>{folder.name}</li>)}</ul>
        </div>}
        <div className="project-dialog-actions">
          <button type="button" className="button secondary" disabled={action.pending} onClick={() => setPreview(undefined)}>返回设置范围</button>
          <button type="button" className="button primary" disabled={action.pending} onClick={send}>
            {preview.scope.autoApply ? '确认发送并自动整理' : '确认发送并排队'}
          </button>
        </div>
      </> : <>
        <ul className="file-tags" aria-label="所选文件">{files.map(file => <li key={file.id}>{file.name}</li>)}</ul>
        <label className="classification-check">
          <input type="checkbox" checked={withExcerpts} onChange={event => setWithExcerpts(event.target.checked)} disabled={action.pending} />
          发送可用的已索引正文摘录，每份最多 1,000 字
        </label>
        <fieldset className="classification-folders">
          <legend>允许归档到这些文件夹</legend>
          <p className="dialog-hint">留空时生成标签建议。已选 {destinations.length} / {FOLDER_LIMIT}</p>
          <input type="search" aria-label="筛选目标文件夹" placeholder="搜索目标文件夹"
            value={search} onChange={event => setSearch(event.target.value)} disabled={action.pending} />
          {matchingFolders.length ? <div className="classification-folder-list">{matchingFolders.map(folder => {
            const chosen = destinations.includes(folder.path);
            return <label className="classification-check" key={folder.path}>
              <input type="checkbox" checked={chosen} disabled={action.pending || !chosen && destinations.length >= FOLDER_LIMIT}
                onChange={() => setDestinations(current => chosen ? current.filter(path => path !== folder.path) : [...current, folder.path])} />
              <span>{folderLabel(folder.path)}</span>
            </label>;
          })}</div> : <p className="dialog-hint">没有匹配的文件夹。</p>}
        </fieldset>
        <label className="classification-check">
          <input type="checkbox" checked={autoApply} onChange={event => setAutoApply(event.target.checked)} disabled={action.pending} />
          自动应用本批建议
        </label>
        <div className="project-dialog-actions">
          <button type="button" className="button primary" disabled={action.pending} onClick={inspect}>预览发送范围</button>
        </div>
      </>}
      <ErrorNote message={action.error} />
      {!!action.error && !preview && <Link className="text-action" to="/settings/models">查看 API 与模型设置</Link>}
    </div>
  </Modal>;
}

const JOB_LABELS: Record<JobState, string> = {
  queued: '已排队', running: '正在生成建议', blocked: '等待调用额度', ready: '建议已返回', failed: '整理未完成', cancelled: '已停止',
};
const CANDIDATE_LABELS: Record<Candidate['state'], string> = {
  pending: '待审核', approved: '已确认待完成', applied: '已应用', partial: '部分应用', stale: '已跳过', rejected: '已拒绝',
};

export function ClassificationReview({ queue, error, refresh, close }: {
  queue?: ClassificationQueue; error?: string; refresh: () => void; close: () => void;
}) {
  const [picked, setPicked] = useState<string[]>([]);
  const [page, setPage] = useState(0);
  const [summary, setSummary] = useState('');
  const action = useAction();
  const pending = queue?.pending ?? [];
  const lastPage = Math.max(0, Math.ceil(pending.length / PAGE_SIZE) - 1);
  const currentPage = Math.min(page, lastPage);
  const visible = pending.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE);
  const chosen = visible.filter(item => item.state === 'pending' && picked.includes(item.id));

  function review(choice: Choice, ids: string[]) {
    void action.run(async () => {
      const outcome = await api<{ reviewed: Candidate[]; skipped: { reason: string }[] }>(CLASSIFICATION + '/review', 'POST', { ids, choice });
      const finished = outcome.reviewed.filter(item => item.state !== 'approved').length;
      const skipped = outcome.skipped.length ? ' ' + [...new Set(outcome.skipped.map(item => item.reason))].join(' ') : '';
      setSummary((finished ? `已处理 ${finished} 项。` : '当前没有已处理的选中项。') + skipped);
      setPicked([]);
      refresh();
    });
  }

  function cancel(job: ClassificationJob) {
    void action.run(async () => {
      await api(`${CLASSIFICATION}/${job.id}/cancel`, 'POST');
      refresh();
    });
  }

  return <Modal title="AI 整理记录" onClose={close} busy={action.pending}>
    <div className="project-dialog-form">
      <div className="action-row">
        <button type="button" className="button secondary" disabled={action.pending} onClick={refresh}>刷新整理记录</button>
      </div>
      {summary && <p className="notice success" role="status">{summary}</p>}
      {!queue ? <BackedStatus error={error} retry={refresh} /> : <>
        <ErrorNote message={error} retry={refresh} />
        {visible.length ? <>
          <div className="action-row" aria-label="审核选中建议">
            <button type="button" className="button primary" disabled={!chosen.length || action.pending}
              onClick={() => review('apply', chosen.map(item => item.id))}>采用所选建议</button>
            <button type="button" className="button secondary" disabled={!chosen.some(item => item.tags.length) || action.pending}
              onClick={() => review('tags', chosen.map(item => item.id))}>只添加标签</button>
            <button type="button" className="button secondary" disabled={!chosen.some(item => item.destination) || action.pending}
              onClick={() => review('move', chosen.map(item => item.id))}>只移动文件</button>
            <button type="button" className="button secondary" disabled={!chosen.length || action.pending}
              onClick={() => review('reject', chosen.map(item => item.id))}>拒绝所选</button>
          </div>
          <ul className="classification-material" aria-label="待审核的分类和标签">
            {visible.map(item => <li key={item.id}>
              {item.state === 'pending' ? <label className="classification-check">
                <input type="checkbox" checked={picked.includes(item.id)} disabled={action.pending}
                  onChange={() => setPicked(current => current.includes(item.id) ? current.filter(id => id !== item.id) : [...current, item.id])}
                  aria-label={'选择建议 ' + item.title} />
                <strong>{item.title}</strong>
              </label> : <><strong>{item.title}</strong><small>已确认待完成</small></>}
              <small>{item.path}</small>
              {item.destination && <p>归档到 {folderLabel(item.destination)}</p>}
              {!!item.tags.length && <ul className="file-tags">{item.tags.map(tag => <li key={tag}>{tag}</li>)}</ul>}
              <small>{item.reason}</small>
              {item.state === 'approved' && <button type="button" className="button secondary" disabled={action.pending}
                onClick={() => review('resume', [item.id])}>继续已确认操作</button>}
            </li>)}
          </ul>
          {lastPage > 0 && <div className="action-row">
            <button type="button" className="button secondary" disabled={!currentPage || action.pending}
              onClick={() => { setPage(currentPage - 1); setPicked([]); }}>上一页</button>
            <small>{currentPage + 1} / {lastPage + 1}</small>
            <button type="button" className="button secondary" disabled={currentPage === lastPage || action.pending}
              onClick={() => { setPage(currentPage + 1); setPicked([]); }}>下一页</button>
          </div>}
        </> : <Empty title="暂无待审核的分类和标签" />}
        {!!queue.jobs.length && <details open>
          <summary>最近的整理请求</summary>
          <ul className="classification-material">
            {queue.jobs.map(job => <li key={job.id}>
              <strong>{JOB_LABELS[job.state]} · {job.fileCount} 份文件</strong>
              <small>{job.recipient.model} · {job.autoApply ? '自动应用' : '生成待审核建议'}</small>
              {job.state === 'ready' && <small>返回 {job.count} 项建议</small>}
              {job.error && <p>{job.error}</p>}
              {job.state === 'blocked' && <Link className="text-action" to="/settings/budget">打开额度恢复</Link>}
              {['queued', 'running', 'blocked'].includes(job.state) && <button type="button" className="button secondary" disabled={action.pending}
                onClick={() => cancel(job)}>停止此请求</button>}
            </li>)}
          </ul>
        </details>}
        {!!queue.history.length && <details open>
          <summary>最近处理结果</summary>
          <ul className="classification-material">
            {queue.history.map(item => <li key={item.id}>
              <strong>{item.title} · {CANDIDATE_LABELS[item.state]}</strong>
              {!!item.result?.tags.length && <p>添加标签 {item.result.tags.join('、')}</p>}
              {item.result?.path && <small>当前位置 {item.result.path}</small>}
              {item.result?.issue && <p>{item.result.issue}</p>}
            </li>)}
          </ul>
        </details>}
      </>}
      <ErrorNote message={action.error} />
    </div>
  </Modal>;
}
