import type { ProjectPriority, ProjectStatus, ProjectSummary } from './api';

export const STATUS_LABELS: Record<ProjectStatus, string> = { active: '进行中', paused: '已暂停', done: '已学完' };
export const STATUSES: ProjectStatus[] = ['active', 'paused', 'done'];
export const PRIORITIES: Record<ProjectPriority, string> = { high: '高', medium: '中', low: '低' };
export const SORT_LABELS = {
  learned: '最近学习', updated: '最近更新', created: '创建时间', name: '名称',
  progress: '已学进度', due: '待复习数量', priority: '项目优先级',
};

export type Condition =
  | { field: 'status'; op: 'in' | 'not_in'; values: ProjectStatus[] }
  | { field: 'tag'; op: 'in' | 'not_in'; values: string[] }
  | { field: 'tag' | 'progress'; op: 'none' }
  | { field: 'review'; op: 'due' | 'not_due' }
  | { field: 'priority'; op: 'is'; value: ProjectPriority | null }
  | { field: 'progress'; op: 'lt' | 'gte'; value: number }
  | { field: 'progress'; op: 'between'; low: number; high: number }
  | { field: 'learned' | 'updated'; op: 'recent'; days: number }
  | { field: 'learned' | 'updated'; op: 'between'; fromDate: string; toDate: string }
  | { field: 'learned'; op: 'never' };

export type SortOrder = { key: keyof typeof SORT_LABELS; direction: 'asc' | 'desc' };
export type ColumnSetting = { key: ProjectStatus; visible: boolean };
export type ViewDefinition = {
  layout: 'board' | 'table' | 'cards'; search: string; combinator: 'and' | 'or'; filters: Condition[];
  sort: SortOrder; columns: ColumnSetting[]; boardSize: 3 | 5 | 10; pageSize: 12 | 24 | 48;
};
export type SavedView = { id: string; name: string; definition: ViewDefinition };
export type PageKey = ProjectStatus | 'all';
export type PageNumbers = Partial<Record<PageKey, number>>;
export type ProjectPageResult = {
  page: number; lastPage: number; total: number; start: number; end: number; items: ProjectSummary[];
};
export type BoardResult = {
  total: number; hasProjects: boolean; counts: Record<ProjectStatus, number>;
  pages: Partial<Record<PageKey, ProjectPageResult>>; stamp: string;
};

export const DEFAULT_VIEW: ViewDefinition = {
  layout: 'board', search: '', combinator: 'and', filters: [], sort: { key: 'learned', direction: 'desc' },
  columns: STATUSES.map(key => ({ key, visible: true })), boardSize: 3, pageSize: 12,
};
