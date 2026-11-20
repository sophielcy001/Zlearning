import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { setTimeout as wait } from 'node:timers/promises';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';
import { createServer } from 'node:http';

async function withService(binary, folder, use) {
  const { PYTHONHOME, PYTHONPATH, VIRTUAL_ENV, ...runtime } = process.env;
  const child = spawn(binary, ['--stdio', '--port', '0', '--data', folder], {
    cwd: dirname(folder), env: runtime, stdio: ['pipe', 'pipe', 'inherit'], windowsHide: true,
  });
  child.stdin.on('error', () => {});
  const closed = once(child, 'close').then(([code]) => code);
  const reader = createInterface({ input: child.stdout });
  const received = once(reader, 'line').then(([line]) => new URL(line));
  const signal = AbortSignal.timeout(20000);
  try {
    const address = await Promise.race([
      received,
      closed.then(code => { throw new Error('Bundled service exited during startup (' + code + ')'); }),
      once(signal, 'abort').then(() => { throw new Error('Bundled service did not report its address'); }),
    ]);
    const session = new URLSearchParams(address.hash.split('?')[1]).get('session');
    assert.equal(address.hostname, '127.0.0.1');
    assert(session);
    const request = async (path, method = 'GET', body, contentType) => {
      const recording = Buffer.isBuffer(body);
      return fetch(address.origin + path, {
        method, signal: AbortSignal.timeout(recording ? 45000 : 3000),
        headers: { Authorization: 'Bearer ' + session, 'Content-Type': contentType ?? (recording ? 'audio/wav' : 'application/json') },
        body: recording || body === undefined ? body : JSON.stringify(body),
      });
    };
    const json = async (path, method, body) => {
      const response = await request(path, method, body);
      if (!response.ok) throw new Error(path + ' returned ' + response.status + ' ' + await response.text());
      return response.json();
    };
    await use({ request, json });
  } finally {
    reader.close();
    child.stdin.end();
    const timeout = setTimeout(() => child.kill('SIGKILL'), 10000).unref();
    try {
      assert.equal(await closed, 0, 'Bundled service should stop when its parent closes stdin');
    } finally {
      clearTimeout(timeout);
    }
  }
}

async function until(check, message) {
  const deadline = Date.now() + 11000;
  while (Date.now() < deadline) {
    try {
      if (await check()) return;
    } catch (error) {
      if (!(error instanceof TypeError || error instanceof DOMException)) throw error;
    }
    await wait(85);
  }
  throw new Error(message);
}

