const sessionKey = 'zhixue-session';
const hash = new URL(window.location.href);
const queryIndex = hash.hash.indexOf('?');
if (queryIndex >= 0) {
  const query = new URLSearchParams(hash.hash.slice(queryIndex + 1));
  const session = query.get('session');
  if (session) {
    sessionStorage.setItem(sessionKey, session);
    query.delete('session');
    const rest = query.toString();
    hash.hash = hash.hash.slice(0, queryIndex) + (rest ? `?${rest}` : '');
    history.replaceState(history.state, '', hash);
  }
}

export function hasDesktopSession() {
  return window.location.hostname === '127.0.0.1' && !!sessionStorage.getItem(sessionKey);
}

export class ApiError extends Error {
  constructor(message: string, readonly status: number, readonly payload: Record<string, unknown>) {
    super(message);
  }
}

export async function apiResponse(path: string, method = 'GET', body?: BodyInit, contentType?: string, signal?: AbortSignal, keepalive?: boolean, extraHeaders?: HeadersInit): Promise<Response> {
  const headers = new Headers(extraHeaders);
  headers.set('Authorization', 'Bearer ' + (sessionStorage.getItem(sessionKey) ?? ''));
  if (contentType) headers.set('Content-Type', contentType);
  const response = await fetch(`/api${path}`, {
    method, signal, headers, body, keepalive,
  });
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) ?? {};
    const message = typeof payload.detail === 'string' ? payload.detail : response.status === 422 ? '请检查填写的内容。' : '请求暂时失败，请重试。';
    throw new ApiError(message, response.status, payload);
  }
  return response;
}

export async function api<T>(path: string, method = 'GET', data?: object, signal?: AbortSignal): Promise<T> {
  const body = data === undefined ? undefined : JSON.stringify(data);
  const response = await apiResponse(path, method, body, body === undefined ? undefined : 'application/json', signal);
  return response.json() as Promise<T>;
}

export type WorkspaceSetup = {
  stage: 'storage' | 'opening' | 'models' | 'complete';
  directory?: string;
  library?: string;
};

export type Goal = { id: string; topic: string; outcome: string; foundation: string };
export type ProjectStatus = 'active' | 'paused' | 'done';
export type ProjectPriority = 'high' | 'medium' | 'low';
export type ProjectOption = { id: string; topic: string };
export const LEARNING_MARKS = {
  known: '已会', to_learn: '待学', to_reinforce: '待巩固',
} as const;
export type LearningMark = keyof typeof LEARNING_MARKS;
export type Lesson = {
  id: string; title: string; phase: string; outcome: string; ready: boolean; learnedOn: string | null; prerequisites: string[];
  stageId: string | null; branch: string | null;
  kind: 'lesson' | 'practice'; learningMark: LearningMark | null; required: boolean; preparationError: string | null;
};
export type TaskStatus = 'queued' | 'failed' | 'ready' | 'blocked' | 'stopped' | 'awaiting_review' | 'deleted' | 'restore_pending';
export const TASK_NAMES: Record<TaskStatus, string> = {
  queued: '准备中', failed: '准备中断', ready: '已准备', blocked: '待处理',
  stopped: '已停止', awaiting_review: '等待核验', deleted: '任务已删除', restore_pending: '待恢复',
};
export type Task = { status: TaskStatus; error: string | null; completed: number; total: number };
export type Project = Goal & {
  sample: boolean;
  generationMode: 'model' | 'local';
  routeVersion: number | null;
  status: ProjectStatus;
  createdOn: string;
  updatedOn: string;
  lastLearnedOn: string | null;
  priority: ProjectPriority | null;
  tags: string[];
  learned: number;
  lessons: Lesson[];
  task: Task;
};
export type ProjectSummary = ProjectOption & {
  outcome: string; status: ProjectStatus; tags: string[]; priority: ProjectPriority | null;
  learned: number; lessonCount: number; progress: number | null; due: number;
  reviewStatus: 'none' | 'complete' | 'paused' | 'due' | 'upcoming';
  nextLesson: Pick<Lesson, 'id' | 'title' | 'phase' | 'ready'> | null;
  createdOn: string; updatedOn: string; lastLearnedOn: string | null;
};
export type TaskList = {
  total: number; page: number; lastPage: number;
  items: TaskSummary[];
};
export type TaskSummary = ProjectOption & Task & { generationMode: Project['generationMode']; paused: boolean };
export type ReadingPoint = { block: number; offset: number; excerpt: string; excerptOffset: number };
export type ReadingPosition = { revision: string; point: ReadingPoint };
export function recordReadingPosition(lesson: string, revision: string, point: ReadingPoint | null) {
  return apiResponse(`/lessons/${lesson}/reading-position`, 'PUT', JSON.stringify({ revision, point }), 'application/json', undefined, true);
}

