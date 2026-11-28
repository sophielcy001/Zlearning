import { lazy, StrictMode, Suspense, useEffect, useRef, useState, type ReactNode } from 'react';
import { BookOpen, BookOpenText, CalendarDays, ChartNoAxesCombined, Flag, GraduationCap, Layers3, ListTodo, MessageCircle, PanelLeft, Smartphone, Timer as TimerIcon } from 'lucide-react';
import { createRoot } from 'react-dom/client';
import { HashRouter, Link, Navigate, NavLink, Route, Routes, useLocation, useMatch } from 'react-router-dom';
import { projectUrl, timerUrl, type DemoState, type WorkspaceSetup } from './api';
import { Home } from './home-chat';
import { LessonPage, ReviewPage, ReviewQueue } from './learning';
import { LibraryPage } from './library';
import { ProjectBoard } from './project-board';
import { BackedStatus, Empty, ErrorNote, Loading, useRead } from './shared';
import { LocalReminders } from './reminders';
import { TimerValue } from './study-timer';
import './style.css';

const FirstUse = lazy(() => import('./first-use').then(module => ({ default: module.FirstUse })));
const StudyReport = lazy(() => import('./study-report').then(module => ({ default: module.StudyReport })));
const DailyPlan = lazy(() => import('./daily-plan').then(module => ({ default: module.DailyPlan })));
const ChatPage = lazy(() => import('./chat').then(module => ({ default: module.ChatPage })));
const ProjectPage = lazy(() => import('./projects').then(module => ({ default: module.ProjectPage })));
const Management = lazy(() => import('./management').then(module => ({ default: module.Management })));
const DisputesPage = lazy(() => import('./disputes').then(module => ({ default: module.DisputesPage })));
const ModelReviewPage = lazy(() => import('./model-review').then(module => ({ default: module.ModelReviewPage })));
const TaskDrawer = lazy(() => import('./task-drawer'));

function NavItem({ to, label, children, active }: { to: string; label: string; children: ReactNode; active?: boolean }) {
  return <NavLink aria-label={label} title={label} to={to} end={to === '/'} className={({ isActive }) => `nav-item ${isActive || active ? 'active' : ''}`}>{children}</NavLink>;
}

