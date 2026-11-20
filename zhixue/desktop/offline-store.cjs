const { DatabaseSync } = require('node:sqlite');

function openOfflineStudy(file) {
  const database = new DatabaseSync(file);
  try {
    database.exec(`
      PRAGMA synchronous = FULL;
      CREATE TABLE IF NOT EXISTS offline_study (
        area TEXT NOT NULL, key TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT,
        PRIMARY KEY (area, key)
      ) STRICT;
    `);
    const items = database.prepare('SELECT payload FROM offline_study WHERE area = ? AND payload IS NOT NULL ORDER BY key');
    const item = database.prepare('SELECT revision, payload FROM offline_study WHERE area = ? AND key = ?');
    const insert = database.prepare(`
      INSERT INTO offline_study (area, key, revision, payload) VALUES (?, ?, 1, ?)
      ON CONFLICT (area, key) DO NOTHING
    `);
    const update = database.prepare(`
      UPDATE offline_study SET revision = revision + 1, payload = ?
      WHERE area = ? AND key = ? AND revision = ?
    `);
    return {
      list() {
        return items.all('items').map(row => JSON.parse(row.payload));
      },
      read(area, key) {
        checkKey(area, key);
        const saved = item.get(area, key);
        return { revision: saved?.revision ?? 0, value: saved?.payload == null ? undefined : JSON.parse(saved.payload) };
      },
      write(area, key, revision, value) {
        checkKey(area, key);
        if (!Number.isSafeInteger(revision) || revision < 0) throw new Error('本机离线记录版本无效。');
        const payload = value === undefined ? null : JSON.stringify(value);
        const result = revision === 0 ? insert.run(area, key, payload) : update.run(payload, area, key, revision);
        return result.changes === 1;
      },
      close() { database.close(); },
    };
  } catch (error) {
    database.close();
    throw error;
  }
}

function checkKey(area, key) {
  if (typeof key !== 'string' || !key || (area !== 'items' && !(area === 'timers' && key === 'ledger'))) {
    throw new Error('本机离线记录标识无效。');
  }
}

module.exports = { openOfflineStudy };
