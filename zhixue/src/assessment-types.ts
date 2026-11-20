import type { Review, ReviewResult } from './api';
import type { ModelBackups, ModelFailover } from './model-backup';
import type { Citation } from './verification';

export type JobState = 'queued' | 'running' | 'blocked';
export type AssessmentJob = { kind: string; state: JobState; roundId: string | null; error: string | null; failovers?: ModelFailover[] };
export type AssessmentNoteLocator = { noteId: string; revision: string; start: number; end: number };
export type AssessmentNoteExcerpt = AssessmentNoteLocator & {
  title: string; text: string; line?: number; route?: string; verified?: boolean; issue?: string | null; key?: string;
};
export type AssessmentSettings = {
  granted: boolean; authorized: boolean; reviewAuthorized: boolean; remediation: boolean; maxCalls: number | null; calls: number; jobs: AssessmentJob[]; backups?: ModelBackups;
  noteState?: 'ready' | 'blocked' | 'waiting'; noteExcerpts?: AssessmentNoteExcerpt[];
};
export type AssessmentReport = ReviewResult & {
  scoredOn: string;
  gradingDelayed: boolean;
  items: { index: number; met: boolean; reason: string; required: boolean }[];
};
export type AssessmentGroup = {
  id: string; round: number; mode: 'formal' | 'practice'; state: 'preparing' | 'ready' | 'grading' | 'graded' | 'rechecking' | 'superseded';
  courseVersion: number; questions: { text: string }[]; hint: string | null; draft: string[]; answers: string[];
  result: AssessmentReport | null;
  history: (AssessmentReport & { reviewer: { name: string; model: string } | null; evaluatedAt: string })[];
  jobState: JobState | null; error: string | null; remediation: RemediationStatus | null;
  noteReferences?: { noteId: string; title: string; text: string; line: number; route: string }[];
};
export type RemediationStatus = {
  id: string; state: 'authorization_required' | 'drafting' | 'verifying' | 'publishing' | 'published' | 'blocked'
    | 'disputed' | 'superseded' | 'paused';
  error: string | null; fileId: string | null;
};
export type RemediationCard = {
  id: string; fileId: string; revision: string; assessmentId: string; objectiveKey: string; sectionIndex: number; sectionTitle: string;
  courseVersion: number; round: number; scoredOn: string; sourcesChanged: boolean; citations: Citation[];
  entries: { index: number; question: string; cause: string; explanation: string; reason: string }[];
};
export type AssessmentDetail = Review & { round: number; group: AssessmentGroup | null; latest: AssessmentGroup | null };
export const abilityName = { recall: '回忆与理解', apply: '应用', analyze: '分析' };
