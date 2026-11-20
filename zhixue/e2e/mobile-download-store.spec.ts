import type { AddressInfo } from 'node:net';
import { expect, test } from '@playwright/test';
import { createServer } from 'vite';

test('a download commit verifies its pairing and keeps existing offline work', async ({ page }) => {
  test.setTimeout(60000);
  const vite = await createServer({ configFile: false, server: { host: '127.0.0.1', port: 0, hmr: false } });
  try {
    await vite.listen();
    const { port } = vite.httpServer!.address() as AddressInfo;
    await page.goto('http://127.0.0.1:' + port + '/zhixue.png');
    const result = await page.evaluate(async () => {
      const modulePath = '/src/mobile-store.ts';
      const { PairingChanged, downloaded, saveLesson, saveNote, savePair, savePaper } =
        await import(modulePath) as typeof import('../src/mobile-store');

      const key = await crypto.subtle.importKey('raw', new Uint8Array(32), 'HKDF', false, ['deriveKey']);
      const old = { relay: location.origin, room: 'original-room', key };
      const next = { ...old, room: 'replacement-room' };
      const lesson = {
        id: 'lesson', workspace: 'same-workspace', title: '下载课程', projectTopic: '测试课程',
        body: '# 课文', revision: 'revision-one',
        note: { id: 'note', title: '本节笔记', revision: 'note-one', content: '原始笔记' },
      };
      const paper = {
        id: 'review', workspace: lesson.workspace, title: '复习题组', projectTopic: lesson.projectTopic,
        lessonId: lesson.id, key: 'review-key', dueOn: '2026-11-20', round: 1, kind: 'local' as const,
        groupId: null, basis: 'paper-one', questions: [{ text: '选一个', options: ['表', '列'] }],
      };
      await savePair(old);
      const existing = await saveLesson(lesson, old.room);
      await saveNote(existing!.key, '原始笔记', '本机离线续写');
      const before = await downloaded();

      // Simulate replies already decoded and checked just before another page changed the pair.
      await savePair(next, old.room);
      const delayed = { ...lesson, body: '# 旧配对的迟到正文', revision: 'stale-revision' };
      const blocked = await Promise.allSettled([saveLesson(delayed, old.room), savePaper(paper, old.room)]);
      const afterOutdated = await downloaded();
      await saveLesson({ ...lesson, revision: 'duplicate-revision' }, next.room);
      await savePaper(paper, next.room);
      const current = await downloaded();
      return {
        blocked: blocked.map(reply => reply.status === 'rejected' && reply.reason instanceof PairingChanged),
        before, afterOutdated, current,
      };
    });
    expect(result.blocked).toEqual([true, true]);
    expect(result.afterOutdated).toEqual(result.before);
    expect(result.current).toHaveLength(2);
    expect(result.current.find(record => record.kind === 'lesson')).toEqual(result.before[0]);
    expect(result.current.find(record => record.kind === 'review')).toMatchObject({ draft: [null] });
  } finally {
    await vite.close();
  }
});

for (const desktop of [false, true]) {
  test('a removed download rejects older course and paper replies in ' + (desktop ? 'the desktop store' : 'IndexedDB'),
    async ({ page }) => {
      test.setTimeout(60000);
      const vite = await createServer({ configFile: false, server: { host: '127.0.0.1', port: 0, hmr: false } });
      try {
        await vite.listen();
        const { port } = vite.httpServer!.address() as AddressInfo;
        await page.goto('http://127.0.0.1:' + port + '/zhixue.png');
        const result = await page.evaluate(async desktopStorage => {
          const records = new Map<string, { revision: number; value?: unknown }>();
          let removeWhileWriting = false;
          if (desktopStorage) {
            sessionStorage.setItem('zhixue-session', 'isolated desktop storage');
            window.zhixueDesktop = {
              onWindowHidden: () => () => {},
              offline: {
                list: async () => [...records.values()].map(item => item.value).filter(value => value !== undefined),
                read: async (_, key) => records.get(key) ?? { revision: 0 },
                write: async (_, key, revision, value) => {
                  const previous = records.get(key)?.revision ?? 0;
                  if (removeWhileWriting) {
                    removeWhileWriting = false;
                    records.set(key, { revision: previous + 1 });
                    return false;
                  }
                  if (previous !== revision) return false;
                  records.set(key, { revision: revision + 1, value });
                  return true;
                },
              },
            };
          }
          const modulePath = '/src/mobile-store.ts';
          const { downloaded, downloadVersion, removeDownload, saveAnswers, saveLesson, savePaper } =
            await import(modulePath) as typeof import('../src/mobile-store');
          const dataPath = '/src/mobile-data.ts';
          const { studyKey } = await import(dataPath) as typeof import('../src/mobile-data');
          const lesson = { id: 'lesson', workspace: 'workspace', title: '课节', projectTopic: '项目',
            body: '原课文', revision: 'one', note: null };
          const paper = { id: 'paper', workspace: 'workspace', title: '题组', projectTopic: '项目',
            lessonId: lesson.id, key: 'review-key', dueOn: '2026-11-20', round: 1, kind: 'local' as const,
            groupId: null, basis: 'one', questions: [{ text: '简答' }] };
          const lessonKey = studyKey('lesson', lesson.workspace, lesson.id);
          const paperKey = studyKey('review', paper.workspace, paper.id);
          const oldLesson = await downloadVersion(lessonKey);
          const oldPaper = await downloadVersion(paperKey);
          await saveLesson(lesson, undefined, oldLesson);
          await savePaper(paper, undefined, oldPaper);
          await removeDownload(lessonKey);
          await removeDownload(paperKey);
          const staleLesson = await saveLesson(lesson, undefined, oldLesson);
          const stalePaper = await savePaper(paper, undefined, oldPaper);
          const emptyAfterRemoval = (await downloaded()).length === 0;
          if (desktopStorage) {
            const fresh = await downloadVersion(lessonKey);
            removeWhileWriting = true;
            if (await saveLesson(lesson, undefined, fresh)) throw new Error('CAS retried a removed download');
          }

          await saveLesson({ ...lesson, body: '重新下载的课文', revision: 'two' }, undefined, await downloadVersion(lessonKey));
          await savePaper({ ...paper, basis: 'two' }, undefined, await downloadVersion(paperKey));
          // A response from before removal cannot overwrite the freshly downloaded record.
          await saveLesson(lesson, undefined, oldLesson);
          await savePaper(paper, undefined, oldPaper);
          const keptPaper = (await downloaded()).find(item => item.key === paperKey);
          if (keptPaper?.kind !== 'review') throw new Error('Expected the downloaded review');
          await saveAnswers(paperKey, keptPaper.draft, ['本机未同步答案']);
          let draftProtected = false;
          try { await removeDownload(paperKey); }
          catch (error) { draftProtected = error instanceof Error && error.message.includes('本机草稿'); }
          return { staleBlocked: !staleLesson && !stalePaper, emptyAfterRemoval, draftProtected, current: await downloaded() };
        }, desktop);
        expect(result).toMatchObject({ staleBlocked: true, emptyAfterRemoval: true, draftProtected: true });
        expect(result.current).toHaveLength(2);
        expect(result.current.find(item => item.kind === 'lesson')).toMatchObject({ snapshot: { revision: 'two' } });
        expect(result.current.find(item => item.kind === 'review')).toMatchObject({ snapshot: { basis: 'two' }, draft: ['本机未同步答案'] });
      } finally {
        await vite.close();
      }
    });
}
