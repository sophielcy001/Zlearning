import { useState } from 'react';
import { api, apiResponse } from './api';
import { Link } from 'react-router-dom';
import { Empty, ErrorNote, Loading, Modal, useAction, useRead } from './shared';
import { RepositoryConnections } from './repository-mcp';

type SkillSummary = {
  name: string;
  description: string;
  version: string | null;
  license: string | null;
  compatibility: string | null;
  declaredTools: string | null;
  checksum: string;
};

type SkillDocument = SkillSummary & { source: string };
type CapabilityKind = 'skill' | 'tool' | 'mcp';
type CapabilityStatus = {
  status: 'installed' | 'enabled' | 'unavailable';
  canUpdate: boolean;
};
type CapabilityEntry = CapabilityStatus & {
  id: string;
  name: string;
  description: string;
  source: string;
  version: string | null;
  permission: string;
  dependencies: string | null;
  agents: { id: string; name: string; available?: boolean; folders?: string[]; repositories?: string[] }[];
};
type CatalogSkill = SkillSummary & CapabilityEntry & { position: number | null };

function Availability({ status, canUpdate }: CapabilityStatus) {
  return <>
    <span>已安装</span>
    {status === 'enabled' && <> · <span>已启用</span></>}
    {status === 'unavailable' && <> · <span>不可用</span></>}
    {canUpdate && <> · <span>可更新</span></>}
  </>;
}

const capabilityPath = (kind: CapabilityKind) => '/capabilities?kind=' + kind;

function ToolCatalog({ kind }: { kind: 'tool' | 'mcp' }) {
  const path = capabilityPath(kind);
  const list = useRead<{ items: CapabilityEntry[] }>(path);
  const action = useAction();
  const [detail, setDetail] = useState<CapabilityEntry>();

  async function inspect(id: string) {
    const entry = await action.run(async () => {
      const response = await api<{ items: CapabilityEntry[] }>(path);
      return response.items.find(item => item.id === id);
    });
    if (entry) setDetail(entry);
    list.refresh();
  }

  return <>
    {kind === 'mcp' && <RepositoryConnections changed={list.refresh} />}
    <ErrorNote message={list.error} retry={list.refresh} />
    <ErrorNote message={action.error} />
    {!list.value && !list.error && <Loading />}
    {list.value?.items.length === 0 && <Empty title={kind === 'mcp' ? '尚无已配置的 MCP 连接' : '没有可用工具'} />}
    {!!list.value?.items.length && <ul className="skill-list">
      {list.value.items.map(entry => <li key={entry.id}>
        <div><strong>{entry.name}</strong><p>{entry.description}</p>
          <p className="model-help"><Availability {...entry} /></p></div>
        <button type="button" className="button soft" aria-label={'查看 ' + entry.name} disabled={action.pending}
          onClick={() => void inspect(entry.id)}>查看</button>
      </li>)}
    </ul>}
    {detail && <Modal title={detail.name} onClose={() => setDetail(undefined)}>
      <div className="project-dialog-form">
        <dl className="skill-fields">
          <div><dt>来源</dt><dd>{detail.source}</dd></div>
          <div><dt>版本</dt><dd>{detail.version ?? '未声明'}</dd></div>
          <div><dt>所需权限</dt><dd>{detail.permission}</dd></div>
          <div><dt>依赖</dt><dd>{detail.dependencies ?? '未声明'}</dd></div>
          <div><dt>状态</dt><dd><Availability {...detail} /></dd></div>
        </dl>
        {detail.agents.length ? <section>
          <h3>Agent 授权</h3>
          <ul className="skill-list" aria-label="Agent 授权">
            {detail.agents.map(agent => <li key={agent.id}><div>
              <strong>{agent.name}</strong><p>{agent.available === false ? (kind === 'mcp' ? '连接不可用' : '目录不可用') : '可用'}</p>
              {agent.folders?.map(folder => <p key={folder}>{folder}</p>)}
              {agent.repositories?.map(repository => <p key={repository}>{repository}</p>)}
            </div></li>)}
          </ul>
        </section> : <p>尚无 Agent 获得授权</p>}
        <Link className="text-action" to="/settings/agents">管理 Agent 授权</Link>
      </div>
    </Modal>}
  </>;
}

