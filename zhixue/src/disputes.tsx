import { useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { api, courseUrl, projectUrl } from './api';
import { EvidenceLinks } from './course-reading';
import { refreshReminders } from './reminders';
import { BackedStatus, Empty, ErrorNote, Modal, useAction, useRead } from './shared';
import { verificationLabels, type Reviewer, type VerifiedSection } from './verification';

type Item = VerifiedSection & { lessonId: string; lessonTitle: string; projectId: string; projectTopic: string;
  findingId: string; reviewer: Reviewer; foundAt: string };
type Subject = Pick<Item, 'lessonId' | 'index' | 'findingId'>;
type Mode = 'adopt' | 'recheck' | 'later';
type ItemResult = Subject & { title?: string; status: 'ready' | 'applied' | 'unavailable'; error?: string };
type Batch = { action: Mode; items: Subject[] };
type Preview = { confirmation: string; items: ItemResult[] };
type Listing = { items: Item[]; courses: { id: string; title: string; topic: string }[]; total: number; hasMore: boolean };

const actions: Record<Mode, string> = { adopt: '采纳进正文', recheck: '请求重新核验', later: '稍后处理' };
const dispositions = { pending: '待处理', adopted: '已采纳', later: '稍后处理' };
const keyOf = (item: Subject) => `${item.lessonId}/${item.index}/${item.findingId}`;

function DisputeDetail({ item, selectAction }: { item: Item; selectAction: (mode: Mode) => void }) {
  return <article className="panel dispute-detail">
    <h2>{item.title}</h2>
    <p className="model-help">{item.lessonTitle} · {verificationLabels[item.state]} · {dispositions[item.disposition]}</p>
    <blockquote className="evidence-text">{item.text}</blockquote>
    <div className="dispute-section"><h3>复核意见</h3>
      <p className="model-help">{item.reviewer.name} · {item.reviewer.model}</p><p>{item.reason}</p>
      {item.detail && <p>{item.detail}</p>}
    </div>
    <div className="dispute-section"><h3>依据</h3>
      {item.citations.length ? <EvidenceLinks entries={item.citations} /> : <p className="model-help">当前没有可定位的依据</p>}
    </div>
    <div className="action-row">
      {item.disposition !== 'adopted' && <button type="button" className="button secondary" onClick={() => selectAction('adopt')}>采纳进正文</button>}
      <button type="button" className="button secondary" onClick={() => selectAction('recheck')}>请求重新核验</button>
      {item.disposition === 'pending' && <button type="button" className="text-action" onClick={() => selectAction('later')}>稍后处理</button>}
    </div>
    <div className="action-row"><Link className="text-action" to={projectUrl(item.projectId)}>课程项目</Link>
      <Link className="text-action" to={courseUrl(item.lessonId)}>查看当前课程</Link>
    </div>
  </article>;
}

function BatchDialog({ batch, preview, close, completed }: {
  batch: Batch; preview: Preview; close: () => void; completed: (items: ItemResult[]) => void;
}) {
  const action = useAction();
  return <Modal title={actions[batch.action]} onClose={close} busy={action.pending}>
    <div className="project-dialog-form">
      {batch.action === 'adopt' && <p>选中的片段将带有「用户采纳，尚未核验通过」标记进入正文。</p>}
      {batch.action === 'recheck' && <p>按项目授权的资料重新排队，实际请求计入任务额度。</p>}
      <ul className="scope-items">
        {preview.items.map(item => <li key={keyOf(item)}>
          <span>{item.title ?? `片段 ${item.index + 1}`}</span><span>{item.error ?? '待确认'}</span>
        </li>)}
      </ul>
      <ErrorNote message={action.error} />
      <div className="project-dialog-actions">
        <button type="button" className="button secondary" disabled={action.pending} onClick={close}>取消</button>
        <button type="button" className="button primary" disabled={action.pending || !preview.items.some(item => item.status === 'ready')}
          onClick={() => void action.run(async () => {
            const result = await api<{ items: ItemResult[] }>('/disputes/apply', 'POST', { ...batch, confirmation: preview.confirmation });
            refreshReminders();
            completed(result.items.map((item, index) => ({ ...item, title: preview.items[index]?.title })));
          })}>确认{actions[batch.action]}</button>
      </div>
    </div>
  </Modal>;
}

export function DisputesPage({ queued, onChange }: { queued: boolean; onChange: () => void }) {
  const [params, setParams] = useSearchParams();
  const course = params.get('lesson') ?? '';
  const status = params.get('status') ?? 'pending';
  const page = Math.max(1, Number(params.get('page')) || 1);
  const query = new URLSearchParams({ status, page: String(page), ...(course ? { lesson: course } : {}) });
  const read = useRead<Listing>(`/disputes?${query}`);
  const { refresh } = read;
  const [selected, setSelected] = useState('');
  const [checked, setChecked] = useState<string[]>([]);
  const [confirmation, setConfirmation] = useState<{ batch: Batch; preview: Preview }>();
  const [results, setResults] = useState<ItemResult[]>([]);
  const action = useAction();
  useEffect(() => {
    if (!queued) { refresh(); return; }
    const interval = window.setInterval(refresh, 1000);
    return () => window.clearInterval(interval);
  }, [queued, refresh]);

  function setFilter(next: { lesson?: string; status?: string; page?: number }) {
    const target = new URLSearchParams({ status: next.status ?? status, page: String(next.page ?? 1) });
    const identity = next.lesson ?? course;
    if (identity) target.set('lesson', identity);
    setChecked([]);
    setResults([]);
    setParams(target);
  }
  function prepare(mode: Mode, entries: Item[]) {
    void action.run(async () => {
      const batch = { action: mode, items: entries.map(({ lessonId, index, findingId }) => ({ lessonId, index, findingId })) };
      const preview = await api<Preview>('/disputes/preview', 'POST', batch);
      setConfirmation({ batch, preview });
    });
  }
  const data = read.value;
  const current = data?.items.find(item => keyOf(item) === selected) ?? data?.items[0];
  const batch = data?.items.filter(item => checked.includes(keyOf(item))) ?? [];
  return <section className="page">
    <div className="page-head"><div><h1>争议待处理</h1><p>查看课程原文、来源定位和独立复核意见。</p></div></div>
    {results.length > 0 && <ul className="notice scope-items" aria-label="处理结果" role="status">
      {results.map(item => <li key={keyOf(item)}><strong>{item.title ?? `片段 ${item.index + 1}`}</strong>
        <span>{item.error ?? '已处理'}</span></li>)}
    </ul>}
    <ErrorNote message={action.error} />
    {data ? <>
      <div className="action-row dispute-filters">
        <label className="project-field">课程
          <select value={course} onChange={event => setFilter({ lesson: event.target.value })}>
            <option value="">全部课程</option>{data.courses.map(item => <option key={item.id} value={item.id}>{item.topic} · {item.title}</option>)}
          </select>
        </label>
        <label className="project-field">处理状态
          <select value={status} onChange={event => setFilter({ status: event.target.value })}>
            <option value="pending">待处理</option><option value="adopted">已采纳</option>
            <option value="later">稍后处理</option><option value="all">全部</option>
          </select>
        </label>
      </div>
      <ErrorNote message={read.error} retry={refresh} />
      {batch.length > 0 && <div className="action-row dispute-filters" aria-label="批量处理">
        <span>已选 {batch.length} 项</span>
        {Object.entries(actions).map(([mode, name]) => <button type="button" className="button secondary" disabled={action.pending}
          key={mode} onClick={() => prepare(mode as Mode, batch)}>批量{name}</button>)}
      </div>}
      {current ? <div className="split disputes-layout">
        <section className="panel dispute-list" aria-label="争议列表">
          {data.items.map(item => {
            const identity = keyOf(item);
            return <div className="dispute-item" key={identity}>
              <input type="checkbox" aria-label={`勾选 ${item.title}`} checked={checked.includes(identity)}
                onChange={event => {
                  const add = event.currentTarget.checked;
                  setChecked(values => add ? [...values, identity] : values.filter(key => key !== identity));
                }} />
              <button type="button" className={identity === keyOf(current) ? 'selected' : undefined} aria-pressed={identity === keyOf(current)}
                onClick={() => setSelected(identity)}><strong>{item.title}</strong><small>{item.lessonTitle} · {verificationLabels[item.state]}</small></button>
            </div>;
          })}
          {(page > 1 || data.hasMore) && <div className="pager">
            <button type="button" className="text-action" disabled={page === 1} onClick={() => setFilter({ page: page - 1 })}>上一页</button>
            <span>第 {page} 页</span>
            <button type="button" className="text-action" disabled={!data.hasMore} onClick={() => setFilter({ page: page + 1 })}>下一页</button>
          </div>}
        </section>
        <DisputeDetail item={current} selectAction={mode => prepare(mode, [current])} />
      </div> : <Empty title="没有符合条件的争议" />}
    </> : <BackedStatus error={read.error} retry={refresh} />}
    {confirmation && <BatchDialog {...confirmation} close={() => setConfirmation(undefined)} completed={items => {
      setConfirmation(undefined);
      setResults(items);
      setChecked([]);
      refresh();
      onChange();
    }} />}
  </section>;
}
