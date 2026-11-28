import { useRef } from 'react';
import { api, type ProjectOption } from './api';
import type { ModelFailover } from './model-backup';
import { useAction } from './shared';
import type { Citation, Reviewer } from './verification';
import type { McpReference } from './repository-mcp';

export const EXAMPLE_MESSAGE = '我想系统学习 SQLite，从学习记录中筛选数据。';

export type CourseSelection = { version: number; sectionIndex: number; start: number; end: number; text: string };
export type LessonQuestion = { id: string; version: number; lessonId: string; selection: CourseSelection | null; favorite: boolean };
export type LessonAnswer = {
  questionId: string; state: 'queued' | 'running' | 'blocked' | 'answered' | 'paused';
  support: 'grounded' | 'insufficient' | 'unverified' | null;
  citations: Citation[]; recipient: Reviewer | null; error: string | null; failovers?: ModelFailover[];
};
export type ChatSkillTrace = { name: string; checksum: string; position: number; version: string | null; declaredTools: string | null };
export type ChatKnowledge = { excerpts: { id: string; path: string; name: string; number: number; label: string; excerpt: string }[] };
export type MemoryScope = 'shared' | 'private';
export type MemorySelection = { id: string; revision: number; scope: MemoryScope; text: string };
export type ChatAgentTrace = {
  id: string; name: string; skill: ChatSkillTrace | null; skillMode?: 'automatic'; knowledge?: ChatKnowledge;
  memories?: (Omit<MemorySelection, 'text'> & { checksum: string })[];
  mcp?: McpReference;
};

export type ModelChatReply = {
  agent?: ChatAgentTrace;
  questionId: string; questionText: string; hasHistory: boolean;
  state: 'queued' | 'running' | 'blocked' | 'answered';
  recipient: Reviewer; error: string | null; errorCode: string | null;
};
export type FavoriteQuestion = LessonQuestion & { conversationId: string; text: string; position: number };
export type Message = {
  id: string;
  position: number;
  role: 'user' | 'demo' | 'assistant';
  text: string;
  createdAt: string;
  courseCommand?: true;
  question?: LessonQuestion;
  answer?: LessonAnswer;
  modelReply?: ModelChatReply;
};
type FieldOrigin = 'fact' | 'inference' | 'suggestion' | 'record' | 'edited';
export const GOAL_ORIGINS: Record<FieldOrigin, string> = {
  fact: '原话提取', inference: '推断', suggestion: '建议', record: '学习记录', edited: '你的填写',
};
export type GoalField = {
  key: string;
  label: string;
  limit: number;
  value: string;
  origin: FieldOrigin;
  evidenceIds: string[];
  type: 'text' | 'date';
};
export type Evidence = {
  id: string;
  kind: 'message' | 'history' | 'project' | 'file';
  label: string;
  excerpt: string;
  target: string | null;
};
export type GoalDraft = { id: string; revision: number; fields: GoalField[]; evidence: Evidence[]; question?: string | null };
export type Attachment = { id: string; name: string };
export type ThreadSummary = {
  id: string;
  title: string;
  lessonId: string | null;
  createdAt: string;
  updatedAt: string;
};
export type CourseAction = { kind: 'known' | 'earlier' | 'goal'; lessonId: string; projectId: string };
export type Thread = ThreadSummary & {
  assistantMode: 'model' | 'local' | null;
  courseAction?: CourseAction;
  goal: GoalDraft | null;
  messages: Message[];
  attachments: Attachment[];
  projects: ProjectOption[];
  olderBefore: number | null;
};
export type ChatHistory = { items: ThreadSummary[]; total: number; page: number; lastPage: number };
type StartOptions = { sample?: boolean; attachments?: string[]; lessonId?: string };
type StartInput = StartOptions & { id: string; message: { id: string; text: string } };

export const chatUrl = (id: string) => `/chat/${id}`;
export function questionSource(question: LessonQuestion) {
  const params = new URLSearchParams({ readingVersion: String(question.version) });
  if (question.selection) {
    params.set('section', String(question.selection.sectionIndex));
    params.set('selectionStart', String(question.selection.start));
    params.set('selectionEnd', String(question.selection.end));
  }
  return `/lessons/${question.lessonId}?${params}`;
}

export function useStartConversation() {
  const attempt = useRef<{ key: string; request: StartInput } | undefined>(undefined);
  const action = useAction();
  async function start(text: string, options: StartOptions = {}) {
    const key = JSON.stringify({ text, ...options });
    if (attempt.current?.key !== key) {
      attempt.current = { key, request: { id: crypto.randomUUID(), message: { id: crypto.randomUUID(), text }, ...options } };
    }
    const request = attempt.current.request;
    const created = await action.run(() => api<Thread>('/conversations', 'POST', request));
    if (created) attempt.current = undefined;
    return created;
  }
  return { start, pending: action.pending, error: action.error };
}
