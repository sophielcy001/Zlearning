import { lazy, Suspense, useEffect, useState } from 'react';
import { ArrowRight } from 'lucide-react';
import { Link, useMatch, useNavigate } from 'react-router-dom';
import { api, type WorkspaceSetup } from './api';
import { BackedStatus, ErrorNote, Loading, useAction } from './shared';

const ModelSettings = lazy(() => import('./model-settings').then(module => ({ default: module.ModelSettings })));

function StorageChoice({ directory, selected }: { directory: string; selected: () => void }) {
  const [folder, setFolder] = useState(directory);
  const action = useAction();

  async function save() {
    const entered = folder.trim();
    if (!entered) return;
    const result = await action.run(() => api<WorkspaceSetup>('/setup/storage', 'PUT', {
      directory: entered === directory ? null : entered,
    }));
    if (result) selected();
  }

  return <form className="panel settings setup-storage project-dialog-form" onSubmit={event => { event.preventDefault(); void save(); }}>
    <label className="project-field">保存文件夹
      <input required maxLength={2048} disabled={action.pending} value={folder}
        onChange={event => setFolder(event.target.value)} />
    </label>
    <p className="model-help">可以使用默认位置，或填写本机新文件夹或空文件夹的绝对路径。资料和课程保存在其中的 library 文件夹，学习记录保存在同一位置。</p>
    <ErrorNote message={action.error} />
    <div className="action-row">
      <button className="button primary" type="submit" disabled={!folder.trim() || action.pending}>
        {action.pending ? '正在检查目录' : '使用此位置'} <ArrowRight size={17} />
      </button>
    </div>
  </form>;
}

export function FirstUse({ setup, refresh }: { setup: WorkspaceSetup; refresh: () => void }) {
  const navigate = useNavigate();
  const viewingResults = useMatch('/setup/results') !== null;
  const [starting, setStarting] = useState(false);
  const [delayed, setDelayed] = useState(false);
  const action = useAction();
  const opening = setup.stage === 'opening' || (setup.stage === 'storage' && starting);
  const configuring = setup.stage === 'models';

  useEffect(() => {
    if (!opening) return;
    const interval = window.setInterval(refresh, 450);
    const threshold = window.setTimeout(() => setDelayed(true), 15000);
    return () => { window.clearInterval(interval); window.clearTimeout(threshold); };
  }, [opening, refresh]);

  async function finish() {
    const result = await action.run(() => api<WorkspaceSetup>('/setup/complete', 'POST'));
    if (result) {
      navigate('/', { replace: true });
      refresh();
    }
  }

  return <main className="first-use">
    <div className="first-use-brand"><img src="/zhixue.png" alt="" /><strong>智学</strong></div>
    <header>
      <h1>{configuring ? viewingResults ? '查看测试结果' : '配置学习模型' : '选择知识库位置'}</h1>
      {configuring && <p className="setup-location">知识库 <code>{setup.library}</code></p>}
    </header>
    {opening ? <div className="panel settings">
      <Loading text="正在打开知识库" />
      {delayed && <BackedStatus error="打开知识库所需时间较长，请确认选定的目录仍可访问。" retry={refresh} />}
    </div> : configuring ? <>
      <Suspense fallback={<Loading text="正在打开模型设置" />}>
        <ModelSettings purpose={viewingResults ? 'check' : 'setup'} />
      </Suspense>
      <ErrorNote message={action.error} />
      <div className="first-use-actions">
        {viewingResults ? <>
          <Link className="button secondary" to="/setup">返回模型设置</Link>
          <button className="button primary" type="button" disabled={action.pending} onClick={() => void finish()}>
            进入首页 <ArrowRight size={17} />
          </button>
        </> : <>
          <button className="button secondary" type="button" disabled={action.pending} onClick={() => void finish()}>稍后配置模型</button>
          <Link className="button primary" to="/setup/results">查看测试结果 <ArrowRight size={17} /></Link>
        </>}
      </div>
    </> : setup.directory && <StorageChoice directory={setup.directory} selected={() => { setStarting(true); refresh(); }} />}
  </main>;
}