function SkillDetails({ skill }: { skill: SkillDocument }) {
  return <>
    <dl className="skill-fields">
      <div><dt>名称</dt><dd>{skill.name}</dd></div>
      <div><dt>来源</dt><dd>本地 SKILL.md</dd></div>
      <div><dt>描述</dt><dd>{skill.description}</dd></div>
      <div><dt>版本</dt><dd>{skill.version ?? '未声明'}</dd></div>
      <div><dt>许可证</dt><dd>{skill.license ?? '未声明'}</dd></div>
      <div><dt>环境要求</dt><dd>{skill.compatibility ?? '未声明'}</dd></div>
      <div><dt>声明的工具</dt><dd>{skill.declaredTools ?? '未声明'}</dd></div>
    </dl>
    <details>
      <summary>查看 SKILL.md 原文</summary>
      <pre className="skill-source">{skill.source}</pre>
    </details>
  </>;
}

const skillFields = {
  declaredTools: '声明的工具', compatibility: '环境要求', license: '许可证', version: '版本', description: '描述',
};
type Comparison = {
  current: SkillDocument;
  changes: { field: keyof typeof skillFields; before: string | null; after: string | null }[];
  diff: string | null;
  confirmation: string;
};

function SkillComparison({ change, target }: { change: Comparison; target: SkillDocument }) {
  return <>
    {change.changes.length > 0
      ? <table className="backup-counts skill-compare" aria-label="元数据变更">
        <thead><tr><th scope="col">字段</th><th scope="col">当前</th><th scope="col">目标</th></tr></thead>
        <tbody>{change.changes.map(row => <tr key={row.field}>
          <th scope="row">{skillFields[row.field]}</th><td>{row.before ?? '未声明'}</td><td>{row.after ?? '未声明'}</td>
        </tr>)}</tbody>
      </table>
      : <p>工具声明和其他元数据保持原值。</p>}
    <details>
      <summary>查看原文差异</summary>
      {change.diff === null
        ? <><h3>当前版本</h3><pre className="skill-source">{change.current.source}</pre>
          <h3>目标版本</h3><pre className="skill-source">{target.source}</pre></>
        : change.diff ? <pre className="skill-source">{change.diff}</pre>
          : <p>原文的行分隔符或文件结束符有变化。</p>}
    </details>
  </>;
}

type VersionSummary = SkillSummary & { position: number };
type VersionDetail = { document: SkillDocument; position: number; comparison: Comparison | null };

function SkillHistory({ entry, onClose, onChange }: { entry: CatalogSkill; onClose: () => void; onChange: () => void }) {
  const path = '/skills/' + encodeURIComponent(entry.name);
  const active = useRead<SkillDocument>(path);
  const history = useRead<{ currentChecksum: string; items: VersionSummary[] }>(path + '/versions');
  const [selected, setSelected] = useState<string>();
  const version = useRead<VersionDetail>(selected ? path + '/versions/' + selected : null);
  const action = useAction();
  const historical = version.value;

  function activate() {
    if (!historical?.comparison) return;
    void action.run(async () => {
      await api<SkillDocument>(path + '/rollback', 'POST', {
        checksum: historical.document.checksum, confirmation: historical.comparison?.confirmation,
      });
      setSelected(undefined);
      active.refresh();
      history.refresh();
      onChange();
    });
  }

  return <Modal title="Skill 详情" busy={action.pending} onClose={onClose}>
    <div className="project-dialog-form">
      <ErrorNote message={history.error} retry={history.refresh} />
      {selected ? <>
        <ErrorNote message={version.error} retry={version.refresh} />
        {!historical && !version.error && <Loading />}
        {historical && <>
          <h3>第 {historical.position} 版</h3>
          {historical.comparison && <SkillComparison change={historical.comparison} target={historical.document} />}
          <SkillDetails skill={historical.document} />
        </>}
        <ErrorNote message={action.error} retry={version.refresh} />
        <div className="project-dialog-actions">
          <button type="button" className="button soft" disabled={action.pending} onClick={() => setSelected(undefined)}>返回历史</button>
          {historical?.comparison && <button type="button" className="button primary"
            disabled={action.pending} onClick={activate}>确认切换到此版本</button>}
        </div>
      </> : <>
        <ErrorNote message={active.error} retry={active.refresh} />
        {!active.value && !active.error && <Loading />}
        {active.value && <SkillDetails skill={active.value} />}
        <dl className="skill-fields">
          <div><dt>应用权限</dt><dd>{entry.permission}</dd></div>
          <div><dt>状态</dt><dd><Availability {...entry} /></dd></div>
          <div><dt>已授权的 Agent</dt><dd>{entry.agents.map(agent => agent.name).join('、') || '无'}</dd></div>
        </dl>
        {history.value && <section aria-label="版本历史">
          <h3>版本历史</h3>
          <ul className="skill-list">
            {history.value.items.map(item => <li key={item.checksum}>
              <div>
                <strong>{item.checksum === history.value?.currentChecksum ? '当前版本 · ' : ''}第 {item.position} 版</strong>
                {item.version && <p>声明版本 {item.version}</p>}
              </div>
              {item.checksum !== history.value?.currentChecksum && <button type="button" className="button soft"
                onClick={() => setSelected(item.checksum)}>查看第 {item.position} 版</button>}
            </li>)}
          </ul>
        </section>}
      </>}
    </div>
  </Modal>;
}

