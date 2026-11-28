import { execFileSync, spawn, spawnSync } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { once } from 'node:events';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, realpathSync,
  renameSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { dirname, isAbsolute, join, relative, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { _electron as electron, expect, test, type Locator, type Page } from '@playwright/test';

test.describe.configure({ timeout: 270000 });

const installed = process.env.ZHIXUE_TEST_ELECTRON;
const oldService = process.env.ZHIXUE_0051_SERVICE;
const schemaFixture = process.env.ZHIXUE_0051_FIXTURE === '1';
const python = resolve(process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');

const home = '今天，想弄懂什么？';
const oldSchema = '0051_course_anchor_links';
let migrationHead: string | undefined;
const noteText = '宏观迁移验收。每行代表一条独立记录。';
const originalText = '安装验收隔离资料，表由记录和字段组成。';
const password = 'installed app isolated archive fixture only';

type ReviewSchedule = {
  lessonId: string;
  key: string;
  title: string;
  stage: number;
  dueOn: string;
};

function schedule({ lessonId, key, title, stage, dueOn }: ReviewSchedule) {
  return { lessonId, key, title, stage, dueOn };
}

function readSqlite(file: string, statement: string) {
  // Apple's /usr/bin/sqlite3 -readonly returns SQLITE_CANTOPEN (14) for a WAL
  // database whose -wal and -shm files were removed on a clean close. The
  // locked Python SQLite creates the shared-memory file for a read-only URI.
  return execFileSync(python, ['-c',
    'from pathlib import Path; import sqlite3, sys; ' +
    'db = sqlite3.connect(Path(sys.argv[1]).as_uri() + "?mode=ro", uri=True); ' +
    'print(db.execute(sys.argv[2]).fetchone()[0]); db.close()', file, statement,
  ], { encoding: 'utf8', windowsHide: true }).trim();
}

function schema(file: string) {
  return readSqlite(file, 'SELECT version_num FROM alembic_version');
}

function expectedMigrationHead() {
  migrationHead ??= process.env.ZHIXUE_TEST_MIGRATION_HEAD ?? execFileSync(python, ['-c', [
    'from alembic.script import ScriptDirectory',
    'from backend.db import ROOT',
    'print(ScriptDirectory(str(ROOT / "migrations")).get_current_head())',
  ].join('\n')], { encoding: 'utf8', windowsHide: true }).trim();
  expect(migrationHead).toMatch(/^\d{4}_[a-z0-9_]+$/);
  return migrationHead;
}

function downgradeFixture(workspace: string) {
  execFileSync(python, ['-c', `
import sys
from pathlib import Path
from alembic import command
from alembic.config import Config
from backend.db import ROOT, open_db

config = Config()
config.set_main_option("script_location", str(ROOT / "migrations").replace("%", "%%"))
engine = open_db(Path(sys.argv[1]))
try:
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, sys.argv[2])
finally:
    engine.dispose()
`, workspace, oldSchema], { windowsHide: true });
}

function digest(file: string) {
  return createHash('sha256').update(readFileSync(file)).digest('hex');
}

async function seedWorkspace(executable: string, folder: string, fileBody = originalText) {
  const child = spawn(executable, ['--stdio', '--port', '0', '--data', folder], {
    stdio: ['pipe', 'pipe', 'inherit'], windowsHide: true,
  });
  child.stdin.on('error', () => {});
  const closed = once(child, 'close').then(([status]) => status);
  const reader = createInterface({ input: child.stdout });
  try {
    const line = once(reader, 'line').then(([address]) => new URL(address));
    const timeout = once(AbortSignal.timeout(60000), 'abort').then(() => { throw new Error('Seed service startup timed out'); });
    const origin = await Promise.race([
      line, timeout, closed.then(status => { throw new Error('Seed service exited before startup ' + status); }),
    ]);
    const token = new URLSearchParams(origin.hash.split('?')[1]).get('session');
    expect(origin.hostname).toBe('127.0.0.1');
    expect(token).toBeTruthy();
    function send(path: string, method = 'GET', data?: object) {
      return fetch(origin.origin + '/api' + path, {
        method, headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
        body: data === undefined ? undefined : JSON.stringify(data), signal: AbortSignal.timeout(10000),
      });
    }
    async function api(path: string, method = 'GET', data?: object) {
      const response = await send(path, method, data).catch(error => {
        throw new Error(`Seed workspace request ${method} ${path} failed: ${error.message}`, { cause: error });
      });
      expect(response.ok, path + ' HTTP ' + response.status).toBeTruthy();
      return response.json();
    }

    await api('/setup/storage', 'PUT', { directory: null });
    await expect.poll(async () => {
      const response = await send('/setup').catch(error => {
        if (error instanceof TypeError) return null;
        throw new Error(`Seed workspace transition did not respond: ${error.message}`, { cause: error });
      });
      return response?.ok ? (await response.json()).stage : null;
    }, { timeout: 20000 }).toBe('models');
    expect(relative(folder, realpathSync((await api('/setup')).directory))).toBe('');
    await api('/setup/complete', 'POST');
    const projectId = randomUUID();
    const project = await api('/projects', 'POST', { id: projectId, topic: 'SQLite', outcome: '复验升级与正式复习' });
    await expect.poll(async () => (await api('/projects/' + projectId)).task.status).toBe('ready');
    const lessonId = project.lessons[0].id;
    const lesson = '/lessons/' + lessonId;
    const note = await api(lesson + '/note');
    const savedNote = await api(lesson + '/note', 'PUT', { content: noteText, baseRevision: note.revision });
    await api(lesson + '/learned', 'PUT', { learned: true });
    const learnedOn = (await api('/projects/' + projectId)).lessons[0].learnedOn;
    const imported = await api('/files', 'POST', { name: '迁移验收资料.txt', content: fileBody });
    const reviews: ReviewSchedule[] = (await api('/reviews')).reviews.map(schedule);
    expect(learnedOn).toBeTruthy();
    expect(reviews).toHaveLength(2);
    return { projectId, lessonId, learnedOn, originalId: imported.id, noteRevision: savedNote.revision, reviews };
  } finally {
    reader.close();
    child.stdin.end();
    const stop = setTimeout(() => child.kill('SIGKILL'), 10000).unref();
    try {
      expect(await closed).toBe(0);
    } finally {
      clearTimeout(stop);
    }
  }
}

async function withApp(
  folder: string, exercise: (page: Page) => Promise<void>, shutdown: 'normal' | 'terminate' = 'normal',
) {
  const app = await electron.launch({
    executablePath: installed,
    env: { ...process.env, ZHIXUE_DEMO_DATA: folder },
    timeout: 60000,
  });
  const processHandle = app.process();
  let health: URL | undefined;
  try {
    expect(await app.evaluate(({ app }) => app.isPackaged)).toBe(true);
    const page = await app.firstWindow();
    await expect(page.locator('body')).toBeVisible();
    health = new URL('/healthz', page.url());
    await exercise(page);
  } finally {
    if (processHandle.exitCode === null && processHandle.signalCode === null) {
      if (shutdown === 'terminate') {
        const mainPID = await app.evaluate(() => process.pid);
        expect(process.kill(mainPID, 'SIGKILL')).toBe(true);
      } else {
        await app.close();
      }
    }
    if (shutdown === 'terminate') {
      await expect.poll(() => processHandle.exitCode !== null || processHandle.signalCode !== null).toBe(true);
    } else {
      await expect.poll(() => processHandle.exitCode).toBe(0);
    }
    if (health) {
      await expect.poll(() => fetch(health!, { signal: AbortSignal.timeout(750) })
        .then(() => false, error => error.cause?.code === 'ECONNREFUSED')).toBe(true);
    }
  }
}

async function wire(page: Page, path: string, method = 'GET', data?: object) {
  const token = await page.evaluate(() => sessionStorage.getItem('zhixue-session'));
  expect(token).toBeTruthy();
  return page.request.fetch(new URL('/api' + path, page.url()).href, {
    method, data, headers: { Authorization: 'Bearer ' + token },
  });
}

async function api(page: Page, path: string, method = 'GET', data?: object) {
  const response = await wire(page, path, method, data);
  expect(response.ok(), path + ' HTTP ' + response.status()).toBeTruthy();
  return response.json();
}

type SeededWorkspace = Awaited<ReturnType<typeof seedWorkspace>>;

async function verifyRestoredWorkspace(
  page: Page, folder: string, current: SeededWorkspace, missing: SeededWorkspace, fileBody: string,
) {
  expect(await api(page, '/lessons/' + current.lessonId + '/note'))
    .toMatchObject({ revision: current.noteRevision, content: noteText });
  expect((await api(page, '/reviews')).reviews.map(schedule)).toEqual(current.reviews);
  const file = await api(page, '/files/' + current.originalId);
  expect(file.content).toBe(fileBody);
  const stored = realpathSync(join(folder, file.path));
  const localPath = relative(join(folder, 'library'), stored);
  expect(localPath && !isAbsolute(localPath) && !/^\.\.(?:[\\/]|$)/.test(localPath)).toBeTruthy();
  expect(readFileSync(stored, 'utf8')).toBe(fileBody);
  expect((await wire(page, '/projects/' + missing.projectId)).status()).toBe(404);
  expect((await wire(page, '/files/' + missing.originalId)).status()).toBe(404);
  const database = join(folder, 'zhixue.sqlite3');
  expect(schema(database)).toBe(expectedMigrationHead());
  expect(readSqlite(database, 'PRAGMA quick_check')).toBe('ok');
}

test('the installed Electron app finds a renamed backup past a same-size damaged copy', async ({}, info) => {
  test.skip(!installed, 'Set ZHIXUE_TEST_ELECTRON to run against an installed app');
  const parent = realpathSync(tmpdir());
  const root = realpathSync(mkdtempSync(join(parent, 'zhixue-installed-backup-selection-')));
  const workspace = join(root, '学习资料 # %');
  const media = join(root, '归档 # %');
  mkdirSync(workspace);
  mkdirSync(media);
  const originalName = '找回验收资料.txt';
  const editedText = '制作备份后保存的现行资料。';
  const renamedName = '已改名的完整备份.zhixue-backup';
  const openBackups = async (page: Page) => {
    await page.getByRole('link', { name: '管理中心', exact: true }).click();
    await page.getByRole('link', { name: '备份与恢复', exact: true }).click();
  };
  const openPreview = async (page: Page) => {
    await page.getByRole('button', { name: '预览备份', exact: true }).click();
    const dialog = page.getByRole('dialog');
    await dialog.getByLabel('备份密码').fill(password);
    const response = page.waitForResponse(reply => reply.request().method() === 'POST' &&
      new URL(reply.url()).pathname.endsWith('/preview'));
    await dialog.getByRole('button', { name: '解锁并比较', exact: true }).click();
    const preview = await response;
    expect(preview.status(), '只读取选中归档的预览 · ' + await preview.text()).toBe(200);
    await expect(dialog.getByRole('heading', { name: '整体恢复预览', exact: true })).toBeVisible();
    return dialog;
  };

  try {
    let archive!: { name: string; id: string };
    let storedPath!: string;
    let fileId!: string;
    let copyId!: string;
    let intactDigest!: string;
    let damagedDigest!: string;
    await withApp(workspace, async page => {
      await page.getByRole('button', { name: '使用此位置', exact: true }).click();
      await page.getByRole('button', { name: '稍后配置模型', exact: true }).click();
      await expect(page.getByRole('heading', { name: home })).toBeVisible();
      const original = await api(page, '/files', 'POST', { name: originalName, content: originalText });
      fileId = original.id;
      storedPath = (await api(page, '/files/' + fileId)).path;
      await api(page, '/backups/destination', 'PUT', { directory: media });
      archive = await api(page, '/backups', 'POST', { password });
      const recorded = join(media, archive.name);
      const intact = join(media, renamedName);
      const bytes = readFileSync(recorded);
      intactDigest = digest(recorded);
      renameSync(recorded, intact);
      bytes[bytes.length - 1] ^= 1;
      writeFileSync(recorded, bytes, { flag: 'wx' });
      damagedDigest = digest(recorded);
      expect(damagedDigest).not.toBe(intactDigest);
      expect(readFileSync(recorded).length).toBe(readFileSync(intact).length);
      writeFileSync(join(workspace, storedPath), editedText, 'utf8');

      await openBackups(page);
      const dialog = await openPreview(page);
      await expect(page.getByText(renamedName, { exact: true })).toBeVisible();
      await dialog.getByRole('button', { name: '找回文件', exact: true }).click();
      await dialog.getByRole('button', { name: '选取 ' + originalName, exact: true }).click();
      await expect(dialog.getByText('当前文件内容不同', { exact: true })).toBeVisible();
      const recovered = page.waitForResponse(reply => reply.request().method() === 'POST' &&
        new URL(reply.url()).pathname === '/api/backups/' + archive.id + '/files/restore');
      await dialog.getByRole('button', { name: '确认找回副本', exact: true }).click();
      const response = await recovered;
      expect(response.status()).toBe(201);
      copyId = (await response.json()).id;
      expect(copyId).not.toBe(fileId);
      await dialog.getByRole('link', { name: '打开找回副本', exact: true }).click();
      await expect(page.getByText(originalText, { exact: true })).toBeVisible();
    });

    await withApp(workspace, async page => {
      await expect(page.getByRole('heading', { name: home })).toBeVisible();
      expect((await api(page, '/files/' + fileId)).content).toBe(editedText);
      const copy = await api(page, '/files/' + copyId);
      expect(copy.content).toBe(originalText);
      expect(readFileSync(join(workspace, copy.path), 'utf8')).toBe(originalText);
      await openBackups(page);
      await openPreview(page);
      await expect(page.getByText(renamedName, { exact: true })).toBeVisible();
    });
    expect(readFileSync(join(workspace, storedPath), 'utf8')).toBe(editedText);
    expect(digest(join(media, renamedName))).toBe(intactDigest);
    expect(digest(join(media, archive.name))).toBe(damagedDigest);
    expect(readSqlite(join(workspace, 'zhixue.sqlite3'), 'PRAGMA quick_check')).toBe('ok');
    await info.attach('backup-selection', { contentType: 'application/json', body: JSON.stringify({
      archiveId: archive.id, recordedName: archive.name, renamedName, intactDigest, damagedDigest, copyId,
      previewed: true, recoveredCopyAfterRestart: true, currentFilePreserved: true, integrity: 'ok',
    }, null, 2) });
  } finally {
    expect(dirname(realpathSync(root))).toBe(parent);
    rmSync(root, { recursive: true });
  }
});

test('the installed Electron app recovers workspace switches across restarts', async () => {
  test.skip(!installed, 'Set ZHIXUE_TEST_ELECTRON to run against an installed app');
  const appDirectory = dirname(resolve(installed!));
  const executable = process.platform === 'darwin'
    ? join(appDirectory, '..', 'Resources', 'zhixue-service')
    : join(appDirectory, 'resources', 'zhixue-service.exe');
  const parent = realpathSync(tmpdir());
  const root = realpathSync(mkdtempSync(join(parent, 'zhixue-installed-switch-restart-')));
  expect(dirname(root)).toBe(parent);
  const receiver = join(root, '恢复接收端');
  const restoreWork = join(receiver, '.restore-work');
  const database = 'zhixue.sqlite3';
  const receiverText = '整库恢复前，接收端独有的知识库原文。';
  try {
    const original = await seedWorkspace(executable, receiver, receiverText);
    const originalDigest = digest(join(receiver, database));

    for (const committed of [false, true]) {
      const id = randomUUID().replaceAll('-', '');
      const backupId = randomUUID();
      const producer = join(root, '备份源-' + id);
      const incoming = await seedWorkspace(executable, producer);
      const incomingDigest = digest(join(producer, database));
      expect(originalDigest).not.toBe(incomingDigest);
      const job = join(restoreWork, 'switch-' + id);
      for (const name of ['incoming', 'retained', 'discarded']) mkdirSync(join(job, name), { recursive: true });

      // Recreate the durable layout at the two interruption points without a Python runtime on the test host.
      for (const item of [database, 'library']) {
        renameSync(join(producer, item), join(job, 'incoming', item));
        renameSync(join(receiver, item), join(job, 'retained', item));
      }
      renameSync(join(job, 'incoming', database), join(receiver, database));
      if (committed) renameSync(join(job, 'incoming', 'library'), join(receiver, 'library'));
      writeFileSync(join(job, 'journal.json'), JSON.stringify({
        phase: committed ? 'committed' : 'switching',
        original: [database, 'library'],
        receipt: { id, backupId, state: 'complete' },
      }), { flush: true });
      expect(digest(join(receiver, database))).toBe(incomingDigest);
      expect(existsSync(join(receiver, 'library'))).toBe(committed);
      const receipt = committed ? { id, backupId, state: 'complete' }
        : { id, backupId, state: 'failed', code: 'interrupted' };

      for (const launch of ['recovery', 'second restart']) {
        await withApp(receiver, async page => {
          await expect(page.getByRole('heading', { name: home })).toBeVisible();
          await verifyRestoredWorkspace(
            page, receiver, committed ? incoming : original, committed ? original : incoming,
            committed ? originalText : receiverText,
          );
          expect(await api(page, '/backups/restore-status')).toMatchObject(receipt);
          expect(JSON.parse(readFileSync(join(restoreWork, 'last-restore.json'), 'utf8'))).toMatchObject(receipt);
          expect(readdirSync(restoreWork).filter(name => name.startsWith('switch-'))).toEqual([]);
        });
        console.log(`PASS ${launch} after ${committed ? 'committed' : 'interrupted'} workspace switch`);
      }
    }
  } finally {
    rmSync(root, { force: true, recursive: true, maxRetries: 5, retryDelay: 200 });
  }
});

test('the installed Electron app migrates, completes a review, and restores to a fresh workspace', async () => {
  test.skip(!installed, 'Set ZHIXUE_TEST_ELECTRON to run against an installed app');
  expect(Boolean(oldService) !== schemaFixture, 'Choose an actual 0051 service or explicit schema fixture mode').toBe(true);
  const executable = schemaFixture
    ? join(dirname(resolve(installed!)), 'resources', 'zhixue-service.exe')
    : resolve(oldService!);
  if (schemaFixture) expect(process.platform).toBe('win32');
  const root = realpathSync(mkdtempSync(join(tmpdir(), 'zhixue-installed-migrate-')));
  const control = join(root, 'older-control');
  const workspace = control;
  const receiver = join(root, 'fresh-control');
  const receiverWorkspace = receiver;
  const media = join(root, 'encrypted-archives');
  for (const dir of [control, receiver, media]) mkdirSync(dir);
  try {
    const fixture = await seedWorkspace(executable, workspace);
    const lesson = '/lessons/' + fixture.lessonId;
    const db = join(workspace, 'zhixue.sqlite3');
    const courseFile = join(workspace, 'library', 'courses', fixture.lessonId + '.md');
    if (schemaFixture) {
      expect(schema(db)).toBe(expectedMigrationHead());
      downgradeFixture(workspace);
    }
    expect(schema(db)).toBe(oldSchema);
    console.log(schemaFixture ? 'Fixture: installed Windows service data downgraded to 0051' : 'Fixture: original 0051 service');
    const courseDigest = digest(courseFile);
    const objective = fixture.reviews.find(item => item.title === '区分行与列');
    expect(objective).toBeDefined();
    const reviewPath = `${lesson}/reviews/${objective!.key}`;
    let completed: ReviewSchedule[];
    let attempt: object;
    let backupId: string;

    await withApp(control, async page => {
      await expect(page.getByRole('heading', { name: home })).toBeVisible();
      expect(schema(db)).toBe(expectedMigrationHead());
      const upgrades = join(workspace, 'backups', 'database-upgrades');
      const [oldCopy] = readdirSync(upgrades).filter(name => name.endsWith('.sqlite3'));
      expect(oldCopy, 'Migration should leave a readable pre-upgrade SQLite copy').toBeTruthy();
      expect(schema(join(upgrades, oldCopy))).toBe(oldSchema);
      expect(readSqlite(join(upgrades, oldCopy), 'PRAGMA quick_check')).toBe('ok');
      expect(digest(courseFile)).toBe(courseDigest);
      expect(await api(page, lesson + '/note')).toMatchObject({ content: noteText, revision: fixture.noteRevision });
      expect(await api(page, '/projects/' + fixture.projectId)).toMatchObject({ lessons: [
        expect.objectContaining({ id: fixture.lessonId, learnedOn: fixture.learnedOn }),
        expect.any(Object), expect.any(Object),
      ] });
      expect((await api(page, '/reviews')).reviews.map(schedule)).toEqual(fixture.reviews);
      console.log('PASS 0051 → ' + expectedMigrationHead() + ', pre-upgrade SQLite, source course, note, progress and review due dates');

      await page.evaluate(path => { location.hash = path; }, lesson);
      await expect(page.getByRole('heading', { name: '一张表能记下什么' })).toBeVisible();
      await expect(page.getByRole('checkbox', { name: '已学完' })).toBeChecked();
      await page.getByRole('link', { name: '知识库', exact: true }).click();
      await page.getByRole('searchbox', { name: '搜索知识库' }).fill('独立记录');
      const match = page.getByRole('button', { name: /理解表、行和列 · 笔记/ });
      await match.click();
      await expect(page.getByText(noteText, { exact: true })).toBeVisible();

      await page.getByRole('link', { name: '今日复习', exact: true }).click();
      await page.getByRole('button', { name: '推进试用日期一天' }).click();
      await page.getByRole('link', { name: /区分行与列/ }).click();
      await page.getByRole('radio', { name: '一行', exact: true }).first().check();
      await page.getByRole('radio', { name: '一列', exact: true }).last().check();
      await page.getByRole('button', { name: '提交本组答案' }).click();
      await expect(page.getByRole('heading', { name: '独立通过', exact: true })).toBeVisible();
      completed = (await api(page, '/reviews')).reviews.map(schedule);
      const next = completed.find(item => item.lessonId === fixture.lessonId && item.key === objective!.key);
      expect(next?.stage).toBe(objective!.stage + 1);
      expect(next!.dueOn > objective!.dueOn).toBe(true);
      const result = await api(page, reviewPath);
      expect(result.latest).toMatchObject({ outcome: 'passed', stage: next?.stage, dueOn: next?.dueOn });
      attempt = result.latest;
      expect(completed.filter(item => item.key !== objective!.key)).toEqual(
        fixture.reviews.filter(item => item.key !== objective!.key));
      console.log('PASS installed window reads a lesson, searches a saved note and submits an independent review');

      await api(page, '/backups/destination', 'PUT', { directory: media });
      const backup = await api(page, '/backups', 'POST', { password });
      expect(backup.status).toBe('ready');
      backupId = backup.id;
    });

    await withApp(receiver, async page => {
      await page.getByRole('button', { name: '使用此位置', exact: true }).click();
      await page.getByRole('button', { name: '稍后配置模型', exact: true }).click();
      await expect(page.getByRole('heading', { name: home })).toBeVisible();
      const local = await api(page, '/files', 'POST', { name: '恢复前的本机资料.txt', content: '只存在于隔离接收端。' });
      await api(page, '/backups/destination', 'PUT', { directory: media });
      expect((await wire(page, '/backups/' + backupId + '/preview', 'POST', { password: 'different test password' })).ok()).toBe(false);
      expect(await api(page, '/files/' + local.id)).toMatchObject({ content: '只存在于隔离接收端。' });
      await page.getByRole('link', { name: '管理中心', exact: true }).click();
      await page.getByRole('link', { name: '备份与恢复', exact: true }).click();
      await page.getByRole('button', { name: '预览备份', exact: true }).click();
      const dialog = page.getByRole('dialog');
      await dialog.getByLabel('备份密码').fill(password);
      await dialog.getByRole('button', { name: '解锁并比较', exact: true }).click();
      await expect(dialog.getByText('恢复前的本机资料.txt', { exact: true })).toBeVisible();
      await dialog.getByRole('button', { name: '继续整体恢复', exact: true }).click();
      await dialog.getByRole('button', { name: '确认整体恢复', exact: true }).click();
      await expect(page.getByText('整体恢复完成。恢复前的数据已保存为加密备份。', { exact: true })).toBeVisible({ timeout: 25000 });
      const rollback = page.getByRole('article').filter({ hasText: '恢复前快照' });
      await expect(rollback.getByRole('button', { name: '取消永久保留', exact: true })).toBeVisible();
      await rollback.getByRole('button', { name: '预览备份', exact: true }).click();
      await dialog.getByLabel('备份密码').fill(password);
      await dialog.getByRole('button', { name: '解锁并比较', exact: true }).click();
      await expect(dialog.getByText('恢复前的本机资料.txt', { exact: true })).toBeVisible();
      await page.keyboard.press('Escape');
      expect((await wire(page, '/files/' + local.id)).status()).toBe(404);
      expect(await api(page, '/files/' + fixture.originalId)).toMatchObject({ content: originalText });
      expect(await api(page, lesson + '/note')).toMatchObject({ content: noteText });
      console.log('PASS incorrect password preserves existing files; confirmed encrypted restore saves the receiving workspace snapshot');
    });

    await withApp(receiver, async page => {
      await expect(page.getByRole('heading', { name: home })).toBeVisible();
      const restoredDb = join(receiverWorkspace, 'zhixue.sqlite3');
      expect(schema(restoredDb)).toBe(expectedMigrationHead());
      expect(readSqlite(restoredDb, 'PRAGMA quick_check')).toBe('ok');
      expect(readSqlite(restoredDb, 'SELECT count(*) FROM model_charges')).toBe('0');
      expect(digest(join(receiverWorkspace, 'library', 'courses', fixture.lessonId + '.md'))).toBe(courseDigest);
      expect(await api(page, '/files/' + fixture.originalId)).toMatchObject({ content: originalText });
      expect(await api(page, lesson + '/note')).toMatchObject({ content: noteText, revision: fixture.noteRevision });
      expect((await api(page, '/reviews')).reviews.map(schedule)).toEqual(completed);
      expect((await api(page, reviewPath)).latest).toEqual(attempt);
      expect((await api(page, '/projects/' + fixture.projectId)).lessons[0].learnedOn).toBe(fixture.learnedOn);
      console.log('PASS after restart: course, knowledge file, note, review schedule and SQLite integrity');
    });
  } finally {
    rmSync(root, { force: true, recursive: true });
  }
});

async function withWindowsPathLocked(path: string, exercise: () => Promise<void>) {
  // Share reads and writes, but deny renaming or replacing the selected path.
  const child = spawn(python, ['-c', [
    'from ctypes import WinDLL, WinError, get_last_error',
    'from ctypes import wintypes as win',
    'import sys',
    'kernel = WinDLL("kernel32", use_last_error=True)',
    'create = kernel.CreateFileW',
    'create.argtypes = [win.LPCWSTR, win.DWORD, win.DWORD, win.LPVOID, win.DWORD, win.DWORD, win.HANDLE]',
    'create.restype = win.HANDLE',
    'kernel.CloseHandle.argtypes = [win.HANDLE]',
    'generic_read, share_read_write, open_existing, backup_semantics = 0x80000000, 3, 3, 0x02000000',
    'handle = create(sys.argv[1], generic_read, share_read_write, None, open_existing, backup_semantics, None)',
    'if handle == win.HANDLE(-1).value:',
    '    raise WinError(get_last_error())',
    'try:',
    '    print("locked", flush=True)',
    '    sys.stdin.buffer.read()',
    'finally:',
    '    if not kernel.CloseHandle(handle):',
    '        raise WinError(get_last_error())',
  ].join('\n'), path], { stdio: ['pipe', 'pipe', 'inherit'], windowsHide: true });
  child.stdin.on('error', () => {});
  const closed = once(child, 'close').then(([code]) => code);
  const reader = createInterface({ input: child.stdout });
  try {
    const pending = once(reader, 'line', { signal: AbortSignal.timeout(5000) }).then(([line]) => line);
    expect(await Promise.race([pending, closed])).toBe('locked');
    await exercise();
  } finally {
    reader.close();
    child.stdin.end();
    const stop = setTimeout(() => child.kill(), 5000).unref();
    try {
      expect(await closed).toBe(0);
    } finally {
      clearTimeout(stop);
    }
  }
}

function interruptWindowsSwitch(receiver: string, producer: string, backupId: string) {
  const id = randomUUID().replaceAll('-', '');
  const script = [
    'import os, sys',
    'from pathlib import Path',
    'from shutil import copy2, copytree',
    'from backend import backup_restore as restore',
    'folder, source = map(Path, sys.argv[1:3])',
    'identity, backup_id = sys.argv[3:]',
    'job = restore.restore_root(folder) / ("switch-" + identity)',
    'incoming = job / "incoming"',
    'incoming.mkdir(parents=True)',
    'copy2(source / restore.DATABASE, incoming / restore.DATABASE)',
    'copytree(source / "library", incoming / "library")',
    'move = restore.move',
    'def stop_after_database_switch(origin, target):',
    '    move(origin, target)',
    '    if target == folder / restore.DATABASE:',
    '        os._exit(73)',
    'restore.move = stop_after_database_switch',
    'receipt = {"id": identity, "backupId": backup_id, "state": "complete"}',
    'restore.switch_workspace(folder, job, receipt, restore.scan_library(folder))',
  ].join('\n');
  const process = spawnSync(python, ['-c', script, receiver, producer, id, backupId],
    { encoding: 'utf8', timeout: 12000, windowsHide: true });
  expect(process.status, process.stderr || String(process.error ?? '')).toBe(73);
  return id;
}

test('installed Windows Electron recovers encrypted restores before and after commit', async () => {
  test.skip(!installed || process.platform !== 'win32', 'requires an installed Windows Electron candidate');
  const executable = join(dirname(resolve(installed!)), 'resources', 'zhixue-service.exe');
  const parent = realpathSync(tmpdir());
  const root = realpathSync(mkdtempSync(join(parent, 'zhixue-installed-restore-fault-')));
  expect(dirname(root)).toBe(parent);
  const producer = join(root, '备份源');
  const receiver = join(root, '恢复接收端');
  const media = join(root, '加密备份');
  const library = join(receiver, 'library');
  const database = join(receiver, 'zhixue.sqlite3');
  const restoreWork = join(receiver, '.restore-work');
  const receiptFile = join(restoreWork, 'last-restore.json');
  const receiverText = '恢复接收端独有资料。切换前和切换后的文件内容可以区分。';
  try {
    for (const dir of [producer, receiver, media]) mkdirSync(dir);
    const source = await seedWorkspace(executable, producer);
    const original = await seedWorkspace(executable, receiver, receiverText);
    let archive: { id: string; directory: string; name: string } | undefined;
    await withApp(producer, async page => {
      await expect(page.getByRole('heading', { name: home })).toBeVisible();
      await api(page, '/backups/destination', 'PUT', { directory: media });
      const backup = await api(page, '/backups', 'POST', { password });
      expect(backup.status).toBe('ready');
      archive = backup;
    });
    expect(archive).toBeDefined();
    const archivePath = join(archive!.directory, archive!.name);
    expect(relative(media, dirname(realpathSync(archivePath)))).toBe('');
    const backupEndpoint = '/backups/' + archive!.id;
    const verifyWorkspace = (page: Page, current: SeededWorkspace, missing: SeededWorkspace, fileBody: string) =>
      verifyRestoredWorkspace(page, receiver, current, missing, fileBody);
    const requestRestore = async (page: Page, confirmation: string, endpoint = backupEndpoint) => {
      const request = await wire(page, endpoint + '/restore', 'POST', { password, confirmation });
      expect(request.status()).toBe(202);
      const { id } = await request.json();
      let result: { id: string; state: string; code?: string; rollbackBackupId?: string };
      await expect.poll(async () => {
        result = await api(page, '/backups/restore-status');
        return result.id === id && result.state !== 'running';
      }, { timeout: 45000 }).toBe(true);
      return result!;
    };
    const oldDatabase = digest(database);
    const crash = interruptWindowsSwitch(receiver, producer, archive!.id);
    const pendingSwitch = join(restoreWork, 'switch-' + crash);
    expect(digest(database)).toBe(digest(join(producer, 'zhixue.sqlite3')));
    expect(digest(database)).not.toBe(oldDatabase);
    expect(existsSync(library)).toBe(false);
    expect(existsSync(join(pendingSwitch, 'retained', 'library'))).toBe(true);
    expect(JSON.parse(readFileSync(join(pendingSwitch, 'journal.json'), 'utf8')).phase).toBe('switching');
    let failedSwitch = '';
    await withApp(receiver, async page => {
      await expect(page.getByRole('heading', { name: home })).toBeVisible();
      await verifyWorkspace(page, original, source, receiverText);
      expect(await api(page, '/backups/restore-status'))
        .toMatchObject({ id: crash, state: 'failed', code: 'interrupted' });
      expect(existsSync(pendingSwitch)).toBe(false);
      console.log('PASS packaged startup reconciles an interrupted switch after the incoming database moves');
      await api(page, '/backups/destination', 'PUT', { directory: media });
      const preview = await api(page, backupEndpoint + '/preview', 'POST', { password });
      expect(preview.restore.confirmation).toMatch(/^[0-9a-f]{64}$/);
      const valid = readFileSync(archivePath);
      const corrupted = Buffer.from(valid);
      corrupted[corrupted.length - 1] ^= 1;
      writeFileSync(archivePath, corrupted);
      try {
        const rejected = await wire(page, backupEndpoint + '/preview', 'POST', { password });
        expect(rejected.status()).toBe(422);
        expect(await rejected.json()).toMatchObject({ code: 'authentication' });
        const failed = await requestRestore(page, preview.restore.confirmation);
        expect(failed).toMatchObject({ state: 'failed', code: 'authentication' });
        await verifyWorkspace(page, original, source, receiverText);
        console.log('PASS corrupted ciphertext is rejected both in preview and after a prior valid preview');
      } finally {
        writeFileSync(archivePath, valid);
      }

      const validPreview = await api(page, backupEndpoint + '/preview', 'POST', { password });
      await withWindowsPathLocked(library, async () => {
        const failed = await requestRestore(page, validPreview.restore.confirmation);
        expect(failed).toMatchObject({ state: 'failed', code: 'restore_write' });
        failedSwitch = failed.id;
        const persisted = JSON.parse(readFileSync(receiptFile, 'utf8'));
        expect(persisted).toMatchObject({
          id: failedSwitch, state: 'failed', code: 'interrupted', rollbackBackupId: expect.any(String),
        });
        expect(readdirSync(restoreWork).filter(name => name.startsWith('switch-'))).toEqual([]);
        const backups = await api(page, '/backups');
        expect(backups.items).toEqual(expect.arrayContaining([
          expect.objectContaining({ id: persisted.rollbackBackupId, pinned: true, status: 'ready' }),
        ]));
        const rollback = await api(page, '/backups/' + persisted.rollbackBackupId + '/preview', 'POST', { password });
        expect(rollback.files).toEqual(expect.arrayContaining([expect.objectContaining({ name: '迁移验收资料.txt' })]));
        await verifyWorkspace(page, original, source, receiverText);
        console.log('PASS an actual Windows sharing violation rolls back the data switch and keeps both live stores readable');
      });
    });

    let committed: Awaited<ReturnType<typeof requestRestore>> | undefined;
    await withApp(receiver, async page => {
      await expect(page.getByRole('heading', { name: home })).toBeVisible();
      await verifyWorkspace(page, original, source, receiverText);
      expect(await api(page, '/backups/restore-status'))
        .toMatchObject({ id: failedSwitch, state: 'failed', code: 'interrupted' });
      const preview = await api(page, backupEndpoint + '/preview', 'POST', { password });
      await withWindowsPathLocked(receiptFile, async () => {
        committed = await requestRestore(page, preview.restore.confirmation);
        expect(committed).toMatchObject({ state: 'complete', rollbackBackupId: expect.any(String) });
        const job = join(restoreWork, 'switch-' + committed.id);
        expect(JSON.parse(readFileSync(join(job, 'journal.json'), 'utf8')))
          .toMatchObject({ phase: 'committed', receipt: { id: committed.id, state: 'complete' } });
        expect(JSON.parse(readFileSync(receiptFile, 'utf8'))).toMatchObject({ id: failedSwitch, state: 'failed' });
        expect(existsSync(join(job, 'retained', 'zhixue.sqlite3'))).toBe(true);
        expect(existsSync(join(job, 'retained', 'library'))).toBe(true);
        await verifyWorkspace(page, source, original, originalText);
        console.log('PASS the installed app committed the incoming database and library, with the former receipt still locked');
        const rollbackEndpoint = '/backups/' + committed.rollbackBackupId;
        const second = await api(page, rollbackEndpoint + '/preview', 'POST', { password });
        expect(await requestRestore(page, second.restore.confirmation, rollbackEndpoint))
          .toMatchObject({ state: 'failed', code: 'previous_restore_pending' });
        expect(readdirSync(restoreWork).filter(name => name.startsWith('switch-'))).toEqual(['switch-' + committed.id]);
        await verifyWorkspace(page, source, original, originalText);
        console.log('PASS a second restore does not replace the committed pair while the previous receipt is locked');
      });
    }, 'terminate');

    expect(committed).toBeDefined();
    const committedId = committed!.id;
    expect(existsSync(join(restoreWork, 'switch-' + committedId, 'journal.json'))).toBe(true);
    await withApp(receiver, async page => {
      await expect(page.getByRole('heading', { name: home })).toBeVisible();
      expect(await api(page, '/backups/restore-status')).toMatchObject({
        id: committedId, state: 'complete', rollbackBackupId: committed!.rollbackBackupId,
      });
      expect(JSON.parse(readFileSync(receiptFile, 'utf8'))).toMatchObject({ id: committedId, state: 'complete' });
      expect(readdirSync(restoreWork).filter(name => name.startsWith('switch-'))).toEqual([]);
      await verifyWorkspace(page, source, original, originalText);
      const rollback = await api(page, '/backups/' + committed!.rollbackBackupId + '/preview', 'POST', { password });
      expect(rollback.files).toEqual(expect.arrayContaining([expect.objectContaining({ name: '迁移验收资料.txt' })]));
      console.log('PASS packaged restart preserves the committed pair, persists the success receipt, and removes the pending switch');
    });
  } finally {
    rmSync(root, { force: true, recursive: true, maxRetries: 5, retryDelay: 200 });
  }
});

test('the installed Electron app persists note currency, math, and linked source through restart', async () => {
  test.skip(!installed, 'Set ZHIXUE_TEST_ELECTRON to run against an installed app');
  const folder = realpathSync(mkdtempSync(join(tmpdir(), 'zhixue-installed-note-roundtrip-')));
  const amounts = '第一笔 $5，第二笔 $7。';
  const formula = '公式 $x^2 + y^2$。';
  let lesson: string;
  let saved: string;
  try {
    await withApp(folder, async page => {
      await page.getByRole('button', { name: '使用此位置', exact: true }).click();
      await page.getByRole('button', { name: '稍后配置模型', exact: true }).click();
      await expect(page.getByRole('heading', { name: home })).toBeVisible();
      const id = randomUUID();
      const project = await api(page, '/projects', 'POST', { id, topic: 'SQLite', outcome: '安装版笔记往返' });
      lesson = project.lessons[0].id;
      await expect.poll(async () => (await api(page, '/projects/' + id)).task.status).toBe('ready');
      const file = await api(page, '/files', 'POST', { name: '候选查询卡.txt', content: '本次安装版的独立来源。' });
      await page.evaluate(path => { location.hash = path; }, '/lessons/' + lesson);
      await page.getByRole('button', { name: '笔记', exact: true }).click();
      const panel = page.locator('.reading-side .note-panel');
      const editor = panel.getByRole('textbox', { name: '本节笔记' });
      const save = panel.getByRole('button', { name: '保存笔记', exact: true });
      const raw = panel.getByRole('button', { name: 'Markdown 原文' });
      const path = join(folder, 'library', 'notes', lesson + '.md');
      await raw.click();
      await editor.fill('# 安装版笔记\n\n' + formula);
      await save.click();
      await expect(panel.getByText('笔记已保存')).toBeVisible();
      await panel.getByRole('button', { name: '富文本', exact: true }).click();
      await expect(editor.locator('[data-type="inline-math"] annotation')).toHaveText('x^2 + y^2');
      await editor.getByRole('heading', { name: '安装版笔记' }).evaluate(element => {
        element.closest<HTMLElement>('[contenteditable="true"]')?.focus();
        window.getSelection()?.collapse(element, element.childNodes.length);
      });
      await page.keyboard.press('Enter');
      await page.keyboard.insertText(amounts);
      await expect(editor.getByText(amounts, { exact: true })).toBeVisible();
      await save.click();
      await expect.poll(() => readFileSync(path, 'utf8')).toContain('第一笔 \\$5，第二笔 \\$7。');
      await expect(panel.getByText('笔记已保存')).toBeVisible();
      console.log('PASS installed service saves literal dollars separately from the existing formula');

      await raw.click();
      const written = await editor.inputValue();
      const partial = written + '\n\n见 [[候选查询';
      const suffix = '\n\n保留的后续内容。';
      await editor.fill(partial + suffix);
      await editor.evaluate((field, offset) => {
        (field as HTMLTextAreaElement).setSelectionRange(offset, offset);
      }, partial.length);
      await editor.press('Shift');
      const option = panel.getByRole('option', { name: /候选查询卡\.txt/ });
      await expect(option).toHaveAttribute('aria-selected', 'true');
      await editor.press('Tab');
      const completed = written + '\n\n见 [[' + file.id + '|候选查询卡.txt]]';
      saved = completed + suffix;
      await expect(editor).toHaveValue(saved);
      expect(await editor.evaluate(field => (field as HTMLTextAreaElement).selectionStart)).toBe(completed.length);
      await save.click();
      await expect(panel.getByText('笔记已保存')).toBeVisible();
      expect(readFileSync(path, 'utf8')).toBe(saved);
      console.log('PASS installed source editor completes a wiki link without replacing its following text');
    });

    await withApp(folder, async page => {
      await expect(page.getByRole('heading', { name: home })).toBeVisible();
      await page.evaluate(path => { location.hash = path; }, '/lessons/' + lesson);
      await page.getByRole('button', { name: '笔记', exact: true }).click();
      const panel = page.locator('.reading-side .note-panel');
      const editor = panel.getByRole('textbox', { name: '本节笔记' });
      await expect(editor.getByText(amounts, { exact: true })).toBeVisible();
      await expect(editor.locator('[data-type="inline-math"]')).toHaveCount(1);
      await expect(editor.getByRole('link', { name: '[[候选查询卡.txt]]' })).toBeVisible();
      await panel.getByRole('button', { name: 'Markdown 原文' }).click();
      await expect(editor).toHaveValue(saved);
      console.log('PASS restarted installer reads back currency, the original formula, the resolved link, and trailing text');
    });
  } finally {
    rmSync(folder, { force: true, recursive: true, maxRetries: 5, retryDelay: 200 });
  }
});

test('the installed Electron app locates mixed-script search hits and recovers from an invalid anchor', async () => {
  test.skip(!installed, 'Set ZHIXUE_TEST_ELECTRON to run against an installed app');
  const temp = realpathSync(tmpdir());
  const folder = realpathSync(mkdtempSync(join(temp, 'zhixue-installed-library-search-')));
  expect(dirname(folder)).toBe(temp);
  const filename = '安装检索验收.txt';
  const passage = 'Straße '.repeat(90) + '现在记录中文复习计划和重点。' + ' 后续正文'.repeat(48);
  const original = '合成原文说明。\n\n' + passage;
  const replacement = '合成原文说明。\n更新后核对阅读计划。';
  try {
    await withApp(folder, async page => {
      await page.getByRole('button', { name: '使用此位置', exact: true }).click();
      await page.getByRole('button', { name: '稍后配置模型', exact: true }).click();
      await expect(page.getByRole('heading', { name: home })).toBeVisible();
      const { id } = await api(page, '/files', 'POST', { name: filename, content: original });
      const endpoint = '/files/' + id;
      const file = await api(page, endpoint);
      expect(file.content).toBe(original);
      await page.getByRole('link', { name: '知识库', exact: true }).click();
      const search = page.getByRole('searchbox', { name: '搜索知识库' });
      for (const query of ['复', '复习', '复习计划']) {
        await search.fill(query);
        const hit = page.getByRole('button', { name: /安装检索验收\.txt.*第 3 行/ });
        await expect.soft(hit.locator('.hit-excerpt')).toContainText('中文复习计划');
        await hit.click();
        await expect(page.locator('.file-anchor')).toContainText(passage);
      }

      const source = realpathSync(resolve(folder, file.path));
      expect(relative(join(folder, 'library'), source).startsWith('..')).toBe(false);
      expect(readFileSync(source, 'utf8')).toBe(original);
      writeFileSync(source, replacement, 'utf8');
      await api(page, '/files/reindex', 'POST');
      expect(readFileSync(source, 'utf8')).toBe(replacement);
      const stale = await wire(page, endpoint + '?at=3&revision=' + file.revision);
      expect(stale.status()).toBe(409);
      const unavailable = await wire(page, endpoint + '?at=3');
      expect.soft(unavailable.status()).toBe(404);
      await page.evaluate(id => { location.hash = '/library?file=' + id + '&at=3'; }, id);
      const preview = page.locator('.library-preview');
      await expect(preview.getByRole('alert')).toContainText('找不到文件中的这一行或段落');
      await preview.getByRole('button', { name: '查看当前版本' }).click();
      await expect(preview.locator('.plain-file')).toContainText('更新后核对阅读计划。');
      await search.fill('核对阅读计划');
      await page.getByRole('button', { name: /安装检索验收\.txt.*第 2 行/ }).click();
      await expect(preview.locator('.file-anchor')).toContainText('更新后核对阅读计划。');
      console.log('PASS installed service returns original snippets and recovers invalid anchors after reindex');
    });
  } finally {
    rmSync(folder, { force: true, recursive: true, maxRetries: 5, retryDelay: 200 });
  }
});

test('the installed Electron app preserves wiki search context and original location', async ({}, info) => {
  test.skip(!installed, 'Set ZHIXUE_TEST_ELECTRON to run against an installed app');
  const temp = realpathSync(tmpdir());
  const folder = realpathSync(mkdtempSync(join(temp, 'zhixue-installed-wiki-')));
  expect(dirname(folder)).toBe(temp);
  const q = '安装检索续接样本';
  const conditions = { q, scope: 'all', folder: 'library/imports', format: 'md' };
  const parse = (page: Page) => new URLSearchParams(new URL(page.url()).hash.split('?')[1]);
  const navigate = (page: Page, hash: string) => page.evaluate(value => { location.hash = value; }, hash);
  async function checkSearch(page: Page) {
    await expect(page.getByRole('searchbox', { name: '搜索知识库' })).toHaveValue(q);
    for (const [key, value] of Object.entries(conditions)) expect(parse(page).get(key)).toBe(value);
    await expect(page.locator('.library-list .library-file strong')).toHaveText(['安装验收原文.md']);
  }
  const targetText = '目标文件的第三行。';
  let savedHash: string;
  let targetVersion: string;
  let targetId: string;

  async function checkTarget(page: Page) {
    await expect(page.locator('.library-preview .file-anchor')).toContainText(targetText);
    await checkSearch(page);
    expect(parse(page).get('file')).toBe(targetId);
    expect(parse(page).get('at')).toBe('3');
    expect(parse(page).get('revision')).toBe(targetVersion);
  }

  try {
    await withApp(folder, async page => {
      await page.getByRole('button', { name: '使用此位置', exact: true }).click();
      await page.getByRole('button', { name: '稍后配置模型', exact: true }).click();
      await expect(page.getByRole('heading', { name: home })).toBeVisible();
      const target = await api(page, '/files', 'POST', { name: '安装参考入口.md', content: '# 安装参考段落\n\n' + targetText });
      targetId = target.id;
      targetVersion = (await api(page, '/files/' + targetId)).revision;
      expect(targetVersion).toMatch(/^[a-f0-9]{64}$/);
      const choice = await api(page, '/files', 'POST', { name: '同名安装资料.md', content: '# 甲版安装资料' });
      await api(page, '/files', 'POST', { name: '同名安装资料.md', content: '# 乙版安装资料' });
      const link = '#/library?' + new URLSearchParams({ file: targetId, at: '3', revision: targetVersion });
      const source = await api(page, '/files', 'POST', {
        name: '安装验收原文.md',
        content: '# 安装验收原文\n\n' + q + ' [[安装参考入口]]、[[同名安装资料]]，再读[指定原文](' + link + ')。',
      });

      await navigate(page, '#/library?' + new URLSearchParams({ folder: conditions.folder }));
      await page.getByRole('combobox', { name: '搜索范围' }).selectOption('all');
      await page.getByRole('combobox', { name: '文件类型' }).selectOption('md');
      await page.getByRole('searchbox', { name: '搜索知识库' }).fill(q);
      const preview = page.locator('.library-preview');
      const anchor = preview.locator('.file-anchor');
      await page.getByRole('button', { name: /安装验收原文\.md.*第 3 行/ }).click();
      await expect(anchor).toContainText(q);
      const sourceVersion = parse(page).get('revision');
      expect(sourceVersion).toMatch(/^[a-f0-9]{64}$/);
      await preview.getByRole('link', { name: '[[安装参考入口]]', exact: true }).click();
      await expect(preview.getByRole('heading', { name: '安装参考段落' })).toBeVisible();
      await checkSearch(page);
      expect(parse(page).get('file')).toBe(targetId);
      expect(parse(page).get('at')).toBeNull();
      expect(parse(page).get('revision')).toBeNull();

      await preview.getByText('反向链接', { exact: true }).click();
      await preview.getByRole('link', { name: '安装验收原文.md · 第 3 行' }).click();
      await expect(anchor).toContainText(q);
      await checkSearch(page);
      expect(parse(page).get('file')).toBe(source.id);
      expect(parse(page).get('at')).toBe('3');
      expect(parse(page).get('revision')).toBe(sourceVersion);

      await preview.getByRole('link', { name: '[[同名安装资料]]', exact: true }).click();
      await expect(preview.getByText('有多个文件匹配「同名安装资料」。请选择要打开的文件。')).toBeVisible();
      await checkSearch(page);
      await preview.getByRole('link', { name: new RegExp(choice.id) }).click();
      await expect(preview.getByRole('heading', { name: '甲版安装资料', exact: true })).toBeVisible();
      await checkSearch(page);

      await page.goBack();
      await preview.getByRole('link', { name: '指定原文', exact: true }).click();
      await checkTarget(page);
      await page.reload();
      await checkTarget(page);
      savedHash = new URL(page.url()).hash;
    });

    await withApp(folder, async page => {
      await expect(page.getByRole('heading', { name: home })).toBeVisible();
      await navigate(page, savedHash);
      await checkTarget(page);
      await page.screenshot({ path: info.outputPath('安装版双链检索续接.png') });
    });
  } finally {
    rmSync(folder, { force: true, recursive: true, maxRetries: 5, retryDelay: 200 });
  }
});

test('the installed Electron app retains one overdue review with its grading delay after restart', async ({}, info) => {
  test.skip(!installed, 'Set ZHIXUE_TEST_ELECTRON to run against an installed app');
  const temp = realpathSync(tmpdir());
  const folder = realpathSync(mkdtempSync(join(temp, 'zhixue-installed-late-grade-')));
  expect(dirname(folder)).toBe(temp);
  type PlannedReview = { lessonId: string; key: string; kind: string; dueOn: string };
  let expected: Omit<PlannedReview, 'kind'> & {
    title: string; lessonTitle: string; submittedOn: string; scoredOn: string; groupId: string;
  };
  async function verifyReview(page: Page) {
    await page.getByRole('link', { name: '今日复习', exact: true }).click();
    const row = page.locator('.review-row').filter({ has: page.getByText(expected.title, { exact: true }) });
    await expect(row).toHaveCount(1);
    await expect(row.getByRole('link', { name: '开始复习：' + expected.title })).toBeVisible();
    await expect(row).toContainText(expected.dueOn.replaceAll('-', '.'));
    await expect(row.getByText('评分延迟', { exact: true })).toBeVisible();
    const plan: { days: { tasks: PlannedReview[] }[] } = await api(page, '/study-plan');
    const matching = plan.days.flatMap(day => day.tasks)
      .filter(task => task.kind === 'review' && task.lessonId === expected.lessonId && task.key === expected.key);
    expect(matching.map(task => task.dueOn)).toEqual([expected.dueOn]);
  }
  async function verifyFormalReport(page: Page) {
    const query = 'period=day&on=' + expected.submittedOn;
    const report = await api(page, '/study-report?' + query);
    expect(report.totals.review).toEqual({ passed: 1, hinted: 0, failed: 0, ungraded: 0 });
    expect(report.reviewRecords).toHaveLength(1);
    expect(report.reviewRecords[0]).toMatchObject({
      lessonId: expected.lessonId, key: expected.key, on: expected.submittedOn, outcome: 'passed',
      round: 1, groupId: expected.groupId, title: expected.title,
    });
    const scoreDay = await api(page, '/study-report?period=day&on=' + expected.scoredOn);
    expect(scoreDay.totals.review).toEqual({ passed: 0, hinted: 0, failed: 0, ungraded: 0 });
    expect(scoreDay.reviewRecords).toEqual([]);

    await page.evaluate(query => { location.hash = '/study-report?' + query; }, query);
    await page.getByRole('button', { name: '查看独立通过的正式复习记录', exact: true }).click();
    const detail = page.getByRole('region', { name: '正式复习记录', exact: true });
    await expect(detail.getByRole('combobox', { name: '复习结果' })).toHaveValue('passed');
    const row = detail.getByRole('listitem');
    await expect(row).toHaveCount(1);
    await expect(row.getByText(expected.title, { exact: true })).toBeVisible();
    await expect(row.getByRole('link', { name: expected.lessonTitle, exact: true }))
      .toHaveAttribute('href', '#/lessons/' + expected.lessonId);
  }
  try {
    await withApp(folder, async page => {
      await page.getByRole('button', { name: '使用此位置', exact: true }).click();
      await page.getByRole('button', { name: '稍后配置模型', exact: true }).click();
      await expect(page.getByRole('heading', { name: home })).toBeVisible();
      const origin = new URL(page.url()).origin;
      const token = await page.evaluate(() => sessionStorage.getItem('zhixue-session'));
      expect(token).toBeTruthy();
      const output = execFileSync(python, ['-c', `
from datetime import date, timedelta
import json
from pathlib import Path
import sys

from acceptance.product import Product, until
from acceptance.scenarios import ANSWERS, learning_route, verify_course
from acceptance.server import Run, RunStore
from acceptance.simulation import MAIN, REVIEWER, local_models

origin, token, report = json.load(sys.stdin)
api = Product(origin, token)
run = Run(RunStore(Path(report)), None)
with local_models() as (models, address):
    roles = {}
    for role, model in (("main", MAIN), ("review", REVIEWER)):
        connection = api.post("/api/models/connections", {
            "name": f"安装版验收 {role}", "provider": "custom", "protocol": "chat-completions",
            "model": model, "endpoint": address,
        })
        roles[role] = connection["id"]
    api.request("PATCH", "/api/models/roles", roles)
    project, lesson = learning_route(run, api)
    verify_course(run, api, models, project, lesson)
    api.put(lesson + "/learned", {"learned": True})
    api.authorize(lesson + "/assessment", {"maxCalls": 3})
    first = until(lambda: api.get("/api/reviews")["reviews"], "未准备第一次复习。", timeout=30)
    due, = first
    api.post("/api/demo/next-day")
    api.post("/api/demo/next-day")
    objective = api.get(lesson + "/assessments/" + due["key"])
    path = "/api/assessments/" + objective["group"]["id"]
    with models.hold("grade") as pending:
        submitted = api.post(path + "/submit", {"action": "submit", "answers": ANSWERS})
        assert submitted["state"] == "grading" and pending.arrived.wait(14)
        assert api.get("/api/reviews")["reviews"][0]["status"] == "waiting"
        for _ in range(3):
            api.post("/api/demo/next-day")
    result = until(lambda: api.get(path)["result"], "未保存延迟评分。", timeout=30)
    submitted = date.fromisoformat(result["submittedOn"])
    assert submitted > date.fromisoformat(due["dueOn"])
    expected = (submitted + timedelta(days=2)).isoformat()
    assert result["outcome"] == "passed" and result["gradingDelayed"] and result["dueOn"] == expected
    assert result["scoredOn"] > expected
    print(json.dumps({"dueOn": expected, "title": due["title"], "lessonId": due["lessonId"],
                      "key": due["key"], "lessonTitle": next(item["title"] for item in api.get(project)["lessons"]
                                          if item["id"] == due["lessonId"]),
                      "submittedOn": result["submittedOn"], "scoredOn": result["scoredOn"],
                      "groupId": objective["group"]["id"]}, ensure_ascii=False))
`], { input: JSON.stringify([origin, token, info.outputPath('fixture')]), encoding: 'utf8', windowsHide: true,
        timeout: 150000, env: { ...process.env, PYTHONUTF8: '1', NO_PROXY: '127.0.0.1' } });
      expected = JSON.parse(output);
      await verifyReview(page);
      await verifyFormalReport(page);
    });
    await withApp(folder, async page => {
      await expect(page.getByRole('heading', { name: home })).toBeVisible();
      await verifyReview(page);
      await test.step('reads formal review details on the original submission day after restart', async () => {
        await verifyFormalReport(page);
        await page.getByRole('region', { name: '正式复习记录', exact: true })
          .screenshot({ path: info.outputPath('安装版正式复习明细.png') });
      });
      console.log('PASS restarted installed service shows one delayed review on the date derived from its actual submission');
    });
  } finally {
    rmSync(folder, { force: true, recursive: true, maxRetries: 5, retryDelay: 200 });
  }
});

test('the installed Electron app enforces single-page recognition authorization', async () => {
  test.skip(!installed, 'Set ZHIXUE_TEST_ELECTRON to run against an installed app');
  const pdf = execFileSync(python, ['-c', [
    'import pymupdf, sys',
    'with pymupdf.open() as document:',
    '    for fill in [(0, 0, 1), (1, 0, 0)]:',
    '        page = document.new_page(width=250, height=150)',
    '        page.draw_rect(page.rect, color=None, fill=fill)',
    '    sys.stdout.buffer.write(document.tobytes())',
  ].join('\n')], { windowsHide: true });
  const parent = realpathSync(tmpdir());
  const folder = realpathSync(mkdtempSync(join(parent, 'zhixue-installed-recognition-')));
  expect(dirname(folder)).toBe(parent);
  const filename = '安装版外部识别扫描.pdf';
  const corrected = '确认后才能搜索的安装版页图文字';
  const calls: { route?: string; type?: string; body: Buffer }[] = [];
  const provider = createServer(async (request, response) => {
    const chunks: Buffer[] = [];
    for await (const chunk of request) chunks.push(Buffer.from(chunk));
    calls.push({ route: request.url, type: request.headers['content-type'], body: Buffer.concat(chunks) });
    response.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify({
      choices: [{ message: { content: '隔离接收端返回的页图草稿' }, finish_reason: 'stop' }],
      usage: { prompt_tokens: 20, completion_tokens: 7 },
    }));
  });
  try {
    provider.listen(0, '127.0.0.1');
    await once(provider, 'listening');
    const bound = provider.address();
    if (!bound || typeof bound === 'string') throw new Error('Expected a loopback port');
    const origin = 'http://127.0.0.1:' + bound.port;
    const initial = { name: '安装版页图接收方', protocol: 'chat-completions', provider: 'custom',
      endpoint: origin + '/initial/v1', model: 'installed-vision' };
    const replacement = { ...initial, endpoint: origin + '/replacement/v1', model: 'replacement-vision' };
    const grants = '/recognition/ocr';
    const editor = (page: Page) => page.getByRole('region', { name: '识别与校对' });
    const send = (panel: Locator) => panel.getByRole('button', { name: /^发送所选页图/ });
    const draft = (panel: Locator) => panel.getByRole('textbox', { name: '校对文字' });
    let fileId: string;
    let revision: string;
    let connection: { id: string; revision: number };
    let approval: { id: string };
    let voiceApproval: { id: string };
    let pageImage: Buffer;

    async function authorize(panel: Locator, destination: typeof initial) {
      const count = calls.length;
      await panel.getByRole('button', { name: '选择外部识别服务' }).click();
      await panel.getByLabel('识别服务', { exact: true }).selectOption(connection.id);
      await panel.getByRole('button', { name: '预览发送范围' }).click();
      await expect(panel.getByText(destination.endpoint + '/chat/completions', { exact: true })).toBeVisible();
      await expect(panel.getByText(destination.model, { exact: true })).toBeVisible();
      await expect(panel.getByText(/单页 PNG 图像/)).toBeVisible();
      await expect(panel.getByText(/PDF 第 2 页/)).toBeVisible();
      await panel.getByRole('button', { name: '保存授权' }).click();
      await expect(send(panel)).toBeEnabled();
      expect(calls).toHaveLength(count);
    }

    function verifyCall(index: number, destination: typeof initial) {
      expect(calls).toHaveLength(index + 1);
      const request = calls[index];
      expect(request.route).toBe(new URL(destination.endpoint).pathname + '/chat/completions');
      expect(request.type).toBe('application/json');
      expect(JSON.parse(request.body.toString())).toEqual({
        model: destination.model, max_tokens: expect.any(Number),
        messages: [{ role: 'user', content: [
          { type: 'text', text: expect.any(String) },
          { type: 'image_url', image_url: { url: 'data:image/png;base64,' + pageImage.toString('base64') } },
        ] }],
      });
    }

    await withApp(folder, async page => {
      await page.getByRole('button', { name: '使用此位置', exact: true }).click();
      await page.getByRole('button', { name: '稍后配置模型', exact: true }).click();
      await expect(page.getByRole('heading', { name: home })).toBeVisible();
      connection = await api(page, '/models/connections', 'POST', initial);
      const voice = await api(page, '/models/connections', 'POST', {
        ...initial, name: '仅用于语音范围对照', endpoint: origin + '/voice/v1', model: 'voice-only',
      });
      const { confirmation } = await api(page, '/recognition/voice/preview', 'POST', { connectionId: voice.id });
      voiceApproval = (await api(page, '/recognition/voice/authorize', 'POST', {
        connectionId: voice.id, confirmation,
      })).authorization;
      await page.getByRole('link', { name: '知识库', exact: true }).click();
      await page.getByLabel('导入资料').setInputFiles({ name: filename, mimeType: 'application/pdf', buffer: pdf });
      await expect(page.getByText('第 1、2 页无可提取文字')).toBeVisible();
      fileId = new URLSearchParams(new URL(page.url()).hash.split('?')[1]).get('file')!;
      expect(fileId).toBeTruthy();
      const path = '/files/' + fileId;
      revision = (await api(page, path)).revision;
      const first = await wire(page, path + '/pages/1/image');
      const second = await wire(page, path + '/pages/2/image');
      expect(first.ok() && second.ok()).toBe(true);
      pageImage = await second.body();
      expect(pageImage.equals(await first.body())).toBe(false);
      const crossUse = await wire(page, path + '/pages/2/ocr/external', 'POST', {
        revision, authorizationId: voiceApproval.id,
      });
      expect(crossUse.status()).toBe(409);
      expect(calls).toHaveLength(0);
      await page.getByRole('button', { name: '下一页' }).click();
      const panel = editor(page);
      await authorize(panel, initial);
      approval = (await api(page, grants)).authorization;
      await send(panel).click();
      await expect(draft(panel)).toHaveValue('隔离接收端返回的页图草稿');
      verifyCall(0, initial);
      expect((await api(page, '/files/search?q=' + encodeURIComponent('隔离接收端返回'))).hits).toEqual([]);
      await draft(panel).fill(corrected);
      await panel.getByRole('button', { name: '保存校对文字' }).click();
      await expect(page.locator('.file-anchor')).toContainText(corrected);
      console.log('PASS authorization and explicit send deliver only the selected page, and only reviewed text is saved');
    });
    await withApp(folder, async page => {
      await expect(page.getByRole('heading', { name: home })).toBeVisible();
      expect((await api(page, grants)).authorization).toMatchObject(approval);
      const path = '/files/' + fileId;
      const endpoint = path + '/pages/2/ocr/external';
      expect((await api(page, path + '?at=2')).anchor.text).toBe(corrected);
      const original = await wire(page, path + '/original');
      expect(await original.body()).toEqual(pdf);

      connection = await api(page, '/models/connections/' + connection.id, 'PUT', {
        ...replacement, revision: connection.revision,
      });
      expect((await api(page, grants)).authorization).toBeNull();
      expect((await wire(page, endpoint, 'POST', { revision, authorizationId: approval.id })).status()).toBe(409);
      expect(calls).toHaveLength(1);
      await page.evaluate(id => { location.hash = '/library?file=' + id + '&at=2'; }, fileId);
      const panel = editor(page);
      await authorize(panel, replacement);
      const next = (await api(page, grants)).authorization;
      expect(next.id).not.toBe(approval.id);
      await send(panel).click();
      await expect(draft(panel)).toHaveValue('隔离接收端返回的页图草稿');
      verifyCall(1, replacement);

      await panel.getByRole('button', { name: '撤销授权' }).click();
      await expect(send(panel)).toHaveCount(0);
      expect((await api(page, grants)).authorization).toBeNull();
      expect((await wire(page, endpoint, 'POST', { revision, authorizationId: next.id })).status()).toBe(409);
      expect((await api(page, '/recognition/voice')).authorization).toMatchObject(voiceApproval);
      expect((await api(page, path + '?at=2')).anchor.text).toBe(corrected);
      expect(calls).toHaveLength(2);
      console.log('PASS restart preserves consent; recipient change and revocation block stale uploads without touching voice consent');
    });
  } finally {
    provider.closeAllConnections();
    if (provider.listening) await new Promise<void>(done => provider.close(() => done()));
    rmSync(folder, { force: true, recursive: true, maxRetries: 5, retryDelay: 200 });
  }
});

test('the installed Electron app protects a concurrent OCR correction and its source file', async () => {
  test.skip(!installed, 'Set ZHIXUE_TEST_ELECTRON to run against an installed app');
  const temp = realpathSync(tmpdir());
  const folder = realpathSync(mkdtempSync(join(temp, 'zhixue-installed-ocr-')));
  expect(dirname(folder)).toBe(temp);
  const filename = '安装版扫描件.pdf';
  const kept = '另一窗口确认的原文 937';
  const draftText = '旧草稿待处理 618';
  const corrected = '重新核对后的扫描文字 249';
  const later = '翻页后才返回的扫描校对 367';
  const pdf = execFileSync(python, ['-c', [
    'import pymupdf, sys',
    'with pymupdf.open() as text, pymupdf.open() as scan:',
    '    page = text.new_page(width=560, height=210)',
    '    page.insert_text((35, 65), "Installed Scan 2026", fontsize=32)',
    '    page.insert_text((35, 140), "中文学习材料", fontsize=34, fontname="china-s")',
    '    scan.new_page(width=560, height=210).insert_image(page.rect, pixmap=page.get_pixmap(dpi=200))',
    '    scan.new_page(width=560, height=210)',
    '    sys.stdout.buffer.write(scan.tobytes(garbage=4, deflate=True))',
  ].join('\n')], { windowsHide: true });
  let id: string;
  let textRevision: string;
  const search = (query: string) => '/files/search?' + new URLSearchParams({ q: query, format: 'pdf', mode: 'keyword' });
  try {
    await withApp(folder, async page => {
      await page.getByRole('button', { name: '使用此位置', exact: true }).click();
      await page.getByRole('button', { name: '稍后配置模型', exact: true }).click();
      await expect(page.getByRole('heading', { name: home })).toBeVisible();
      await page.getByRole('link', { name: '知识库', exact: true }).click();
      await page.getByLabel('导入资料').setInputFiles({ name: filename, mimeType: 'application/pdf', buffer: pdf });
      const preview = page.locator('.library-preview');
      await expect(preview.getByText('第 1、2 页无可提取文字')).toBeVisible();
      const file = new URLSearchParams(new URL(page.url()).hash.split('?')[1]).get('file');
      expect(file).toBeTruthy();
      id = file!;
      const path = '/files/' + id;
      const ocr = path + '/pages/1/ocr';
      const original = await api(page, path);
      const editor = preview.getByRole('region', { name: '识别与校对' });
      await editor.getByRole('button', { name: '识别本页' }).click();
      const draft = editor.getByRole('textbox', { name: '校对文字' });
      await expect(draft).toHaveValue(/Installed Scan 2026/, { timeout: 12000 });
      await expect(draft).toHaveValue(/中文学习材料/);
      expect((await api(page, search('Installed Scan'))).hits).toEqual([]);
      await draft.fill(draftText);

      const correctionFromOtherWindow = await test.step('keeps a competing OCR correction and the unchanged original', async () => {
        const other = await api(page, ocr, 'PUT', { revision: original.revision, textRevision: null, text: kept });
        await preview.getByRole('button', { name: '收藏文件' }).click();
        await expect(preview.locator('.file-anchor')).toContainText(kept);
        await expect(draft).toHaveValue(draftText);
        const rejected = page.waitForResponse(response => new URL(response.url()).pathname === '/api' + ocr
          && response.request().method() === 'PUT');
        await editor.getByRole('button', { name: '保存校对文字' }).click();
        expect((await rejected).status()).toBe(409);
        await expect(editor.getByRole('alert')).toContainText('识别文本已在其他窗口保存');
        await expect(draft).toHaveValue(draftText);
        const saved = await api(page, path + '?at=1');
        expect(saved.anchor).toMatchObject({ text: kept, textRevision: other.textRevision });
        expect((await api(page, search(draftText))).hits).toEqual([]);
        const originalBytes = await wire(page, path + '/original');
        expect(originalBytes.ok()).toBe(true);
        expect(await originalBytes.body()).toEqual(pdf);
        return other.textRevision;
      });

      async function saveWhile(navigate: () => Promise<void>) {
        const target = (url: URL) => url.pathname === '/api' + ocr;
        const { promise: started, resolve: markStarted } = Promise.withResolvers<void>();
        const { promise: released, resolve: release } = Promise.withResolvers<void>();
        await page.route(target, async route => {
          if (route.request().method() !== 'PUT') return route.fallback();
          markStarted();
          await released;
          await route.continue();
        });
        const response = page.waitForResponse(reply => target(new URL(reply.url())) && reply.request().method() === 'PUT');
        try {
          await editor.getByRole('button', { name: '保存校对文字' }).click();
          await started;
          await navigate();
        } finally {
          release();
        }
        expect((await response).status()).toBe(200);
        return () => page.unroute(target);
      }

      const box = page.getByRole('searchbox', { name: '搜索知识库' });
      const scope = page.getByRole('combobox', { name: '搜索范围' });
      const hit = page.getByRole('button', { name: /安装版扫描件\.pdf.*第 1 页 · 已校对 OCR/ });
      const earlierRevision = await test.step('preserves the active search while saving the correction', async () => {
        await editor.getByRole('button', { name: '取消', exact: true }).click();
        await editor.getByRole('button', { name: '编辑校对文字' }).click();
        await expect(draft).toHaveValue(kept);
        await draft.fill(corrected);
        const releaseRoute = await saveWhile(async () => {
          await box.fill(corrected);
          await scope.selectOption('all');
          await expect.poll(() => page.evaluate(() => {
            const current = new URLSearchParams(location.hash.split('?')[1]);
            return [current.get('q'), current.get('scope')];
          })).toEqual([corrected, 'all']);
        });
        await expect(preview.locator('.file-anchor')).toContainText(corrected);
        await expect(box).toHaveValue(corrected);
        await expect(scope).toHaveValue('all');
        await expect(hit).toBeVisible();
        await releaseRoute();
        const current = await api(page, path + '?at=1');
        expect(current.anchor.text).toBe(corrected);
        expect(current.anchor.textRevision).not.toBe(correctionFromOtherWindow);
        return current.anchor.textRevision;
      });

      await test.step('keeps the chosen page when a pending OCR save finishes', async () => {
        await box.fill(later);
        await expect(page.getByText('没有匹配的内容')).toBeVisible();
        await editor.getByRole('button', { name: '编辑校对文字' }).click();
        await draft.fill(later);
        const second = page.getByRole('img', { name: 'PDF 原件第 2 页' });
        const releaseRoute = await saveWhile(async () => {
          await preview.getByRole('button', { name: '下一页' }).click();
          await expect(second).toBeVisible();
        });
        const saved = await api(page, path + '?at=1');
        expect(saved.anchor.text).toBe(later);
        expect((await api(page, search(later))).hits).toEqual([expect.objectContaining({
          id, location: expect.objectContaining({ number: 1, textRevision: saved.anchor.textRevision }),
        })]);
        await expect(hit).toBeVisible();
        await expect(second).toBeVisible();
        await releaseRoute();
        await expect(box).toHaveValue(later);
        await hit.click();
        await expect(preview.getByRole('img', { name: 'PDF 原件第 1 页' })).toBeVisible();
        await expect(preview.locator('.file-anchor')).toContainText(later);
        const confirmed = await api(page, path + '?at=1');
        textRevision = confirmed.anchor.textRevision;
        expect(textRevision).not.toBe(earlierRevision);
        expect(new URLSearchParams(new URL(page.url()).hash.split('?')[1]).get('revision')).toBe(textRevision);
      });
    });
    await test.step('reopens the corrected page and original after restart', async () => {
      await withApp(folder, async page => {
        await expect(page.getByRole('heading', { name: home })).toBeVisible();
        await page.getByRole('link', { name: '知识库', exact: true }).click();
        await page.getByRole('searchbox', { name: '搜索知识库' }).fill(later);
        await page.getByRole('button', { name: /安装版扫描件\.pdf.*第 1 页 · 已校对 OCR/ }).click();
        const preview = page.locator('.library-preview');
        await expect(preview.locator('.file-anchor')).toContainText(later);
        await expect(preview.getByText('第 2 页无可提取文字')).toBeVisible();
        const reopened = await api(page, '/files/' + id + '?at=1');
        expect(reopened.anchor).toMatchObject({ text: later, textRevision });
        const originalBytes = await wire(page, '/files/' + id + '/original');
        expect(originalBytes.ok()).toBe(true);
        expect(await originalBytes.body()).toEqual(pdf);
      });
    });
  } finally {
    rmSync(folder, { force: true, recursive: true, maxRetries: 5, retryDelay: 200 });
  }
});

test('the installed Electron app recalibrates and replays an offline timer after restart', async ({}, info) => {
  test.skip(!installed, 'Set ZHIXUE_TEST_ELECTRON to run against an installed app');
  type Ledger = Awaited<ReturnType<typeof import('../src/mobile-timer').timerLedger>>;
  type TimerEvent = import('../src/mobile-timer').PhoneTimerEvent;
  type Receipt = { id: string; state: 'held' | 'saved' | 'resolved'; seconds: number; reason?: string };
  type Trace = { clockReads: number; sent: { clockReads: number; event: TimerEvent }[]; pause?: Receipt };
  const parent = realpathSync(tmpdir());
  const folder = realpathSync(mkdtempSync(join(parent, 'zhixue-installed-timer-clock-')));
  const database = join(folder, 'zhixue.sqlite3');
  const offlineDatabase = join(folder, 'profile', 'offline-study.sqlite3');
  const delayMs = 2350;
  const button = (page: Page, name: string) => page.getByRole('button', { name, exact: true });
  const timer = (page: Page) => page.getByRole('region', { name: '本机计时', exact: true });
  const queue = (ledger: Ledger) => ledger.events.map(event => event.action);
  let timerId = '';
  let workspace: string;
  let expectedIds: string[];
  let pendingAfterLoss: Ledger;
  let pauseSeconds: number;
  let pausedMilliseconds: number;

  function ledger(page: Page): Promise<Ledger> {
    return page.evaluate(async () => (await window.zhixueDesktop!.offline!.read('timers', 'ledger')).value as Ledger ??
      { events: [], clocks: {} });
  }

  function receipts() {
    expect(timerId).toMatch(/^[0-9a-f-]{36}$/);
    return JSON.parse(readSqlite(database, `
      SELECT json_group_array(json(item)) FROM (
        SELECT json_object('action', json_extract(details, '$.action'), 'reply', json(reply)) AS item
        FROM mobile_changes WHERE kind = 'timer' AND json_extract(details, '$.timerId') = '${timerId}'
        ORDER BY received_at, id
      )`)) as { action: TimerEvent['action']; reply: Receipt }[];
  }

  async function reportedSeconds(page: Page) {
    const report = await api(page, '/study-report?period=month');
    return (report.timeRecords as { timerId: string; category: string; seconds: number }[])
      .filter(entry => entry.timerId === timerId && entry.category === 'notes')
      .reduce((seconds, entry) => seconds + entry.seconds, 0);
  }

  function duration(event: TimerEvent) {
    expect(event.span?.reliable).toBe(true);
    const { beganAt, endedAt } = event.span!;
    return Math.floor(Date.parse(endedAt) / 1000) - Math.floor(Date.parse(beganAt) / 1000);
  }

  async function synchronize(page: Page) {
    await button(page, '同步').click();
    await button(page, '同步到电脑').click();
  }

  async function startOfflinePage(page: Page, staging: { blocked: boolean; delay: boolean; trace?: Trace }) {
    const base = new URL(page.url()).origin;
    const match = (pathname: string) => (url: URL) => url.origin === base && url.pathname === pathname;
    const token = await page.evaluate(() => sessionStorage.getItem('zhixue-session'));
    expect(token).toBeTruthy();
    await page.route(match('/api/mobile/catalog'), route => staging.blocked
      ? route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"验收期间暂缓计时同步。"}' })
      : route.continue());
    await page.route(match('/api/mobile/timer'), async route => {
      const trace = staging.trace;
      if (!trace) return route.continue();
      const request = route.request();
      if (request.method() === 'GET') {
        trace.clockReads++;
        if (!staging.delay || trace.clockReads !== 1) return route.continue();
        const response = await route.fetch();
        expect(response.ok()).toBe(true);
        await new Promise(resolve => setTimeout(resolve, delayMs));
        return route.fulfill({ response });
      }
      const event = request.postDataJSON() as TimerEvent;
      trace.sent.push({ clockReads: trace.clockReads, event });
      if (!staging.delay || event.action !== 'pause') return route.continue();
      const response = await route.fetch();
      expect(response.ok()).toBe(true);
      trace.pause = await response.json() as Receipt;
      staging.blocked = true;
      await route.abort('failed');
    });
    await page.goto(base + '/mobile/#/?session=' + encodeURIComponent(token!));
    await expect(page.getByRole('heading', { name: '智学离线', exact: true })).toBeVisible();
  }

  async function waitActiveSeconds(page: Page, seconds: number) {
    await expect.poll(async () => (await timer(page).locator('time').innerText()).split(':')
      .reduce((total, unit) => total * 60 + Number(unit), 0)).toBeGreaterThanOrEqual(seconds);
  }

  try {
    await withApp(folder, async page => {
      await button(page, '使用此位置').click();
      await button(page, '稍后配置模型').click();
      await expect(page.getByRole('heading', { name: home })).toBeVisible();
      const id = randomUUID();
      await api(page, '/projects', 'POST', { id, topic: 'SQLite', outcome: '安装版离线计时校时验收' });
      await expect.poll(async () => (await api(page, '/projects/' + id)).task.status, { timeout: 45000 }).toBe('ready');
      const lesson = (await api(page, '/projects/' + id)).lessons[0];
      await api(page, '/lessons/' + lesson.id + '/note', 'PUT', { content: '安装版计时验收笔记。' });
      workspace = (await api(page, '/mobile/catalog')).workspace;

      const staging: { blocked: boolean; delay: boolean; trace?: Trace } = { blocked: false, delay: true };
      await startOfflinePage(page, staging);
      await button(page, '下载课程 ' + lesson.title).click();
      await expect(button(page, '打开课程 ' + lesson.title)).toBeVisible();
      await expect.poll(async () => !!(await ledger(page)).clocks[workspace], { timeout: 12000 }).toBe(true);
      staging.blocked = true;
      await synchronize(page);
      await expect(page.getByText('等待连接', { exact: true })).toBeVisible();
      await button(page, '课程').click();
      await button(page, '打开课程 ' + lesson.title).click();
      await button(page, '开始笔记计时').click();
      await waitActiveSeconds(page, 2);
      await button(page, '暂停计时').click();
      await expect(timer(page).getByText('计时已暂停', { exact: true })).toBeVisible();
      const paused = await ledger(page);
      timerId = paused.active!.id;
      await new Promise(resolve => setTimeout(resolve, 2200));
      await button(page, '继续计时').click();
      await waitActiveSeconds(page, Math.floor(paused.active!.elapsed / 1000) + 2);
      await button(page, '结束计时').click();
      await expect(timer(page)).toHaveCount(0);
      const queued = await ledger(page);
      expect(queue(queued)).toEqual(['start', 'pause', 'resume', 'finish']);
      expect(queued.events.every(event => event.timerId === timerId && event.at.stable && event.at.clock && !event.request)).toBe(true);
      expectedIds = queued.events.map(event => event.id);
      pausedMilliseconds = queued.events[2].at.wall - queued.events[1].at.wall;
      expect(pausedMilliseconds).toBeGreaterThanOrEqual(2200);

      await button(page, '同步').click();
      await expect(button(page, '同步到电脑')).toBeEnabled();
      const trace = staging.trace = { clockReads: 0, sent: [] } as Trace;
      staging.blocked = false;
      await synchronize(page);
      await expect.poll(async () => queue(await ledger(page)), { timeout: 20000 }).toEqual(['pause', 'resume', 'finish']);
      await expect.poll(() => trace.pause, { timeout: 20000 }).toBeTruthy();
      await expect(button(page, '同步到电脑')).toBeEnabled();
      pendingAfterLoss = await ledger(page);
      await info.attach('延迟校时与丢失回执', { body: JSON.stringify({
        delayMs, clockReadsBeforeUpload: trace.sent[0].clockReads, pause: trace.pause,
        pending: queue(pendingAfterLoss),
      }), contentType: 'application/json' });
      expect(trace.sent.map(item => item.event.id)).toEqual(expectedIds.slice(0, 2));
      expect(trace.sent[0].clockReads, 'A fresh timer sample must arrive before the first upload').toBe(2);
      expect(trace.pause).toMatchObject({ id: expectedIds[1], state: 'saved', seconds: duration(trace.sent[1].event) });
      pauseSeconds = trace.pause!.seconds;
      expect(pauseSeconds).toBeGreaterThanOrEqual(2);
      expect(pendingAfterLoss.events[0].request).toEqual(trace.sent[1].event);
      expect(receipts().map(item => item.reply.id)).toEqual(expectedIds.slice(0, 2));
      expect(await reportedSeconds(page)).toBe(0);
    });
    await withApp(folder, async page => {
      const staging = { blocked: true, delay: false, trace: { clockReads: 0, sent: [] } as Trace };
      await expect(page.getByRole('heading', { name: home })).toBeVisible();
      await startOfflinePage(page, staging);
      await button(page, '同步').click();
      await expect(page.getByText('3 次计时操作待同步。', { exact: true })).toBeVisible();
      expect(await ledger(page)).toEqual(pendingAfterLoss);
      await expect(button(page, '同步到电脑')).toBeEnabled();
      staging.blocked = false;
      await synchronize(page);
      await expect(page.getByText(/次计时操作待同步/)).toHaveCount(0, { timeout: 20000 });
      await expect(button(page, '同步到电脑')).toBeEnabled();
      expect(staging.trace.sent.map(item => item.event.id)).toEqual(expectedIds.slice(1));
      expect(staging.trace.sent[0].event).toEqual(pendingAfterLoss.events[0].request);
      const saved = receipts();
      expect(saved.map(item => item.action)).toEqual(['start', 'pause', 'resume', 'finish']);
      expect(saved.map(item => item.reply.id)).toEqual(expectedIds);
      expect(saved.every(item => item.reply.state === 'saved')).toBe(true);
      const finishSeconds = duration(staging.trace.sent[2].event);
      expect(finishSeconds).toBeGreaterThanOrEqual(2);
      expect(saved[1].reply.seconds).toBe(pauseSeconds);
      expect(saved[3].reply.seconds).toBe(finishSeconds);
      const seconds = await reportedSeconds(page);
      expect(seconds).toBe(pauseSeconds + finishSeconds);
      expect(await api(page, '/mobile/timer?' + new URLSearchParams({ workspace }))).toMatchObject({
        active: null, pendingReview: 0,
      });
      writeFileSync(info.outputPath('安装版校时与重放.json'), JSON.stringify({
        platform: process.platform, delayMs, clockReadsBeforeUpload: 2, pauseSeconds, finishSeconds, seconds,
        distinctReceipts: new Set(expectedIds).size, pausedMilliseconds, samePauseRequestAfterRestart: true,
      }, null, 2) + '\n');
    });
    expect(readSqlite(database, 'PRAGMA quick_check')).toBe('ok');
    expect(readSqlite(offlineDatabase, 'PRAGMA quick_check')).toBe('ok');
  } finally {
    if (realpathSync(folder) !== folder || dirname(folder) !== parent) throw new Error('Unexpected installed timer fixture path');
    rmSync(folder, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  }
});

test('the installed Electron app restores archived originals and reopens their old wiki paths', async ({}, info) => {
  test.skip(!installed, 'Set ZHIXUE_TEST_ELECTRON to run against an installed app');
  const parent = realpathSync(tmpdir());
  const root = realpathSync(mkdtempSync(join(parent, 'zhixue-installed-portable-trash-')));
  const producer = join(root, 'producer');
  const receiver = join(root, 'receiver');
  const media = join(root, 'encrypted-archives');
  for (const dir of [producer, receiver, media]) mkdirSync(dir);
  const name = '换机回收验收原件.md';
  const line = '归档保留的原文。';
  const original = '# 换机回收验收\n\n' + line;
  const ordinary = 'library/imports/整库恢复后找回.md';
  const confirmed = 'library/imports/核对后找回.md';
  type Archive = { id: string; archivePath: string; originalPath: string; revision: string };
  let entry!: Archive;
  let fileId!: string;
  let source!: string;
  let backup!: { id: string; name: string };
  let backupDigest!: string;
  let restoreId!: string;

  async function initialize(page: Page) {
    await page.getByRole('button', { name: '使用此位置', exact: true }).click();
    await page.getByRole('button', { name: '稍后配置模型', exact: true }).click();
    await expect(page.getByRole('heading', { name: home })).toBeVisible();
  }

  async function resolveWiki(page: Page, target: string) {
    return api(page, '/files/wiki/resolve?' + new URLSearchParams({ target }));
  }

  function oldPaths() {
    return [entry.originalPath, entry.originalPath.replace(/^library\//, '')];
  }

  async function checkPaths(page: Page, path: string, status: 'ready' | 'trashed') {
    for (const target of oldPaths()) {
      const answer = await resolveWiki(page, target);
      expect(answer.state).toBe('resolved');
      expect(answer.choices).toHaveLength(1);
      expect(answer.choices[0]).toMatchObject({ id: fileId, path, status });
    }
  }

  async function restoreBackup(page: Page) {
    const route = '/backups/' + backup.id;
    const preview = await api(page, route + '/preview', 'POST', { password });
    expect(preview.files).toContainEqual(expect.objectContaining({ path: entry.archivePath, revision: entry.revision }));
    expect(preview.restore.confirmation).toBeTruthy();
    const accepted = await api(page, route + '/restore', 'POST', { password, confirmation: preview.restore.confirmation });
    await expect.poll(async () => (await api(page, '/backups/restore-status')).state, { timeout: 35000 }).toBe('complete');
    const result = await api(page, '/backups/restore-status');
    expect(result.id).toBe(accepted.id);
    expect(result.rollbackBackupId).toBeTruthy();
    expect(readFileSync(join(receiver, entry.archivePath), 'utf8')).toBe(original);
    restoreId = result.id;
    return result.rollbackBackupId as string;
  }

  async function openRestore(page: Page, path: string) {
    await page.evaluate(file => { location.hash = '#/library?' + new URLSearchParams({ file }); }, source);
    const preview = page.locator('.library-preview');
    await preview.getByRole('link', { name: '[[旧回收路径]]', exact: true }).click();
    await preview.getByRole('button', { name: '查看回收站', exact: true }).click();
    await page.locator('.trash-entry').filter({ hasText: name }).getByRole('button', { name: '恢复文件', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '恢复文件' });
    await dialog.getByLabel('恢复位置').fill(path);
    return dialog;
  }

  async function checkOriginal(page: Page, path: string) {
    const current = await api(page, '/files/' + fileId + '?' + new URLSearchParams({ at: '3', revision: entry.revision }));
    expect(current).toMatchObject({ id: fileId, path, revision: entry.revision, anchor: { number: 3, text: line } });
    const file = await wire(page, '/files/' + fileId + '/original');
    expect(file.ok()).toBe(true);
    expect(await file.body()).toEqual(Buffer.from(original, 'utf8'));
    expect(readFileSync(join(receiver, path), 'utf8')).toBe(original);
    expect(existsSync(join(receiver, entry.archivePath))).toBe(false);
    await checkPaths(page, path, 'ready');
    const backlinks = await api(page, '/files/' + fileId + '/backlinks');
    expect(backlinks.map((ref: { id: string; at: number }) => [ref.id, ref.at])).toEqual([[source, 1], [source, 3]]);
  }

  try {
    await withApp(producer, async page => {
      await initialize(page);
      fileId = (await api(page, '/files', 'POST', { name, content: original })).id;
      const item = await api(page, '/files/' + fileId);
      const relativePath = item.path.replace(/^library\//, '');
      source = (await api(page, '/files', 'POST', {
        name: '回收原件链接.md', content: '[[' + item.path + '|旧回收路径]]\n\n[[' + relativePath + ']]',
      })).id;
      const offer = await api(page, '/files/' + fileId + '/trash');
      expect(offer.wikiImpact).toMatchObject({ sourceCount: 1, sources: [expect.objectContaining({ id: source, lines: 2 })] });
      entry = await api(page, '/files/' + fileId + '/trash', 'POST', { path: item.path, revision: item.revision });
      await api(page, '/files/reindex', 'POST');
      await checkPaths(page, entry.originalPath, 'trashed');
      await api(page, '/backups/destination', 'PUT', { directory: media });
      backup = await api(page, '/backups', 'POST', { password });
      backupDigest = digest(join(media, backup.name));
    });

    await test.step('reads archived paths after opening an encrypted backup in a fresh workspace', async () => {
      await withApp(receiver, async page => {
        await initialize(page);
        const prior = await api(page, '/files', 'POST', { name: '接收库原有资料.md', content: '# 本机现有资料' });
        const priorPath = (await api(page, '/files/' + prior.id)).path;
        await api(page, '/backups/destination', 'PUT', { directory: media });
        const rollback = await restoreBackup(page);
        const preview = await api(page, '/backups/' + rollback + '/preview', 'POST', { password });
        expect(preview.files).toContainEqual(expect.objectContaining({ path: priorPath }));
        await checkPaths(page, entry.originalPath, 'trashed');
        const dialog = await openRestore(page, ordinary);
        await dialog.getByRole('button', { name: '确认恢复', exact: true }).click();
        await expect(dialog).toBeHidden();
        await expect(page.locator('.library-preview').getByText(ordinary, { exact: true })).toBeVisible();
        await checkOriginal(page, ordinary);
      });
    });

    await test.step('confirms unchanged trash bytes after a file identity change', async () => {
      await withApp(receiver, async page => {
        await expect(page.getByRole('heading', { name: home })).toBeVisible();
        await restoreBackup(page);
        const archive = realpathSync(join(receiver, entry.archivePath));
        expect(relative(realpathSync(join(receiver, 'library', '.trash')), archive)).not.toMatch(/^\.\./);
        const copied = join(dirname(archive), '.installer-test-copy');
        writeFileSync(copied, readFileSync(archive), { flag: 'wx' });
        renameSync(copied, archive);
        const open = async () => {
          const dialog = await openRestore(page, confirmed);
          await dialog.getByRole('button', { name: '确认恢复', exact: true }).click();
          await expect(dialog.getByRole('status')).toContainText('当前内容与回收记录的版本一致。');
          return dialog;
        };
        const canceled = await open();
        await canceled.screenshot({ path: info.outputPath('安装版回收原件核对.png') });
        await canceled.getByRole('button', { name: '取消', exact: true }).click();
        await checkPaths(page, entry.originalPath, 'trashed');
        expect(readFileSync(archive, 'utf8')).toBe(original);
        const dialog = await open();
        await dialog.getByRole('button', { name: '按核对版本恢复', exact: true }).click();
        await expect(dialog).toBeHidden();
        await expect(page.locator('.library-preview').getByText(confirmed, { exact: true })).toBeVisible();
        await checkOriginal(page, confirmed);
      });
    });

    await test.step('reads the recovered bytes and both old paths after restart', async () => {
      await withApp(receiver, async page => {
        await expect(page.getByRole('heading', { name: home })).toBeVisible();
        await checkOriginal(page, confirmed);
        expect((await api(page, '/backups/restore-status')).id).toBe(restoreId);
        expect(digest(join(media, backup.name))).toBe(backupDigest);
        const database = join(receiver, 'zhixue.sqlite3');
        expect(schema(database)).toBe(expectedMigrationHead());
        expect(readSqlite(database, 'PRAGMA quick_check')).toBe('ok');
      });
    });
    await info.attach('installed-trash-recovery', { contentType: 'application/json', body: JSON.stringify({
      id: fileId, revision: entry.revision, oldPaths: oldPaths(), ordinary, confirmed, restoreId, backupDigest,
    }, null, 2) });
  } finally {
    expect(dirname(realpathSync(root))).toBe(parent);
    rmSync(root, { recursive: true });
  }
});

test('the installed Electron app saves a returned original and reopens it after restart', async ({}, info) => {
  test.skip(!installed, 'Set ZHIXUE_TEST_ELECTRON to run against an installed app');
  const parent = realpathSync(tmpdir());
  const root = realpathSync(mkdtempSync(join(parent, 'zhixue-installed-original-return-')));
  const workspace = join(root, 'workspace');
  mkdirSync(workspace);
  const name = '归位验收资料.md';
  const first = '# 归位验收\n首版原文。';
  const returned = '# 归位验收\n在原目录外修改的原文。';
  const line = '归位后再次保存的原文。';
  const updated = '# 归位验收\n' + line;
  const revision = createHash('sha256').update(updated).digest('hex');
  let fileId!: string;
  let originalPath!: string;
  let physical!: string;
  let linkSource!: string;

  async function checkUpdated(page: Page) {
    const detail = await api(page, '/files/' + fileId + '?' + new URLSearchParams({ at: '2', revision }));
    expect(detail).toMatchObject({ id: fileId, status: 'ready', path: originalPath, revision,
      content: updated, anchor: { number: 2, text: line } });
    const original = await wire(page, '/files/' + fileId + '/original');
    expect(original.ok()).toBe(true);
    expect(await original.body()).toEqual(Buffer.from(updated, 'utf8'));
    expect(digest(physical)).toBe(revision);
    for (const target of [originalPath, originalPath.replace(/^library\//, '')]) {
      const wiki = await api(page, '/files/wiki/resolve?' + new URLSearchParams({ target }));
      expect(wiki.state).toBe('resolved');
      expect(wiki.choices).toHaveLength(1);
      expect(wiki.choices[0]).toMatchObject({ id: fileId, path: originalPath, status: 'ready' });
    }
    await page.evaluate(file => { location.hash = '#/library?' + new URLSearchParams({ file }); }, linkSource);
    const preview = page.locator('.library-preview');
    await preview.getByRole('link', { name: '[[原件位置]]', exact: true }).click();
    await expect(preview).toContainText(line);
  }

  try {
    await withApp(workspace, async page => {
      await page.getByRole('button', { name: '使用此位置', exact: true }).click();
      await page.getByRole('button', { name: '稍后配置模型', exact: true }).click();
      await expect(page.getByRole('heading', { name: home })).toBeVisible();
      expect(schema(join(workspace, 'zhixue.sqlite3'))).toBe(expectedMigrationHead());
      await test.step('returns the original and reads it with the same file identity', async () => {
        fileId = (await api(page, '/files', 'POST', { name, content: first })).id;
        const stored = await api(page, '/files/' + fileId);
        originalPath = stored.path;
        physical = realpathSync(join(workspace, originalPath));
        const within = relative(realpathSync(join(workspace, 'library')), physical);
        expect(within && !isAbsolute(within) && !/^\.\.(?:[\\/]|$)/.test(within)).toBeTruthy();
        expect(readFileSync(physical, 'utf8')).toBe(first);
        linkSource = (await api(page, '/files', 'POST', {
          name: '原件归位引用.md', content: '[[' + originalPath + '|原件位置]]',
        })).id;
        const key = statSync(physical, { bigint: true });
        expect(key.ino).not.toBe(0n);
        const parked = join(workspace, '移出期间.md');
        renameSync(physical, parked);
        expect(await api(page, '/files/' + fileId)).toMatchObject({ id: fileId, status: 'missing' });
        writeFileSync(parked, returned);
        renameSync(parked, physical);
        const seen = statSync(physical, { bigint: true });
        expect([seen.dev, seen.ino]).toEqual([key.dev, key.ino]);
        expect(await api(page, '/files/' + fileId)).toMatchObject({
          id: fileId, status: 'ready', path: originalPath, content: returned,
        });
      });

      await test.step('saves a later replacement and opens it from the original link', async () => {
        const replacement = physical + '.installer-test-write';
        writeFileSync(replacement, updated, { flag: 'wx' });
        renameSync(replacement, physical);
        await checkUpdated(page);
      });
    });

    await test.step('reopens the updated original and its old link after restart', async () => {
      await withApp(workspace, async page => {
        await expect(page.getByRole('heading', { name: home })).toBeVisible();
        await checkUpdated(page);
        expect(readSqlite(join(workspace, 'zhixue.sqlite3'), 'PRAGMA quick_check')).toBe('ok');
        await page.locator('.library-preview').screenshot({ path: info.outputPath('安装版原件归位读回.png') });
      });
    });
  } finally {
    expect(dirname(realpathSync(root))).toBe(parent);
    rmSync(root, { recursive: true, maxRetries: 5, retryDelay: 200 });
  }
});