export async function verifyService(binary) {
  const testRoot = mkdtempSync(join(tmpdir(), 'zhixue-sidecar-'));
  const control = join(testRoot, 'device');
  const workspace = join(testRoot, 'Learning data');
  const file = { name: 'package-test.txt', content: 'Portable workspace verification' };
  const identity = randomUUID();
  let original;
  const prompts = [];
  const provider = createServer((incoming, response) => {
    let body = '';
    incoming.setEncoding('utf8');
    incoming.on('data', chunk => { body += chunk; });
    incoming.on('end', () => {
      const prompt = JSON.parse(body).messages[0].content;
      prompts.push(prompt);
      const fixture = prompt.includes('知识库整理发送范围 JSON') ? {
        suggestions: [{ file: 'F1', folder: 'D1', tags: ['Package study'], reason: 'Archive the local package fixture' }],
      } : {
        links: [{ source: 'N1', target: 'N2', both: true, reason: 'Package fixture relationship' }],
        groups: [{ name: 'Package fixture group', members: ['N1', 'N2'], reason: 'Both fixture sources' }],
      };
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({ choices: [{ message: { content: JSON.stringify(fixture) } }] }));
    });
  });
  try {
    provider.listen(0, '127.0.0.1');
    await once(provider, 'listening');
    const { port } = provider.address();
    await withService(binary, control, async ({ request, json }) => {
      const page = await request('/');
      assert(page.ok);
      const html = await page.text();
      const asset = html.match(/src="(\/assets\/[^"]+\.js)"/)?.[1];
      assert(asset && (await request(asset)).ok, 'The bundled frontend and assets must be served');
      assert.equal((await json('/api/setup')).stage, 'storage');
      await json('/api/setup/storage', 'PUT', { directory: workspace });
      await until(async () => (await json('/api/setup')).stage === 'models', 'Bundled workspace failed to open');
      await json('/api/setup/complete', 'POST');
      assert.equal((await json('/api/voice')).available, true, 'Packaged voice model and runtime must be available');
      const sample = readFileSync(new URL('../e2e/fixtures/voice-jfk.wav', import.meta.url));
      const draft = await json('/api/voice/transcribe?language=en', 'POST', sample);
      assert.match(draft.text, /fellow Americans/i, 'Packaged native libraries must transcribe local audio');
      const htmlFixture = Buffer.from('<main><h1>HTML fixture</h1><p>Bundled web snapshot text</p></main>');
      const uploaded = await request('/api/files/upload?name=package-web.html', 'POST', htmlFixture, 'application/octet-stream');
      assert.equal(uploaded.status, 201);
      const webFile = await uploaded.json();
      const webSource = await json('/api/files/' + webFile.id);
      assert.equal(webSource.sections[0].label, '网页第 1 节 · HTML fixture');
      assert.match(webSource.content, /Bundled web snapshot text/);
      assert(Buffer.from(await (await request('/api/files/' + webFile.id + '/original')).arrayBuffer()).equals(htmlFixture));
      original = await json('/api/files', 'POST', file);
      assert.equal((await json('/api/files/' + original.id)).content, file.content);
      await json('/api/projects', 'POST', { id: identity, topic: 'SQLite', outcome: 'Review a packaged course' });
      const project = () => json('/api/projects/' + identity);
      await until(async () => (await project()).task.status === 'ready', 'Bundled Worker did not prepare courses');
      const lesson = (await project()).lessons[0].id;
      assert(readFileSync(join(workspace, 'library', 'courses', lesson + '.md'), 'utf8'));
      const model = await json('/api/models/connections', 'POST', {
        name: 'Package audit mock', provider: 'custom', protocol: 'chat-completions', model: 'package-graph',
        endpoint: 'http://127.0.0.1:' + port + '/v1',
      });
      await json('/api/models/roles', 'PATCH', { main: model.id });
      const graph = '/api/library/graph';
      const recommendations = graph + '/recommendations';
      const before = await json(graph);
      assert(before.nodes.some(node => node.lessonId === lesson));
      const scope = { id: randomUUID(), nodeIds: [original.id, webFile.id], withExcerpts: true };
      const preview = await json(recommendations + '/preview', 'POST', scope);
      assert.deepEqual(preview.nodes.map(node => node.title), [file.name, 'package-web.html']);
      assert.equal(prompts.length, 0, 'Frozen graph preview must wait for confirmation');
      await json(recommendations, 'POST', { ...scope, consent: preview.consent });
      let pending;
      await until(async () => {
        const state = await json(recommendations);
        if (state.jobs[0]?.state === 'failed') throw new Error(state.jobs[0].error);
        pending = state.candidates;
        return pending.length === 2;
      }, 'Bundled worker did not save graph candidates');
      assert.equal(prompts.length, 1);
      assert(!prompts[0].includes(original.id) && !prompts[0].includes(webFile.id));
      assert.equal((await json(graph)).edges.length, 0);
      const reviewed = await json(recommendations + '/review', 'POST', {
        revision: before.revision, action: 'accept', ids: pending.map(item => item.id),
      });
      assert.equal(reviewed.accepted, 2);
      assert.equal(reviewed.graph.edges[0].both, true);
      assert.equal(reviewed.graph.groups[0].name, 'Package fixture group');

      const destination = await json('/api/files/folders', 'POST', { parent: 'library', name: 'Package archive' });
      const classifications = '/api/files/classification';
      const classificationScope = {
        id: randomUUID(), fileIds: [original.id], folders: [destination.path], withExcerpts: true, autoApply: false,
      };
      const disclosure = await json(classifications + '/preview', 'POST', classificationScope);
      assert.deepEqual(disclosure.files[0].excerpts.map(part => part.text), [file.content]);
      assert.equal(prompts.length, 1, 'Frozen file classification preview must wait for confirmation');
      await json(classifications, 'POST', { ...classificationScope, consent: disclosure.consent });
      let suggestions;
      await until(async () => {
        const state = await json(classifications);
        if (state.jobs[0]?.state === 'failed') throw new Error(state.jobs[0].error);
        suggestions = state.pending;
        return suggestions.length === 1;
      }, 'Bundled worker did not save the file classification');
      assert.deepEqual(JSON.parse(prompts[1].split('\n知识库整理发送范围 JSON\n')[1]), {
        files: disclosure.files, folders: disclosure.folders,
      });
      assert.equal((await json('/api/files/' + original.id)).tags.length, 0);
      const applied = await json(classifications + '/review', 'POST', { ids: [suggestions[0].id], choice: 'apply' });
      assert.equal(applied.reviewed[0].state, 'applied');
      assert.equal(applied.reviewed[0].result.path, destination.path + '/' + original.id + '.txt');
      const organized = await json('/api/files/' + original.id);
      assert.equal(organized.content, file.content);
      assert.deepEqual(organized.tags.map(tag => tag.name), ['Package study']);
      const copyPlan = await json('/api/files/batch/preview', 'POST', { action: 'copy', ids: [original.id], parent: 'library' });
      const copies = await json('/api/files/batch/confirm', 'POST', { action: 'copy', parent: 'library', files: copyPlan.entries });
      const copy = await json('/api/files/' + copies.entries[0].copyId);
      assert.equal(copy.content, file.content);
      assert.deepEqual(copy.tags, organized.tags);

      const backup = await json('/api/backups', 'POST', { password: 'backup-smoke-only' });
      assert.equal(backup.status, 'ready');
    });
    await withService(binary, control, async ({ json }) => {
      assert.equal((await json('/api/setup')).stage, 'complete');
      const saved = await json('/api/files/' + original.id);
      assert.equal(saved.content, file.content);
      assert.equal(saved.path, 'library/Package archive/' + original.id + '.txt');
      assert.equal(saved.tags[0].name, 'Package study');
      assert.equal((await json('/api/projects/' + identity)).task.status, 'ready');
      const graph = await json('/api/library/graph');
      assert.equal(graph.edges[0].both, true);
      assert.equal(graph.groups[0].name, 'Package fixture group');
      assert.equal(prompts.length, 2, 'Bundle restart must preserve graph and classification decisions without rerunning the model');
    });
    console.log('Bundled service passed setup, local voice, HTML import, Worker, graph review, file classification and copy, archive creation, restart and shutdown.');
  } finally {
    provider.closeAllConnections();
    if (provider.listening) await new Promise(done => provider.close(done));
    rmSync(testRoot, { recursive: true, force: true });
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  assert(process.argv[2], 'Pass the path to the bundled service.');
  await verifyService(resolve(process.argv[2]));
}