function SkillLibrary() {
  const list = useRead<{ items: CatalogSkill[] }>(capabilityPath('skill'));
  const action = useAction();
  const [preview, setPreview] = useState<{ file: File; content: SkillDocument & { update?: Comparison } }>();
  const [selected, setSelected] = useState<string>();
  const [notice, setNotice] = useState('');
  const update = preview?.content.update;
  const entry = list.value?.items.find(item => item.name === selected);

  function review(file: File) {
    setNotice('');
    void action.run(async () => {
      if (file.name.toLowerCase() !== 'skill.md') throw new Error('请选择名为 SKILL.md 的文件。');
      const response = await apiResponse('/skills/preview', 'POST', file, 'text/markdown');
      setPreview({ file, content: await response.json() });
    });
  }

  function install() {
    if (!preview) return;
    void action.run(async () => {
      const headers: Record<string, string> = { 'X-Skill-Checksum': preview.content.checksum };
      if (update) headers['X-Skill-Update-Confirmation'] = update.confirmation;
      const response = await apiResponse('/skills', 'POST', preview.file, 'text/markdown', undefined, undefined, headers);
      const skill: SkillDocument = await response.json();
      setPreview(undefined);
      setNotice((update ? '已更新 ' : '已导入 ') + skill.name);
      list.refresh();
    });
  }

  return <>
    <div className="skill-library-heading">
      <h3>Skills</h3>
      <label className="project-field">
        <span>选择 SKILL.md</span>
        <input type="file" accept=".md,text/markdown" disabled={action.pending} onChange={event => {
          const file = event.currentTarget.files?.[0];
          event.currentTarget.value = '';
          if (file) review(file);
        }} />
      </label>
    </div>
    {!preview && <ErrorNote message={action.error} />}
    {!!notice && <p role="status">{notice}</p>}
    <ErrorNote message={list.error} retry={list.refresh} />
    {!list.value && !list.error && <Loading />}
    {list.value?.items.length === 0 && <Empty title="还没有已导入的 Skill" />}
    {!!list.value?.items.length && <ul className="skill-list">
      {list.value.items.map(skill => <li key={skill.name}>
        <div><strong>{skill.name}</strong><p>{skill.description}</p>{skill.version && <p>版本 {skill.version}</p>}
          <p className="model-help"><Availability {...skill} /></p></div>
        <button type="button" className="button soft" aria-label={'查看 ' + skill.name} onClick={() => {
          setSelected(skill.name);
          list.refresh();
        }}>查看</button>
      </li>)}
    </ul>}
    {preview && <Modal title={update ? '确认更新 Skill' : '确认导入 Skill'} busy={action.pending}
      onClose={() => setPreview(undefined)}>
      <div className="project-dialog-form">
        {update && <><p role="status">有可更新版本</p><SkillComparison change={update} target={preview.content} /></>}
        <SkillDetails skill={preview.content} />
        <p>当前知识库以纯文本保存 Skill。声明的工具供查看。</p>
        <ErrorNote message={action.error} retry={action.pending ? undefined : () => review(preview.file)} />
        <div className="project-dialog-actions">
          <button type="button" className="button soft" disabled={action.pending} onClick={() => setPreview(undefined)}>取消</button>
          <button type="button" className="button primary" disabled={action.pending} onClick={install}>
            {update ? '确认更新' : '确认导入'}
          </button>
        </div>
      </div>
    </Modal>}
    {entry && <SkillHistory entry={entry} onClose={() => setSelected(undefined)} onChange={list.refresh} />}
  </>;
}

const categories = [
  { id: 'tool', label: '工具' },
  { id: 'mcp', label: 'MCP' },
  { id: 'skill', label: 'Skills' },
] as const;

export function CapabilityCatalog() {
  const [kind, setKind] = useState<CapabilityKind>('skill');
  return <section className="panel settings skill-library">
    <h2>能力库</h2>
    <div className="segmented" role="group" aria-label="能力分类">
      {categories.map(category => <button type="button" key={category.id} aria-pressed={kind === category.id}
        onClick={() => setKind(category.id)}>{category.label}</button>)}
    </div>
    {kind === 'skill' ? <SkillLibrary /> : <ToolCatalog key={kind} kind={kind} />}
  </section>;
}

