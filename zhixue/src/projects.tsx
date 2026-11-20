import { Fragment, useEffect, useState, type FormEvent } from 'react';
import { ArrowLeft, ArrowRight, BookOpen, Check, SlidersHorizontal, X } from 'lucide-react';
import { Link, useParams } from 'react-router-dom';
import { api, courseUrl, LEARNING_MARKS, TASK_NAMES, type Lesson, type Project, type ProjectPriority, type TaskStatus } from './api';
import { CourseReviewPanel } from './course-review';
import { DraftDialog, GenerationControls, LocalGenerationControls, hasGenerationWork, lessonStage, stepLabel, type Study } from './generation';
import { PRIORITIES } from './project-view';
import { RouteVersionsDialog } from './route-versions';
import { StageAssessments } from './stage-assessments';
import { BackedStatus, Empty, ErrorNote, Modal, useAction, useRead } from './shared';

function ProjectMetadata({ project, onClose, onSaved }: { project: Project; onClose: () => void; onSaved: () => void }) {
  const [tags, setTags] = useState(project.tags);
  const [priority, setPriority] = useState(project.priority);
  const [entry, setEntry] = useState('');
  const action = useAction();
  function withEntry() {
    const name = entry.trim();
    return name && tags.length < 30 && !tags.some(tag => tag.toLocaleLowerCase() === name.toLocaleLowerCase()) ? [...tags, name] : tags;
  }
  function addTag() { setTags(withEntry()); setEntry(''); }
  function save(event: FormEvent) {
    event.preventDefault();
    void action.run(async () => {
      await api(`/projects/${project.id}/metadata`, 'PUT', { tags: withEntry(), priority });
      onSaved();
    });
  }
  return <Modal title="标签与优先级" onClose={onClose} busy={action.pending}>
    <form className="project-dialog-form" onSubmit={save}>
      <label className="project-field">项目优先级<select value={priority ?? ''} disabled={action.pending} onChange={event => setPriority(event.target.value ? event.target.value as ProjectPriority : null)}>
        <option value="">未设置</option>{Object.entries(PRIORITIES).map(([key, value]) => <option key={key} value={key}>{value}</option>)}
      </select></label>
      <label className="project-field">添加标签<input value={entry} maxLength={32} disabled={action.pending || tags.length >= 30}
        onChange={event => setEntry(event.target.value)} onKeyDown={event => {
          if (event.key === 'Enter' && !event.nativeEvent.isComposing) { event.preventDefault(); addTag(); }
        }} /></label>
      <button type="button" className="button secondary new-condition" onClick={addTag} disabled={action.pending || !entry.trim() || tags.length >= 30}>添加标签</button>
      {tags.length > 0 && <div className="project-tags">{tags.map(tag => <button type="button" className="chip" key={tag}
        aria-label={`移除标签：${tag}`} disabled={action.pending} onClick={() => setTags(current => current.filter(item => item !== tag))}>{tag}<X size={14} /></button>)}</div>}
      <ErrorNote message={action.error} />
      <div className="project-dialog-actions">
        <button type="button" className="button secondary" disabled={action.pending} onClick={onClose}>取消</button>
        <button className="button primary" disabled={action.pending}>保存</button>
      </div>
    </form>
  </Modal>;
}

const LOCAL_PREPARATION_NAMES: Partial<Record<TaskStatus, string>> = {
  queued: '正在准备课程', ready: '课程已准备', restore_pending: '课程准备待恢复',
  stopped: '已停止准备', failed: '课程准备中断',
};