export type LessonDetail = {
  project: Project;
  version: number;
  body: string | null;
  courseIssue: string | null;
  courseVersion: number | null;
  courseMatchesVerification: boolean;
  readingRevision: string | null;
  readingPosition: ReadingPosition | null;
  source: { title: string; url: string } | null;
  note: string;
  objectives: { key: string; title: string }[];
};
export type Review = {
  projectId: string;
  projectTopic: string;
  lessonId: string;
  key: string;
  title: string;
  dueOn: string | null;
  stage: number;
  status: 'due' | 'upcoming' | 'paused' | 'complete' | 'waiting' | 'held';
  kind?: 'local' | 'model';
  ability?: 'recall' | 'apply' | 'analyze';
  gradingDelayed?: boolean;
};
export type ReviewResult = { outcome: 'passed' | 'failed' | 'hinted'; stage: number; dueOn: string | null; explanation: string; submittedOn: string };
export type ReviewDetail = Review & {
  round: number;
  questions: { text: string; options: string[] }[];
  answers: (number | null)[];
  hint: string | null;
  latest: ReviewResult | null;
};
export type TimeCategory = 'learning' | 'review' | 'assistant' | 'notes' | 'unclassified';
export type Timer = {
  id: string; lessonId: string; title: string; status: 'running' | 'paused' | 'stashed' | 'finished';
  device?: 'desktop' | 'phone'; elapsed: number; measuredAt: string; category: TimeCategory; reviewKey: string | null; reviewKind: Review['kind'] | null;
};
export type TimerState = { active: Timer | null; stashed: Timer[]; lastSaved: Timer | null };
export type DemoState = {
  today: string; realToday: string; recentProjects: ProjectOption[]; continuation: ProjectSummary | null;
  phonePaired?: boolean; dueCount: number; taskCount: number; queuedCount: number; disputeCount: number; assessmentQueued: number; boardStamp: string; timer: TimerState;
};
export type ReviewList = { today: string; reviews: Review[]; assessmentQueued: number };
export type LibraryFormat = 'txt' | 'md' | 'pdf' | 'docx' | 'png' | 'jpg' | 'jpeg' | 'html';
export type LibraryTag = { key: string; name: string };
export type LibraryItem = {
  id: string; kind: 'course' | 'note' | 'import' | 'supplement'; name: string; format: LibraryFormat; path: string;
  projectId: string | null; projectTopic: string | null; projectIds?: string[]; revision: string | null;
  status: 'ready' | 'partial' | 'missing' | 'failed' | 'empty' | 'trashed' | 'purged'; message: string | null;
  trashId?: string;
  tags?: LibraryTag[]; favorite?: boolean; lastViewedAt?: string | null; lastViewedOrder?: number;
  pageCount: number | null; unreadablePages: number[];
};
export type LibraryLocation = { number: number; label: string; textRevision?: string };
export type WebSource = { requestedUrl: string; url: string; fetchedAt: string; matchesSnapshot: boolean };
export type LibraryFile = LibraryItem & {
  content: string; markdown: boolean; anchor: (LibraryLocation & { text: string }) | null;
  webSource?: WebSource | null; sections?: LibraryLocation[];
};
export type LibraryHit = LibraryItem & {
  location: LibraryLocation | null; snippet: string; semanticExcerpt?: string; match?: 'keyword' | 'semantic' | 'both';
};
export type SemanticIndexStatus = {
  state: 'disabled' | 'preparing' | 'indexing' | 'ready' | 'failed';
  completed: number; total: number; message: string | null; modelBytes: number;
};
export type LibraryMatches = { hits: LibraryHit[]; hasMore: boolean; semantic?: SemanticIndexStatus };
export type WikiTarget = Pick<LibraryItem, 'id' | 'name' | 'kind' | 'status' | 'path' | 'projectTopic'>;
export type WikiResolution = { state: 'resolved' | 'ambiguous' | 'missing'; choices: WikiTarget[] };
export type WikiBacklink = WikiTarget & { at: number; revision: string; label: string; snippet: string };

export async function uploadDocument(file: File, duplicate: 'ask' | 'reuse' | 'copy') {
  const query = new URLSearchParams({ name: file.name, duplicate });
  const response = await apiResponse(`/files/upload?${query}`, 'POST', file, 'application/octet-stream');
  return response.json() as Promise<{ id: string; status: 'duplicate' | 'created' | 'reused'; name?: string }>;
}

export const courseUrl = (id: string) => `/lessons/${id}`;
export const projectUrl = (id: string) => `/projects/${id}`;
export const reviewUrl = (item: Pick<Review, 'kind' | 'lessonId' | 'key'>) => `/reviews/${item.kind === 'model' ? 'model/' : ''}${item.lessonId}/${item.key}`;
export const timerUrl = (timer: Timer) => timer.reviewKey && timer.reviewKind
  ? reviewUrl({ kind: timer.reviewKind, lessonId: timer.lessonId, key: timer.reviewKey }) : courseUrl(timer.lessonId);
export const dateLabel = (day: string) => day.replaceAll('-', '.');
export const timeLabel = (seconds: number) => [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60]
  .map(unit => String(unit).padStart(2, '0')).join(':');
