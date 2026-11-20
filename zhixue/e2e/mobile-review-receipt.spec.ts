import { writeFile } from 'node:fs/promises';
import type { AddressInfo } from 'node:net';
import { expect, test } from '@playwright/test';
import { createServer } from 'vite';

test('a grading reply arriving late cannot replace the settled review', async ({ page, context }, info) => {
  test.setTimeout(60000);
  const vite = await createServer({
    configFile: false, server: { host: '127.0.0.1', port: 0, hmr: false }, optimizeDeps: { noDiscovery: true },
  });
  try {
    await vite.listen();
    const { port } = vite.httpServer!.address() as AddressInfo;
    const peer = await context.newPage();
    const workspace = 'review-receipt-workspace';
    const room = 'approved-room';
    for (const tab of [page, peer]) {
      await tab.route('**/src/mobile-data.ts*', route => route.fulfill({
        contentType: 'application/javascript',
        body: 'export function studyKey(kind, workspace, id) { return [workspace, kind, id].join(":"); }\n' +
          'export function mobileRequest(command, room) { return window.reviewReceiptFixture(command, room); }',
      }));
      await tab.goto('http://127.0.0.1:' + port + '/zhixue.png');
    }
    const identifiers = await page.evaluate(async ({ workspace, room }) => {
      const prefix = '/src/';
      const { savePair, savePaper, saveAnswers, saveLesson, saveNote } =
        await import(prefix + 'mobile-store.ts') as typeof import('../src/mobile-store');
      const key = await crypto.subtle.importKey('raw', new Uint8Array(32), 'HKDF', false, ['deriveKey']);
      await savePair({ relay: location.origin, room, key });
      const lesson = await saveLesson({
        workspace, id: 'a-lesson', title: '复习课', projectTopic: '测试课程', body: '# 课文',
        revision: 'course-first', note: { id: 'note-a', title: '笔记', revision: 'note-base', content: '原笔记' },
      }, room);
      await saveNote(lesson!.key, '原笔记', '未同步笔记');
      const paper = await savePaper({
        workspace, id: 'paper-first', lessonId: 'a-lesson', key: 'target', round: 1, title: '正式复习',
        projectTopic: '测试课程', dueOn: '2026-11-20', kind: 'model', groupId: 'round-a', basis: 'first',
        questions: [{ text: '解释原理' }],
      }, room);
      if (paper?.kind !== 'review') throw new Error('Fixture review was not downloaded');
      const review = await saveAnswers(paper.key, paper.draft, ['离线答案'], 'submit');
      if (review?.kind !== 'review' || !review.submission) throw new Error('Fixture review did not enter the outbox');
      return { review: review.submission.id };
    }, { workspace, room });

    async function setup(tab: typeof page, holdGrading: boolean) {
      await tab.evaluate(async ({ holdGrading, room, workspace }) => {
        const prefix = '/src/';
        const { ApiError } = await import(prefix + 'api.ts') as typeof import('../src/api');
        const { synchronize } = await import(prefix + 'mobile-sync.ts') as typeof import('../src/mobile-sync');
        const sentNotes: string[] = [];
        Reflect.set(window, 'sentNotes', sentNotes);
        Reflect.set(window, 'reviewReceiptFixture', async (
          command: import('../src/mobile-data').StudyCommand, currentRoom: string,
        ) => {
          if (currentRoom !== room || command.op !== 'receive') throw new Error('Unexpected synchronization command');
          if (command.change.kind === 'note') {
            sentNotes.push(command.change.id);
            if (!Reflect.get(window, 'noteAvailable')) throw new ApiError('测试笔记暂时不可写', 500, {});
            return { id: command.change.id, state: 'saved', revision: 'note-saved', submittedRevision: 'note-saved',
              merged: false, content: command.change.content };
          }
          if (holdGrading) {
            await new Promise<void>(release => {
              window.addEventListener('release-grading-reply', () => release(), { once: true });
              document.documentElement.dataset.gradingReply = 'held';
            });
            return { id: command.change.id, state: 'grading' };
          }
          return { id: command.change.id, state: 'settled', result: {
            outcome: 'passed', stage: 1, dueOn: '2026-11-21', explanation: '评阅完成', submittedOn: '2026-11-20',
          } };
        });
        Reflect.set(window, 'runFixtureSync', async () => {
          try {
            await synchronize(workspace, room);
            return '';
          } catch (error) {
            return (error as Error).message;
          }
        });
      }, { holdGrading, room, workspace });
    }
    await setup(page, true);
    await setup(peer, false);
    const sync = (tab: typeof page): Promise<string> => tab.evaluate(() => Reflect.get(window, 'runFixtureSync')());
    const records = () => peer.evaluate(async () => {
      const modulePath = '/src/mobile-store.ts';
      const { downloaded } = await import(modulePath) as typeof import('../src/mobile-store');
      return downloaded();
    });

    const earlier = sync(page);
    try {
      await expect(page.locator('html')).toHaveAttribute('data-grading-reply', 'held');
      expect(await sync(peer)).toBe('测试笔记暂时不可写');
      const newest = (await records()).find(record => record.kind === 'review');
      expect(newest).toMatchObject({ submission: { id: identifiers.review }, reply: { state: 'settled' } });
    } finally {
      await page.evaluate(() => window.dispatchEvent(new Event('release-grading-reply')));
    }

    expect(await earlier).toBe('测试笔记暂时不可写');
    const latest = (await records()).find(record => record.kind === 'review');
    expect(latest, 'the older grading acknowledgement must leave the settled reply intact').toMatchObject({
      submission: { id: identifiers.review }, reply: { state: 'settled', result: { outcome: 'passed' } },
    });

    await peer.evaluate(() => Reflect.set(window, 'noteAvailable', true));
    expect(await sync(peer)).toBe('');
    const note = (await records()).find(record => record.kind === 'lesson');
    expect(note?.note).toMatchObject({ content: '未同步笔记', revision: 'note-saved' });
    expect(note?.note?.pending).toBeUndefined();
    const attempted = [
      ...await page.evaluate(() => Reflect.get(window, 'sentNotes') as string[]),
      ...await peer.evaluate(() => Reflect.get(window, 'sentNotes') as string[]),
    ];
    expect(attempted).toHaveLength(3);
    expect(new Set(attempted).size).toBe(1);
    await writeFile(info.outputPath('复习回执与笔记重试.json'), JSON.stringify({
      gradingReplyDelayed: true, settledReplyPreserved: true, noteFailureDidNotBlockReview: true,
      noteRetriedWithOriginalId: true,
    }, null, 2));
  } finally {
    await vite.close();
  }
});