export function ProjectPage({ onChange, taskChange }: { onChange: () => void; taskChange?: { project: string } }) {
  const { projectId } = useParams();
  const { value, error, refresh } = useRead<Project>(`/projects/${projectId}`);
  const [selected, setSelected] = useState<string>();
  const [editing, setEditing] = useState(false);
  const [versions, setVersions] = useState(false);
  const [previewDraft, setPreviewDraft] = useState<string>();
  const action = useAction();
  const model = value?.generationMode === 'model';
  const generationPath = `/projects/${projectId}/generation`;
  const generation = useRead<Study>(model ? generationPath : null);
  const refreshGeneration = generation.refresh;
  const preparingPractice = value?.lessons.some(item => item.kind === 'practice' && !item.ready && !item.preparationError);
  const working = value?.task.status === 'queued' || hasGenerationWork(generation.value) || preparingPractice;
  useEffect(() => {
    if (!working) return;
    const timer = window.setInterval(() => { refresh(); if (model) refreshGeneration(); }, 700);
    return () => window.clearInterval(timer);
  }, [working, model, refresh, refreshGeneration]);
  useEffect(() => { if (value?.task.status === 'ready') onChange(); }, [value?.task.status, onChange]);
  useEffect(() => {
    if (taskChange?.project !== projectId) return;
    refresh();
    refreshGeneration();
  }, [projectId, taskChange, refresh, refreshGeneration]);
  if (!value) return <section className="page"><BackedStatus error={error} retry={refresh} /></section>;

  const selection = selected ?? generation.value?.focusId;
  const lesson = value.lessons.find(item => item.id === selection)
    ?? value.lessons.find(item => !item.learnedOn && item.required)
    ?? value.lessons.find(item => !item.learnedOn && item.kind !== 'practice')
    ?? value.lessons[0];
  const stage = lesson && lessonStage(generation.value, lesson.id);
  const phases: { name: string; id: string | null; groups: { name: string | null; nodes: Lesson[] }[] }[] = [];
  for (const item of value.lessons) {
    let phase = phases.at(-1);
    if (!phase || phase.name !== item.phase || phase.id !== item.stageId) {
      phase = { name: item.phase, id: item.stageId, groups: [] };
      phases.push(phase);
    }
    const group = phase.groups.at(-1);
    if (group && group.name === item.branch) group.nodes.push(item);
    else phase.groups.push({ name: item.branch, nodes: [item] });
  }
  function update() {
    refresh();
    if (model) refreshGeneration();
    onChange();
  }
  function selectLesson(item: Lesson) {
    if (!model || generation.value?.state === 'deleted' || item.kind === 'practice') {
      setSelected(item.id);
      return;
    }
    void action.run(async () => {
      await api(`${generationPath}/focus`, 'PUT', { lessonId: item.id });
      setSelected(item.id);
      update();
    });
  }
  function pause() {
    void action.run(async () => {
      await api(`/projects/${projectId}/pause`, 'PUT', { paused: value?.status !== 'paused' });
      update();
    });
  }
  function retryPractice(identity: string) {
    void action.run(async () => {
      await api(`/lessons/${identity}/practice/retry`, 'POST');
      refresh();
    });
  }
  return <section className="page">
    <Link className="back" to="/projects"><ArrowLeft size={16} />全部项目</Link>
    <div className="page-head"><div><h1>{value.topic}</h1><p>{value.outcome}</p></div>
      <div className="action-row">
        <button className="button secondary" onClick={() => setVersions(true)}>路线版本与顺序</button>
        <button className="button secondary" onClick={() => setEditing(true)}><SlidersHorizontal size={16} />编辑标签与优先级</button>
        <button className="button secondary" disabled={action.pending} onClick={pause}>{value.status === 'paused' ? '恢复项目' : '暂停项目'}</button>
      </div>
    </div>
    {(value.tags.length > 0 || value.priority) && <div className="project-tags detail-tags">
      {value.tags.map(tag => <span key={tag} className="tag">{tag}</span>)}
      {value.priority && <span className="project-priority">{PRIORITIES[value.priority]}优先级</span>}
    </div>}
    {editing && <ProjectMetadata project={value} onClose={() => setEditing(false)} onSaved={() => {
      setEditing(false); refresh(); onChange();
    }} />}
    {versions && <RouteVersionsDialog projectId={value.id} onClose={() => setVersions(false)} onChanged={update} />}
    {previewDraft && <DraftDialog project={value.id} lesson={previewDraft} close={() => setPreviewDraft(undefined)} />}
    {model ? <>
      {generation.value ? <GenerationControls project={value.id} value={generation.value} changed={update} />
        : <BackedStatus error={generation.error} retry={refreshGeneration} />}
      {value.task.status !== 'deleted' && <CourseReviewPanel key={value.id} project={value.id} onChanged={update} />}
      <ErrorNote message={action.error || error} />
    </> : <>
      <div className="notice task-line" role="status"><BookOpen size={17} />
        <strong>{LOCAL_PREPARATION_NAMES[value.task.status] ?? TASK_NAMES[value.task.status]}</strong>
        <span>{value.task.completed} / {value.task.total} 节已就绪</span>
      </div>
      <ErrorNote message={action.error || error} />
      <LocalGenerationControls project={value.id} task={value.task} changed={update} />
    </>}
    {lesson ? <div className="split route-split">
      <div className="panel route-tree"><h2>学习路线</h2>{phases.map(phase => <details key={phase.groups[0].nodes[0].id} open>
        <summary>{phase.name}</summary>
        {phase.groups.map(group => {
          const buttons = group.nodes.map(item => <button key={item.id} className={`route-item ${item.id === lesson.id ? 'selected' : ''}`}
            aria-label={item.title} aria-pressed={item.id === lesson.id} disabled={action.pending && model} onClick={() => selectLesson(item)}>
            {item.learnedOn ? <Check size={17} /> : <BookOpen size={17} />}<span>{item.title}
              {item.kind === 'practice' && <small>实战 · {item.required ? '必学' : '选做'}</small>}
              {item.learningMark && <small>{LEARNING_MARKS[item.learningMark]}</small>}
              {model && item.kind !== 'practice' && (!item.ready || lessonStage(generation.value, item.id) === 'disputed')
                && <small>{stepLabel(lessonStage(generation.value, item.id))}</small>}
            </span>
          </button>);
          return group.name ? <details className="route-branch" key={group.nodes[0].id} open>
            <summary>{group.name}</summary>{buttons}
          </details> : <Fragment key={group.nodes[0].id}>{buttons}</Fragment>;
        })}
      </details>)}</div>
      <article className="panel lesson-preview"><span className="tag">{lesson.kind === 'practice' ? '实战任务'
        : model ? '模型规划' : value.sample ? 'SQLite 样例' : '学习方法模板'}</span>
        <h2>{lesson.title}</h2><p>{lesson.outcome}</p>
        {lesson.prerequisites.length > 0 && <p className="model-help">建议先学 {lesson.prerequisites.map(id => value.lessons.find(item => item.id === id)?.title).filter(Boolean).join('、')}</p>}
        <div className="action-row">
          {lesson.ready && <Link className="button primary" to={courseUrl(lesson.id)}>
            {lesson.kind === 'practice' ? '打开实战' : lesson.learnedOn ? '重读课程' : '开始学习'}<ArrowRight size={17} />
          </Link>}
          {stage === 'disputed' && <Link className="button secondary" to={`/disputes?lesson=${lesson.id}`}>查看课程争议</Link>}
          {!lesson.ready && lesson.kind === 'practice' && (lesson.preparationError
            ? <ErrorNote message={lesson.preparationError} retry={() => retryPractice(lesson.id)} />
            : <Empty title="正在本机准备实战文件" />)}
          {!lesson.ready && lesson.kind !== 'practice' && stage !== 'disputed' && (
            stage && ['awaiting_review', 'review_pending', 'reviewing', 'review_failed'].includes(stage)
              ? <button type="button" className="button secondary" onClick={() => setPreviewDraft(lesson.id)}>查看待核验草稿</button>
              : <Empty title={model ? stepLabel(stage) : '课程准备完成后即可阅读'} />
          )}
        </div>
      </article>
    </div> : <Empty title="学习路线待创建" />}
    <StageAssessments key={value.id + ':' + value.routeVersion} projectId={value.id} />
  </section>;
}
