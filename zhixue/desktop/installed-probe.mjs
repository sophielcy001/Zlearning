import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { appendFile, readFile, realpath } from 'node:fs/promises';
import { createServer as httpServer } from 'node:http';
import { createServer } from 'node:net';
import { basename, dirname, isAbsolute, join, relative } from 'node:path';
import { setTimeout as wait } from 'node:timers/promises';
import { chromium } from 'playwright';
import { graphSummary } from './windows-evidence.mjs';

const firstUseTitle = '选择知识库位置';
const homeTitle = '今天，想弄懂什么？';

function loopback(url) {
  const value = URL.parse(url);
  return value?.protocol === 'http:' && value.hostname === '127.0.0.1' && value.port ? value : null;
}

async function freePort() {
  const listener = createServer();
  await new Promise((resolve, reject) => listener.once('error', reject).listen(0, '127.0.0.1', resolve));
  try {
    return listener.address().port;
  } finally {
    await new Promise((resolve, reject) => listener.close((error) => error ? reject(error) : resolve()));
  }
}

export async function observed(check, message, timeoutMs = 12000) {
  const deadline = performance.now() + timeoutMs;
  do {
    const found = await check();
    if (found) return found;
    await wait(100);
  } while (performance.now() < deadline);
  throw new Error(message);
}

function start(executable, args, env) {
  const child = spawn(executable, args, { cwd: dirname(executable), env, stdio: 'ignore' });
  let ended = false;
  const outcome = new Promise((settle) => {
    child.once('error', () => { ended = true; settle({ error: true }); });
    child.once('exit', (code, signal) => { ended = true; settle({ code, signal }); });
  });
  return { child, outcome, ended: () => ended };
}

export async function stopOwned(app, signal = 'SIGTERM') {
  if (!app.ended()) app.child.kill(signal);
  await observed(app.ended, '评测启动的桌面进程没有退出。', 14000);
  return app.outcome;
}

