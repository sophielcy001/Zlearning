export type ReminderSettings = { enabled: boolean; quiet: { start: string; end: string } | null };
export type ReminderReceipt = { id: string; version: number };
export type LessonReminder = ReminderReceipt & {
  kind: 'lesson'; title: string; projectTopic: string; lessonId: string; slotDay: string; startsAt: string;
};
export type Reminder = LessonReminder | ReminderReceipt & (
  { kind: 'daily'; reviews: number; disputes: number; disputeReceipt?: ReminderReceipt } | { kind: 'budget'; count: number }
);
export type ReminderCheck = { day: string; reminders: Reminder[]; nextCheckSeconds: number | null };
export type ReminderTrigger = 'foreground' | 'scheduled';
export type ReminderDecision = 'dismiss' | 'in_30_minutes' | 'tomorrow';
export type ReminderInput = {
  trigger: ReminderTrigger; day: string | null; shown: ReminderReceipt[]; delivery: string; notice?: string;
};
export type PushStatus = { publicKey: string; registered: boolean; endpointId: string | null };
export type PushNotice = { v: 1; delivery: string; workspace: string; lessonId: string | null; pairing?: string };