function Application({ libraryPath }: { libraryPath?: string }) {
  const { value: state, error, refresh } = useRead<DemoState>('/state');
  const [collapsed, setCollapsed] = useState(false);
  const [tasks, setTasks] = useState(false);
  const [taskChange, setTaskChange] = useState<{ project: string }>();
  const taskButton = useRef<HTMLButtonElement>(null);
  const location = useLocation();
  const viewingBackups = useMatch('/settings/backups') !== null;
  const learning = !['/chat', '/library', '/settings'].some(path => location.pathname.startsWith(path));
  const queued = Boolean(state?.queuedCount);
  const working = queued || Boolean(state?.assessmentQueued);
  const viewingProjects = location.pathname === '/projects';
  const deviceClock = Boolean(state?.phonePaired || state?.timer.active?.device === 'phone');

  useEffect(() => {
    if (!working && !viewingProjects && !deviceClock) return;
    const timer = window.setInterval(() => {
      if (working || document.visibilityState === 'visible') refresh();
    }, working ? 1000 : deviceClock ? 2500 : 5000);
    return () => window.clearInterval(timer);
  }, [working, viewingProjects, deviceClock, refresh]);
  useEffect(() => {
    window.addEventListener('focus', refresh);
    return () => window.removeEventListener('focus', refresh);
  }, [refresh]);
  useEffect(() => { window.scrollTo(0, 0); }, [location.pathname]);
  function closeTasks() {
    setTasks(false);
    taskButton.current?.focus();
  }

  return <div className={`shell ${collapsed ? 'collapsed' : ''}`}>
    <aside className="sidebar">
      <div className="brand-row"><Link to="/" className="brand" aria-label="智学首页"><img src="/zhixue.png" alt="" /><strong>智学</strong></Link>
        <button className="icon-button collapse" aria-label={collapsed ? '展开导航' : '收起导航'} onClick={() => setCollapsed(!collapsed)}><PanelLeft size={18} /></button>
      </div>
      <nav aria-label="主要导航">
        <NavItem to="/" label="学习" active={learning}><GraduationCap size={21} /><span>学习</span></NavItem>
        <div className="subnav">
          <NavLink to="/projects"><BookOpen size={15} />全部项目</NavLink>
          <NavLink to="/study-plan"><ListTodo size={15} />今日任务</NavLink>
          <NavLink to="/reviews"><CalendarDays size={15} />今日复习</NavLink>
          <NavLink to="/study-report"><ChartNoAxesCombined size={15} />学习报告</NavLink>
          <NavLink to="/disputes"><Flag size={15} />争议待处理{Boolean(state?.disputeCount) && <span className="count">{state?.disputeCount}</span>}</NavLink>
        </div>
        <NavItem to="/chat" label="聊天"><MessageCircle size={20} /><span>聊天</span></NavItem>
        <NavItem to="/library" label="知识库"><BookOpenText size={20} /><span>知识库</span></NavItem>
        <a className="nav-item" href="/mobile/" title="手机离线"><Smartphone size={20} /><span>手机离线</span></a>
        <NavItem to="/settings" label="管理中心"><Layers3 size={20} /><span>管理中心</span></NavItem>
      </nav>
      <div className="sidebar-projects"><p>学习项目</p>{state?.recentProjects.map(project => <Link key={project.id} to={projectUrl(project.id)}><BookOpen size={16} /><span>{project.topic}</span></Link>)}
        {!state?.recentProjects.length && <span className="sidebar-empty">你的项目会显示在这里</span>}
      </div>
      <button ref={taskButton} type="button" className="task-button" aria-label="后台任务" aria-expanded={tasks}
        aria-controls={tasks ? 'background-tasks' : undefined} onClick={() => { if (!tasks) refresh(); setTasks(!tasks); }}>
        <ListTodo size={18} /><span>后台任务</span>{!!state?.taskCount && <b>{state.taskCount}</b>}
      </button>
    </aside>
    <main className="main-content">
      {state && <LocalReminders paired={Boolean(state.phonePaired)} />}
      {state && error && <div className="global-error"><ErrorNote message={error} retry={refresh} /></div>}
      {!state && !viewingBackups ? <div className="page"><BackedStatus error={error} retry={refresh} /></div> :
        <Suspense fallback={<div className="page"><Loading /></div>}>
      {viewingBackups ? <Management onChange={refresh} area="backups" /> : state && <Routes>
        <Route index element={<Home state={state} />} />
        <Route path="chat/:conversationId?" element={<Suspense fallback={<div className="page"><Loading text="正在打开聊天" /></div>}>
          <ChatPage onProjectsChange={refresh} />
        </Suspense>} />
        <Route path="projects" element={<ProjectBoard boardStamp={state.boardStamp} onRefresh={refresh} />} />
        <Route path="projects/:projectId" element={<ProjectPage onChange={refresh} taskChange={taskChange} />} />
        <Route path="lessons/:lessonId" element={<LessonPage active={state.timer.active} onChange={refresh} />} />
        <Route path="study-plan" element={<DailyPlan working={working} />} />
        <Route path="study-report" element={<StudyReport working={working} />} />
        <Route path="reviews" element={<ReviewQueue onChange={refresh} />} />
        <Route path="reviews/model/:lessonId/:key" element={<ModelReviewPage timer={state.timer.active} onChange={refresh} />} />
        <Route path="reviews/:lessonId/:key" element={<ReviewPage active={state.timer.active} onChange={refresh} />} />
        <Route path="disputes" element={<DisputesPage queued={queued} onChange={refresh} />} />
        <Route path="library" element={<LibraryPage />} />
        <Route path="settings" element={<Management state={state} onChange={refresh} libraryPath={libraryPath} />} />
        <Route path="settings/models" element={<Management state={state} onChange={refresh} area="models" />} />
        <Route path="settings/budget" element={<Management state={state} onChange={refresh} area="budget" />} />
        <Route path="settings/skills" element={<Management onChange={refresh} area="skills" />} />
        <Route path="settings/agents" element={<Management onChange={refresh} area="agents" />} />
        <Route path="settings/memory" element={<Management onChange={refresh} area="memory" />} />
        <Route path="*" element={<div className="page"><Empty title="找不到这个页面"><Link to="/">返回学习首页</Link></Empty></div>} />
      </Routes>}
      </Suspense>}
    </main>
    {state?.timer.active && <Link to={timerUrl(state.timer.active)} className="floating-timer" aria-label={state.timer.active.reviewKey ? "返回计时复习" : "返回计时课程"}><TimerIcon size={17} />{state.timer.active.device === 'phone' && <span>手机</span>}{state.timer.active.status === 'paused' && <span>已暂停</span>}<TimerValue timer={state.timer.active} /></Link>}
    {!state?.timer.active && !!state?.timer.stashed.length && location.pathname !== '/settings' && <Link to="/settings" className="floating-timer"><TimerIcon size={17} />有计时记录待核对</Link>}
    {tasks && <Suspense fallback={<aside id="background-tasks" className="panel task-drawer" aria-label="后台任务列表"><Loading text="正在打开任务列表" /></aside>}>
      <TaskDrawer taskCount={state?.taskCount ?? 0} queuedCount={state?.queuedCount ?? 0} onClose={closeTasks}
        onChanged={project => { setTaskChange({ project }); refresh(); }} />
    </Suspense>}
  </div>;
}

function Startup() {
  const setup = useRead<WorkspaceSetup>('/setup');
  const openingGuide = useMatch('/setup/*');
  if (!setup.value) return <main className="first-use"><BackedStatus error={setup.error} retry={setup.refresh} /></main>;
  if (setup.value.stage !== 'complete') return <Suspense fallback={<main className="first-use"><Loading /></main>}>
    <FirstUse setup={setup.value} refresh={setup.refresh} />
  </Suspense>;
  if (openingGuide) return <Navigate to="/" replace />;
  return <Application libraryPath={setup.value.library} />;
}

createRoot(document.getElementById('root')!).render(<StrictMode><HashRouter><Startup /></HashRouter></StrictMode>);
