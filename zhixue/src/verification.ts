export type Reviewer = { id: string; name: string; model: string; address: string };
export type SourceLocator = { fileId: string; at: number; revision: string; offset: number };
export type SourceExcerpt = SourceLocator & { id: string; title: string; label: string; excerpt: string };
export type Citation = Pick<SourceLocator, 'fileId' | 'at' | 'revision'> & {
  title: string; label: string; quote: string;
};
export type VerificationState = 'verified' | 'disputed' | 'insufficient' | 'dependency_blocked';
export type Disposition = 'pending' | 'later' | 'adopted';
export type VerifiedSection = {
  index: number; title: string; text: string; checks: string[];
  state: VerificationState; disposition: Disposition; reason: string; detail: string | null;
  dependsOn: number[]; citations: Citation[];
  consolidation?: { id: string; questions: string[]; reviewer: Reviewer; conversations?: string[] };
};
export type Verification = {
  state: string; error: string | null; findingId: string | null; reviewer: Reviewer | null;
  sections: VerifiedSection[]; published: VerifiedSection[]; publishedVersion: number | null; sourcesChanged: boolean;
};

export const verificationLabels: Record<VerificationState, string> = {
  verified: '独立复核通过',
  disputed: '存在争议',
  insufficient: '依据不足',
  dependency_blocked: '前置结论未核验',
};

export function citationUrl(reference: Citation) {
  return `/library?${new URLSearchParams({ file: reference.fileId, at: String(reference.at), revision: reference.revision })}`;
}
