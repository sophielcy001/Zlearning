import type { LibraryItem, ProjectOption, Review } from './api';

export type Point = { x: number; y: number };
export type Camera = { center: Point; zoom: number };
export type GraphKind = LibraryItem['kind'];
export type GraphNode = {
  id: string; kind: GraphKind; title: string; path: string | null; folder: string | null;
  projectIds: string[]; projectTopic: string | null; excerpt: string | null; fileStatus: LibraryItem['status'] | null;
  lessonId: string | null; learned: boolean; ready: boolean; weak: boolean;
  review: (Pick<Review, 'kind' | 'lessonId' | 'key' | 'title'>) | null;
  position: Point; hidden: boolean;
};
export type GraphEdge = { id: string; source: string; target: string; both: boolean; manual: boolean; automatic: boolean };
export type GraphGroup = { id: string; name: string; members: string[] };
export type GraphView = {
  center: Point | null; zoom: number; folder: string; project: string | null; due: boolean;
  local: boolean; focus: string | null; motion: boolean | null; collapsed: string[];
};
export type GraphFavorite = { id: string; name: string; view: GraphView };
export type GraphData = {
  revision: number; head: number | null; canUndo: boolean; canRedo: boolean; nodes: GraphNode[]; edges: GraphEdge[];
  projects: ProjectOption[]; folders: { path: string; name: string }[];
  groups: GraphGroup[]; favorites: GraphFavorite[]; view: GraphView;
};
type Candidate = { id: string; requestId: string; titles: Record<string, string>; reason: string };
export type GraphLinkCandidate = Candidate & { kind: 'link'; source: string; target: string; both: boolean };
export type GraphCandidate = GraphLinkCandidate | (Candidate & { kind: 'group'; name: string; members: string[] });
export type GraphRecommendationQueue = {
  jobs: { id: string; state: 'queued' | 'running' | 'blocked' | 'ready' | 'failed' | 'cancelled';
    recipient: { model: string }; count: number; error: string | null; budgetBlocked: boolean;
    usage: { inputTokens?: number; outputTokens?: number } | null }[];
  candidates: GraphCandidate[];
};
export type LinkDirection = 'both' | 'one-way' | 'remove' | 'default';
export type GraphEdit =
  | { kind: 'move'; id: string; position: Point }
  | { kind: 'moveGroup'; id: string; delta: Point }
  | { kind: 'visibility'; ids: string[]; hidden: boolean }
  | { kind: 'link'; source: string; target: string; direction: LinkDirection }
  | { kind: 'group'; group: GraphGroup }
  | { kind: 'dropGroup' | 'dropFavorite'; id: string }
  | { kind: 'favorite'; favorite: GraphFavorite }
  | { kind: 'undo' | 'redo' }
  | { kind: 'restore'; id: number };

export const KIND_LABELS: Record<GraphKind, string> = {
  course: '知识点', note: '笔记', import: '资料', supplement: '补充讲解',
};

export function studyLabels(node: GraphNode): string[] {
  if (node.kind !== 'course') return [];
  return [
    node.learned ? '已学完' : node.ready ? '待学习' : '准备中',
    ...(node.review ? ['待复习'] : []),
    ...(node.weak ? ['薄弱'] : []),
  ];
}

export function matchesView(node: GraphNode, view: GraphView) {
  const folder = !view.folder || node.folder === view.folder || node.folder?.startsWith(view.folder + '/');
  return !node.hidden && folder && (!view.project || node.projectIds.includes(view.project)) && (!view.due || node.review !== null);
}

export function nodeMatches(node: GraphNode, query: string) {
  return [node.title, node.path, node.projectTopic].some(value => value?.normalize('NFC').toLocaleLowerCase().includes(query));
}

export function normalizeSearch(value: string) {
  return value.trim().normalize('NFC').toLocaleLowerCase();
}
