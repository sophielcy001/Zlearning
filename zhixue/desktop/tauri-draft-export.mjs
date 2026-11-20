import { cp, lstat, mkdir, mkdtemp, readdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { parseArgs } from 'node:util';
import { chromium } from 'playwright';

const entryName = /^http_127\.0\.0\.1_([1-9]\d{0,4})\.indexeddb\.(leveldb|blob)$/;
const pagePath = '/zhixue-draft-export';
const databaseName = 'zhixue-mobile-study';

async function copyBuckets(source, destination) {
  const ports = new Set();
  for (const item of await readdir(source, { withFileTypes: true })) {
    const found = entryName.exec(item.name);
    if (!found || Number(found[1]) > 65535) continue;
    if (!item.isDirectory() || item.isSymbolicLink()) throw new Error('旧版资料目录中含有链接，请检查来源。');
    if (found[2] === 'leveldb') ports.add(Number(found[1]));
    await cp(join(source, item.name), join(destination, item.name), {
      recursive: true, force: false, errorOnExist: true,
      filter: async path => {
        const entry = await lstat(path);
        if (!entry.isDirectory() && !entry.isFile()) throw new Error('旧版数据库含有链接或特殊文件。');
        return true;
      },
    });
  }
  return [...ports].sort((a, b) => a - b);
}

async function readItems(page) {
  return page.evaluate(async name => {
    if (!(await indexedDB.databases()).some(item => item.name === name)) return null;
    return new Promise((resolveRead, reject) => {
      const request = indexedDB.open(name);
      request.onerror = () => reject(request.error);
      request.onblocked = () => reject(new Error('旧版离线数据库仍被占用。'));
      request.onupgradeneeded = () => {
        request.transaction.abort();
        reject(new Error('读取期间旧版离线数据库已变化。'));
      };
      request.onsuccess = () => {
        const database = request.result;
        if (!database.objectStoreNames.contains('items')) {
          database.close();
          reject(new Error('旧版离线数据库缺少课程和题组记录。'));
          return;
        }
        const tx = database.transaction('items', 'readonly');
        const lookup = tx.objectStore('items').getAll();
        tx.onabort = () => { database.close(); reject(tx.error); };
        tx.oncomplete = () => { database.close(); resolveRead(lookup.result); };
      };
    });
  }, databaseName);
}

async function exportDrafts(profile) {
  const source = resolve(profile, 'EBWebView', 'Default', 'IndexedDB');
  const copy = await mkdtemp(join(tmpdir(), 'zhixue-tauri-draft-export-'));
  const buckets = join(copy, 'Default', 'IndexedDB');
  let context;
  try {
    await mkdir(buckets, { recursive: true });
    const ports = await copyBuckets(source, buckets);
    if (!ports.length) throw new Error('未找到旧版 Tauri 的回环数据库。');
    context = await chromium.launchPersistentContext(copy, {
      channel: 'chrome', headless: true, serviceWorkers: 'block',
      args: ['--disable-background-networking', '--no-proxy-server', '--host-resolver-rules=MAP * ~NOTFOUND'],
    });
    const urls = new Set(ports.map(port => 'http://127.0.0.1:' + port + pagePath));
    await context.route('**/*', route => urls.has(route.request().url()) && route.request().resourceType() === 'document'
      ? route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>智学离线草稿导出</title>' })
      : route.abort());
    const page = context.pages()[0] ?? await context.newPage();
    const origins = [];
    for (const port of ports) {
      await page.goto('http://127.0.0.1:' + port + pagePath, { timeout: 15000 });
      const records = await readItems(page);
      if (records !== null) origins.push({ port, records });
    }
    if (!origins.length) throw new Error('回环数据库中未找到智学的离线记录库。');
    return { format: 'zhixue-tauri-drafts-v1', origins };
  } finally {
    try { await context?.close(); }
    finally { await rm(copy, { recursive: true, force: true, maxRetries: 8, retryDelay: 200 }); }
  }
}

try {
  const { values } = parseArgs({
    options: { profile: { type: 'string' }, output: { type: 'string' }, help: { type: 'boolean' } },
  });
  if (values.help) {
    console.log('node desktop/tauri-draft-export.mjs --profile <Tauri WebView2 资料目录> --output <新 JSON 文件>');
  } else {
    if (!values.profile || !values.output) throw new Error('请提供 --profile 和 --output。');
    const bundle = await exportDrafts(values.profile);
    await writeFile(resolve(values.output), JSON.stringify(bundle, null, 2) + '\n', { encoding: 'utf8', flag: 'wx' });
    console.log('已导出 ' + bundle.origins.length + ' 个旧端口的记录。');
  }
} catch (error) {
  console.error('无法导出旧版草稿。' + error.message);
  process.exitCode = 1;
}
