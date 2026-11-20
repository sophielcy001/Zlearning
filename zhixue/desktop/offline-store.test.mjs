import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { test } from 'node:test';
import { openOfflineStudy } from './offline-store.cjs';

test('offline workspace records survive restart and stale edits cannot undo a newer save', async t => {
  const parent = resolve(tmpdir());
  const directory = await mkdtemp(join(parent, 'zhixue-offline-store-'));
  t.after(async () => {
    assert.equal(dirname(directory), parent);
    await rm(directory, { recursive: true });
  });
  const database = join(directory, 'offline.sqlite3');
  const key = 'workspace:lesson:1';
  const store = openOfflineStudy(database);
  const other = openOfflineStudy(database);
  try {
    assert.equal(store.read('items', key).revision, 0);
    assert.equal(other.write('items', key, 0, { key, draft: 'first' }), true);
    const previous = store.read('items', key);
    assert.equal(other.write('items', key, 1, { key, draft: 'newer' }), true);
    assert.equal(store.write('items', key, previous.revision, { key, draft: 'stale' }), false);
    assert.equal(store.write('timers', 'ledger', 0, { events: ['start', 'finish'] }), true);
    assert.throws(() => store.read('device', 'pair'), /标识无效/);
    assert.throws(() => store.write('timers', 'unknown', 0, {}), /标识无效/);
  } finally {
    store.close();
    other.close();
  }
  const reopened = openOfflineStudy(database);
  try {
    assert.deepEqual(reopened.list(), [{ key, draft: 'newer' }]);
    assert.deepEqual(reopened.read('timers', 'ledger'), { revision: 1, value: { events: ['start', 'finish'] } });
    assert.equal(reopened.write('items', key, 2, undefined), true);
    assert.equal(reopened.write('items', key, 0, { key, draft: 'old browser import' }), false);
    assert.equal(reopened.write('items', key, 2, { key, draft: 'old open view' }), false);
    assert.deepEqual(reopened.list(), []);
  } finally {
    reopened.close();
  }
});