export async function openInstalled(executable, framework, data, webProfile, firstUse, webviewArguments = []) {
  const port = await freePort();
  const cdpOrigin = 'http://127.0.0.1:' + port;
  const debug = ['--remote-debugging-address=127.0.0.1', '--remote-debugging-port=' + port];
  const env = { ...process.env, ZHIXUE_DEMO_DATA: data };
  delete env.ELECTRON_RUN_AS_NODE;
  if (framework === 'tauri') {
    env.WEBVIEW2_USER_DATA_FOLDER = webProfile;
    env.WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = [...debug, ...webviewArguments].join(' ');
    delete env.WEBVIEW2_WAIT_FOR_SCRIPT_DEBUGGER;
  }
  const began = performance.now();
  const app = start(executable, framework === 'electron' ? debug : [], env);
  try {
    const version = await observed(async () => {
      if (app.ended()) throw new Error('安装版桌面应用在打开页面前退出。');
      try {
        const response = await fetch(cdpOrigin + '/json/version', { redirect: 'error', signal: AbortSignal.timeout(450) });
        return response.ok ? await response.json() : null;
      } catch {
        return null;
      }
    }, '安装版没有在环回地址提供测试页面。', 22000);
    const webSocket = URL.parse(version.webSocketDebuggerUrl);
    assert(webSocket?.protocol === 'ws:' && webSocket.hostname === '127.0.0.1' && webSocket.port === String(port),
      '环回调试会话的地址与本次测量配置不符。');
    const browser = await chromium.connectOverCDP(webSocket.href, { timeout: 6000 });
    const page = await observed(() => browser.contexts().flatMap((context) => context.pages())
      .find((candidate) => loopback(candidate.url())), '无法连接安装版的本地学习页面。');
    await page.getByRole('heading', { name: firstUse ? firstUseTitle : homeTitle, exact: true })
      .waitFor({ state: 'visible', timeout: 24000 });
    const readyMs = Math.round(performance.now() - began);
    if (firstUse) {
      const chosen = await page.getByLabel('保存文件夹', { exact: true }).inputValue();
      const shown = await realpath(chosen);
      assert.equal(relative(await realpath(data), shown), '', '首启使用的知识库位置与评测目录不符。');
    }
    const origin = loopback(page.url()).origin;
    const session = await page.evaluate(() => sessionStorage.getItem('zhixue-session'));
    assert(session, '正式网页没有建立本机会话。');
    const renderEngine = String(version.Browser ?? '');
    const viewport = await page.evaluate(() => ({ width: innerWidth, height: innerHeight, dpr: devicePixelRatio }));
    const appResult = { ...app, page, origin, env, framework, viewport, renderEngine, readyMs, began };
    appResult.api = async (endpoint, method = 'GET', body) => {
      assert(endpoint.startsWith('/api/'));
      const response = await fetch(origin + endpoint, {
        method, signal: AbortSignal.timeout(5000),
        headers: { Authorization: 'Bearer ' + session, 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      assert(response.ok, endpoint + ' 返回 HTTP ' + response.status + '。');
      return response.json();
    };
    return appResult;
  } catch (error) {
    await stopOwned(app);
    throw error;
  }
}

export async function reopenExisting(executable, app) {
  const repeat = start(executable, [], app.env);
  try {
    await observed(repeat.ended, '再次启动没有交回当前窗口。', 10000);
    assert.equal((await repeat.outcome).code, 0, '第二次启动退出异常。');
    assert(!app.ended(), '原桌面进程没有保留。');
  } finally {
    if (!repeat.ended()) await stopOwned(repeat);
  }
}

export async function serviceStopped(app) {
  return observed(async () => {
    try {
      await fetch(app.origin + '/healthz', { signal: AbortSignal.timeout(375) });
      return false;
    } catch (error) {
      return error.cause?.code === 'ECONNREFUSED';
    }
  }, '退出后原本地服务仍在响应。', 10000);
}

export async function prepareWorkspace(app, data) {
  await app.page.getByRole('button', { name: '使用此位置', exact: true }).click();
  await app.page.getByRole('button', { name: '稍后配置模型', exact: true }).click({ timeout: 15000 });
  await app.page.getByRole('heading', { name: homeTitle, exact: true }).waitFor();
  const file = { name: 'windows-evaluation.txt', content: '相同的桌面评测资料与 SQLite 样例。' };
  const document = await app.api('/api/files', 'POST', file);
  const stored = await app.api('/api/files/' + document.id);
  assert.equal(stored.content, file.content);
  const disk = await realpath(join(data, stored.path));
  const difference = relative(await realpath(data), disk);
  assert(difference && !difference.startsWith('..') && !isAbsolute(difference) && basename(disk) !== '',
    '评测文件没有保存在隔离的工作区。');
  assert.equal(await readFile(disk, 'utf8'), file.content);
  const changed = file.content + '\n外部修改记录。';
  await appendFile(disk, '\n外部修改记录。', 'utf8');
  await observed(async () => (await app.api('/api/files/' + document.id)).content === changed,
    '应用没有读到隔离资料的外部修改。');
  const project = { id: randomUUID(), topic: 'SQLite 入门', outcome: '复验同一份课程、笔记和计时数据' };
  await app.api('/api/projects', 'POST', project);
  const result = await observed(async () => {
    const current = await app.api('/api/projects/' + project.id);
    return current.task.status === 'ready' ? current : null;
  }, '打包应用的 Worker 没有完成固定课程样例。');
  assert.equal(result.lessons.length, 3);
  const lesson = result.lessons[0];
  const note = '桌面评测用笔记。';
  await app.api('/api/lessons/' + lesson.id + '/note', 'PUT', { content: note });
  const view = await app.api('/api/lessons/' + lesson.id);
  assert(view.body && view.note === note);
  return { documentId: document.id, content: changed, projectId: project.id, lessonId: lesson.id, note };
}

export async function inspectRecovery(app, seed) {
  assert.equal((await app.api('/api/setup')).stage, 'complete');
  assert.equal((await app.api('/api/files/' + seed.documentId)).content, seed.content);
  assert.equal((await app.api('/api/projects/' + seed.projectId)).task.status, 'ready');
  const lesson = await app.api('/api/lessons/' + seed.lessonId);
  assert(lesson.body && lesson.note === seed.note, '重启后不能读取原来的课程及笔记。');
}

export async function animateGraph(page, nodeCount, seconds) {
  const measured = await page.evaluate(({ nodeCount, seconds }) => {
    if (document.visibilityState !== 'visible') throw new Error('绘制图谱时窗口必须可见。');
    const canvas = document.createElement('canvas');
    canvas.width = 1000;
    canvas.height = 640;
    canvas.style.cssText = 'position:fixed;inset:0;z-index:100000;background:#f8faff;width:min(100vw,1000px);height:auto';
    document.body.append(canvas);
    const bounds = canvas.getBoundingClientRect();
    const surface = {
      width: Math.min(innerWidth, bounds.width), height: Math.min(innerHeight, bounds.height), dpr: devicePixelRatio,
    };
    const draw = canvas.getContext('2d');
    if (!draw) {
      canvas.remove();
      throw new Error('Canvas 2D 不可用。');
    }
    const rows = Math.ceil(nodeCount / 50);
    const nodes = Array.from({ length: nodeCount }, (_, index) => ({
      x: 12 + (index % 50) * 19.5, y: 10 + Math.floor(index / 50) * (620 / rows),
      phase: index * 0.37, dx: 0, dy: 0,
    }));
    const periods = [];
    return new Promise((resolve) => {
      let id;
      let first;
      let previous;
      const finish = () => {
        cancelAnimationFrame(id);
        canvas.remove();
        resolve({ periods, surface });
      };
      const fallback = setTimeout(finish, seconds * 1000 + 1800);
      function frame(now) {
        first ??= now;
        const elapsed = (now - first) / 1000;
        if (previous !== undefined && elapsed > 0.3) periods.push(now - previous);
        previous = now;
        draw.clearRect(0, 0, canvas.width, canvas.height);
        for (const node of nodes) {
          node.dx = node.x + Math.sin(elapsed * 0.9 + node.phase) * 5;
          node.dy = node.y + Math.cos(elapsed * 0.7 + node.phase) * 4;
        }
        draw.strokeStyle = '#becfe8';
        draw.lineWidth = 0.55;
        draw.beginPath();
        for (const [index, node] of nodes.entries()) {
          for (const linked of [nodes[(index + 1) % nodeCount], nodes[(index + 50) % nodeCount]]) {
            draw.moveTo(node.dx, node.dy);
            draw.lineTo(linked.dx, linked.dy);
          }
        }
        draw.stroke();
        draw.fillStyle = '#315eae';
        draw.beginPath();
        for (const node of nodes) {
          draw.moveTo(node.dx + 1.45, node.dy);
          draw.arc(node.dx, node.dy, 1.45, 0, 2 * Math.PI);
        }
        draw.fill();
        if (elapsed < seconds) id = requestAnimationFrame(frame);
        else {
          clearTimeout(fallback);
          finish();
        }
      }
      id = requestAnimationFrame(frame);
    });
  }, { nodeCount, seconds });
  return {
    nodes: nodeCount, edges: nodeCount * 2, canvasWidth: 1000, canvasHeight: 640,
    surface: measured.surface, ...graphSummary(measured.periods), frameDurationsMs: measured.periods,
  };
}

export async function verifyHeldCourse(app, whileRunning) {
  let sent = false;
  const completion = Promise.withResolvers();
  const mock = httpServer((request, response) => {
    if (request.method !== 'POST' || request.url !== '/v1/chat/completions' || sent) {
      response.writeHead(400).end();
      return;
    }
    request.resume();
    request.on('end', () => {
      sent = true;
      void completion.promise.then(() => {
        const route = { stages: [{ name: '评测', lessons: [
          { key: 'local', title: '后台路线检查', outcome: '在窗口隐藏时保存路线' },
        ] }] };
        response.writeHead(200, { 'Content-Type': 'application/json' }).end(
          JSON.stringify({ choices: [{ message: { content: JSON.stringify(route) } }] }),
        );
      });
    });
  });
  await new Promise((resolve, reject) => mock.once('error', reject).listen(0, '127.0.0.1', resolve));
  try {
    const connection = await app.api('/api/models/connections', 'POST', {
      name: '后台评测用模拟接口', provider: 'custom', protocol: 'chat-completions',
      endpoint: 'http://127.0.0.1:' + mock.address().port + '/v1', model: 'isolated-desktop-route',
    });
    await app.api('/api/models/roles', 'PATCH', { main: connection.id });
    const thread = await app.api('/api/conversations', 'POST', {
      id: randomUUID(), message: { id: randomUUID(), text: '窗口隐藏时生成学习路线' },
    });
    const endpoint = '/api/conversations/' + thread.id + '/goal';
    const draft = await app.api(endpoint, 'POST');
    const values = { goalId: draft.id, revision: draft.revision, values: { outcome: '在后台保存学过的阶段' } };
    const scope = await app.api(endpoint + '/model-preview', 'POST', { ...values, maxCalls: 1 });
    const project = await app.api(endpoint + '/submit', 'POST', { ...values, modelConsent: scope.consent });
    await observed(() => sent, '后台 Worker 未发出隔离的样本请求。', 9000);
    await whileRunning();
    completion.resolve();
    const saved = await observed(async () => {
      const result = await app.api('/api/projects/' + project.projectId);
      return result.lessons[0]?.title === '后台路线检查' ? result : null;
    }, '隐藏窗口后没有保存进行中的路线。');
    return saved.id;
  } finally {
    completion.resolve();
    mock.closeAllConnections();
    await new Promise((resolve) => mock.close(resolve));
  }
}
