import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { mkdir, mkdtemp, readFile, rename, rm, writeFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { parseArgs, promisify } from 'node:util';
import { fileURLToPath } from 'node:url';
import { setTimeout as wait } from 'node:timers/promises';
import {
  animateGraph, inspectRecovery, observed, openInstalled, prepareWorkspace, reopenExisting,
  serviceStopped, stopOwned, verifyHeldCourse,
} from './installed-probe.mjs';
import {
  directoryBytes, fileEvidence, installedArchitecture, median, processSample, resourceSummary,
} from './windows-evidence.mjs';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const nativeScript = join(root, 'desktop', 'windows-native.ps1');
const runFile = promisify(execFile);
const graphSizes = [1000, 5000];
const graphSeconds = 6;
const idleSeconds = 5;
const shellNames = { electron: "Electron", tauri: "Tauri" };
const shells = Object.keys(shellNames);
const help = 'node desktop/windows-qualify.mjs --electron <已安装的 Electron exe> --electron-installer <NSIS>'
  + ' --tauri <已安装的 Tauri exe> --tauri-installer <NSIS> [--rounds 3] [--reports <目录>] [--use-tray] [--focus-assist]'
  + '\n单壳预验可加 --only electron 或 --only tauri，只提供对应的应用与安装程序。'
  + '\n--focus-assist 等待操作者点击测试窗口取得焦点，再发送退出快捷键或 Enter。';

let scriptBody;

async function native(mode, app, fields = {}) {
  scriptBody ??= await readFile(nativeScript, 'utf8');
  const payload = Buffer.from(JSON.stringify({
    Mode: mode, RootId: app?.child.pid, WindowTitle: app ? '智学 · ' + shellNames[app.framework] : undefined, ...fields,
  })).toString('base64');
  const command = [
    "$ProgressPreference = 'SilentlyContinue'",
    '[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)',
    'try {',
    "$inputData = ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + payload + "')))",
    '$bound = @{}',
    '$inputData.PSObject.Properties | ForEach-Object { $bound[$_.Name] = $_.Value }',
    '& {', scriptBody, '} @bound',
    '} catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }',
  ].join('\n');
  const encoded = Buffer.from(command, 'utf16le').toString('base64');
  const timeout = fields.FocusAssist ? 50000 : fields.DefaultChoice ? 24000 : 19000;
  try {
    const { stdout } = await runFile('powershell.exe', ['-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded], {
      encoding: 'utf8', windowsHide: true, timeout, maxBuffer: 4 * 1024 * 1024,
    });
    return JSON.parse(stdout.trim());
  } catch (error) {
    const detail = error.stderr?.trim() || (error.killed ? '执行超时' : String(error.code ?? error.name));
    throw new Error('Windows ' + mode + ' 探针失败 · ' + detail, { cause: error });
  }
}

export async function sampleResources(readSample, durationMs, logicalProcessors, { baseline = false } = {}) {
  const snapshots = baseline ? [await readSample()] : [];
  const ends = performance.now() + durationMs;
  do {
    snapshots.push(await readSample());
    if (performance.now() < ends) await wait(600);
  } while (performance.now() < ends);
  return { ...resourceSummary(snapshots, logicalProcessors), observations: snapshots };
}

async function describeInstall(kind, executable, installer) {
  const installation = dirname(executable);
  const sidecar = kind === 'electron'
    ? join(installation, 'resources', 'zhixue-service.exe')
    : join(installation, 'zhixue-service.exe');
  const [architecture, sidecarArchitecture, setup, installedBinary, service, installedBytes, installerLoader] = await Promise.all([
    installedArchitecture(executable), installedArchitecture(sidecar), fileEvidence(installer),
    fileEvidence(executable), fileEvidence(sidecar), directoryBytes(installation),
    installedArchitecture(installer, { setup: true }),
  ]);
  assert.equal(architecture, sidecarArchitecture, '桌面应用与 Python 服务的 PE 架构不一致。');
  return { architecture, setup, installerLoader, installedBinary, service, installedBytes };
}

async function requestExit(app, focusAssist) {
  if (focusAssist) console.log('请在 30 秒内点击智学测试主窗口。检测到焦点后将发送 Ctrl+Shift+Q。');
  await native('quit', app, { FocusAssist: focusAssist });
}

async function timerDialog(app, choice, { useDefault = false, focusAssist = false } = {}) {
  if (focusAssist) console.log('请点击「智学 · 完全退出」对话框的标题栏，保留默认选项。检测到焦点后将发送 Enter。');
  await native('dialog', app, {
    DialogTitle: '智学 · 完全退出', Choice: choice, DefaultChoice: useDefault, FocusAssist: focusAssist,
  });
}

async function normalExit(app, focusAssist) {
  await requestExit(app, focusAssist);
  await checkExit(app);
}

async function checkExit(app) {
  await observed(app.ended, '完全退出后桌面进程仍在运行。', 14000);
  assert.equal((await app.outcome).code, 0, '桌面应用未正常退出。');
  await serviceStopped(app);
}

async function showAgain(app, executable, useTray) {
  if (useTray) {
    console.log('请现在点击 Windows 通知区域的「智学 · ' + (app.framework === 'tauri' ? 'Tauri' : 'Electron')
      + '」图标。若图标在折叠区，请先展开通知区域。');
  } else {
    await reopenExisting(executable, app);
  }
  await observed(async () => (await native('visible', app)).visible, '主窗口没有恢复。',
    useTray ? 45000 : 12000);
  return useTray ? '通知区域操作后观察到窗口恢复' : '再次启动同一应用后窗口恢复';
}

async function trial(artifact, args, machine, round, result) {
  const focusAssist = Boolean(args['focus-assist']);
  const scratch = await mkdtemp(join(tmpdir(), 'zhixue-win-qualify-'));
  const workspace = join(scratch, '学习数据');
  const webProfile = join(scratch, 'WebView2');
  await mkdir(workspace);
  let app;
  const launch = async (firstUse) => {
    app = await openInstalled(artifact.executable, artifact.kind, workspace, webProfile, firstUse);
    await observed(async () => (await native('visible', app)).visible, '本地页面就绪后没有观察到可见的主窗口。');
    app.shownMs = Math.round(performance.now() - app.began);
    return app;
  };
  try {
    await launch(true);
    result.coldSetupMs = app.shownMs;
    result.renderedReadyMs = app.readyMs;
    result.engine = app.renderEngine;
    result.viewport = app.viewport;
    const sample = await prepareWorkspace(app, workspace);
    result.filesAndWorker = '通过';
    await wait(1000);
    const readSample = async () => {
      assert(!app.ended(), '桌面进程在资源测量时退出。');
      const raw = await native('snapshot', app, { WebProfile: webProfile });
      return processSample(raw, app.child.pid);
    };
    result.idle = await sampleResources(readSample, idleSeconds * 1000, machine.logical, { baseline: true });
    for (const nodes of graphSizes) {
      const [frames, resources] = await Promise.all([
        animateGraph(app.page, nodes, graphSeconds),
        sampleResources(readSample, graphSeconds * 1000, machine.logical),
      ]);
      result.graph.push({ ...frames, resources });
    }
    if (round === 1) {
      const startTimer = async () => (await app.api('/api/timer', 'POST', {
        action: 'start', lessonId: sample.lessonId,
      })).active;
      const timer = await startTimer();
      const checkActiveTimer = async () => {
        const status = (await app.api('/api/timer')).active;
        assert.equal(status?.id, timer.id, '关闭窗口或取消退出改变了计时记录。');
        assert.equal(status.status, 'running', '关闭窗口或取消退出暂停了计时。');
      };
      const held = await verifyHeldCourse(app, async () => {
        await native('close', app);
        await observed(async () => !(await native('visible', app)).visible, '关闭按钮没有隐藏窗口。');
        await checkActiveTimer();
      });
      result.native.hiddenWorker = '窗口隐藏后保存了已发出的后台路线，计时继续。';
      result.native.reopen = await showAgain(app, artifact.executable, args['use-tray']);
      result.native.tray = args['use-tray'] ? '观察到恢复' : '未验证';
      await checkActiveTimer();
      await requestExit(app, focusAssist);
      await timerDialog(app, '取消');
      await wait(400);
      await checkActiveTimer();
      result.native.cancel = '原计时继续，桌面应用仍可访问。';

      await requestExit(app, focusAssist);
      await timerDialog(app, '暂存并退出');
      await checkExit(app);
      await launch(false);
      const stashed = await app.api('/api/timer');
      assert(stashed.active === null && stashed.stashed.some((entry) => entry.id === timer.id),
        '重启后没有发现同一笔暂存计时。');
      result.native.stash = '重启读到原暂存记录。';
      await inspectRecovery(app, sample);

      const second = await startTimer();
      await requestExit(app, focusAssist);
      await timerDialog(app, '结束计时并退出', { useDefault: true, focusAssist });
      await checkExit(app);
      await launch(false);
      const finished = await app.api('/api/timer');
      assert(finished.active === null && finished.lastSaved?.id === second.id, '默认退出后没有保存同一笔计时。');
      result.native.defaultChoice = '按 Enter 激活默认选项后结束计时，重启读到同一笔记录。';

      await stopOwned(app, 'SIGKILL');
      await serviceStopped(app);
      await launch(false);
      result.homeAfterInterruptionMs = app.shownMs;
      await inspectRecovery(app, sample);
      assert.equal((await app.api('/api/projects/' + held)).lessons[0]?.title, '后台路线检查');
      assert.equal((await app.api('/api/timer')).lastSaved?.id, second.id);
      result.native.recovery = '终止评测进程后恢复原资料、课程笔记、后台路线及已保存计时。';
    }
    await normalExit(app, focusAssist);
    result.native.exit = '桌面进程和旧本地服务已退出。';
    result.state = 'measured';
  } finally {
    if (app && !app.ended()) await stopOwned(app);
    if (app) await serviceStopped(app);
    await rm(scratch, { recursive: true, force: true });
  }
}

export function windowsReportMarkdown(report) {
  const frameworks = shells.filter((kind) => report.artifacts[kind]);
  const only = frameworks.length === 1 ? shellNames[frameworks[0]] : null;
  const columns = frameworks.map((kind) => shellNames[kind]);
  const bytes = (value) => value == null ? '未测' : (value / 1048576).toFixed(1) + ' MiB';
  const value = (number, unit) => number == null ? '未测' : String(number) + unit;
  const resourceFields = [
    ['workingSetPeakBytes', '工作集峰值', bytes],
    ['privatePeakBytes', '私有内存峰值', bytes],
    ['cpuMeanPercent', 'CPU 平均占用', (sample) => value(sample, '%')],
    ['gpuPeakPercent', '最忙 GPU 引擎峰值', (sample) => value(sample, '%')],
  ];
  const graphFields = [
    ['meanFps', '平均帧率', (sample) => value(sample, ' fps')],
    ['p95FrameMs', 'P95 帧间隔', (sample) => value(sample, ' ms')],
    ['framesOver50ms', '超过 50 ms 帧数', (sample) => value(sample, '')],
  ];
  const describe = (samples, fields) => fields
    .filter(([key]) => Number.isFinite(samples?.[key]))
    .map(([key, label, format]) => label + ' ' + format(samples[key]));
  const byKind = (kind) => report.trials.filter((trial) => trial.framework === kind && trial.state === 'measured');
  const get = (kind, select) => median(byKind(kind).map(select).filter(Number.isFinite));
  const lines = [
    '# 智学 Windows 桌面' + (only ? ' ' + only + ' 单壳预验' : '双壳实测'), '',
    '- 操作系统 ' + report.machine.os + '，构建 ' + report.machine.build,
    '- 可执行文件架构 ' + report.architecture + '，CPU 逻辑处理器 ' + report.machine.logical,
    '- 测量始于 ' + report.startedAt, '- 结束状态 ' + (report.state === 'complete' ? '采集完成' : report.state), '',
    ...(only ? ['本次采集 ' + only + '，同机对比待双壳采集。', ''] : []),
    ...(report.policy?.focusAssistRequested ? ['键盘焦点协助已启用。发送按键前仍须确认测试窗口持有焦点。', ''] : []),
    '## 完成轮次汇总', '',
    '| 指标 | ' + columns.join(' | ') + ' |', '| --- | ' + columns.map(() => '---:').join(' | ') + ' |',
  ];
  const row = (label, select) => lines.push('| ' + [label, ...frameworks.map(select)].join(' | ') + ' |');
  row('安装程序大小', (kind) => bytes(report.artifacts[kind].setup.bytes));
  row('安装目录大小', (kind) => bytes(report.artifacts[kind].installedBytes));
  row('完成轮数', (kind) => byKind(kind).length);
  row('空白数据进程启动至首启界面', (kind) => value(get(kind, (item) => item.coldSetupMs), ' ms'));
  for (const [field, label, format] of resourceFields) {
    const gap = field === 'cpuMeanPercent' ? ' ' : '';
    row('空闲' + gap + label, (kind) => format(get(kind, (item) => item.idle?.[field])));
  }
  for (const nodes of report.comparability?.sameGraphSurface === false ? [] : graphSizes) {
    const graphSample = (item) => item.graph?.find((graph) => graph.nodes === nodes);
    for (const [field, label, format] of graphFields) {
      const gap = field === 'p95FrameMs' ? ' ' : '';
      row(nodes + ' 节点' + gap + label, (kind) => format(get(kind, (item) => graphSample(item)?.[field])));
    }
    for (const [field, label, format] of resourceFields.filter(([field]) => field !== 'privatePeakBytes')) {
      const gap = field === 'cpuMeanPercent' ? ' ' : '';
      row(nodes + ' 节点' + gap + label, (kind) => format(get(kind, (item) => graphSample(item)?.resources?.[field])));
    }
  }
  lines.push('', '## 功能与失败记录', '');
  if (report.comparability?.sameGraphSurface === false) {
    lines.push('图谱可见尺寸或像素比不一致。图谱帧率和资源原始值保存在 JSON 中。', '');
  }
  if (report.comparability?.nativeTrayVerified === false) lines.push('通知区域图标恢复尚未完成实机观察。', '');
  for (const item of report.trials) {
    lines.push('- ' + item.framework + ' 第 ' + item.round + ' 轮 · ' + item.state);
    if (item.error) lines.push('  - ' + item.error);
    if (item.state !== 'measured') {
      const startup = describe(item, [['coldSetupMs', '首启', (sample) => value(sample, ' ms')]]);
      const idle = describe(item.idle, resourceFields);
      const graphs = (item.graph ?? []).flatMap((graph) => {
        const samples = [...describe(graph, graphFields), ...describe(graph.resources, resourceFields)];
        if (!samples.length) return [];
        const { width, height, dpr } = graph.surface ?? {};
        const surface = [width, height, dpr].every(Number.isFinite)
          ? '可见画布 ' + width + ' × ' + height + '，像素比 ' + dpr + '，' : '';
        return ['    - ' + graph.nodes + ' 节点 · ' + surface + samples.join('，')];
      });
      if (startup.length || idle.length || graphs.length) {
        lines.push('  - 已取得的采样' + (startup.length ? ' · ' + startup.join('，') : ''));
        if (idle.length) lines.push('    - 空闲 · ' + idle.join('，'));
        lines.push(...graphs);
      }
    }
    if (item.filesAndWorker) lines.push('  - 真实文件读写、外部改动和 Worker 出课通过。');
    for (const [check, outcome] of Object.entries(item.native)) {
      const labels = {
        hiddenWorker: '关闭与后台', reopen: '恢复窗口', tray: '通知区域图标', cancel: '取消退出',
        stash: '暂存计时', defaultChoice: '默认计时退出', recovery: '中断恢复', exit: '完全退出',
      };
      lines.push('  - ' + labels[check] + ' · ' + outcome);
    }
  }
  lines.push('', '表格使用各成功轮数据的中位数。完整原始结果、安装包散列和采样覆盖保存于 JSON。');
  return lines.join('\n') + '\n';
}

async function main() {
  const { values: args } = parseArgs({
    options: {
      electron: { type: 'string' }, tauri: { type: 'string' }, 'electron-installer': { type: 'string' },
      'tauri-installer': { type: 'string' }, only: { type: 'string' },
      reports: { type: 'string' }, rounds: { type: 'string', default: '3' },
      'use-tray': { type: 'boolean' }, 'focus-assist': { type: 'boolean' }, help: { type: 'boolean', short: 'h' },
    },
  });
  if (args.help) {
    console.log(help);
    return;
  }
  if (process.platform !== 'win32') throw new Error('需要在原生 Windows 桌面会话中执行。');
  const rounds = Number(args.rounds);
  assert(Number.isSafeInteger(rounds) && rounds >= 1, '轮数必须是正整数。');
  assert(args.only === undefined || shells.includes(args.only), '--only 必须是 electron 或 tauri。');
  const frameworks = args.only ? [args.only] : shells;
  for (const kind of shells) {
    if (frameworks.includes(kind)) {
      assert(args[kind] && args[kind + '-installer'], '需要提供 ' + shellNames[kind] + ' 的安装程序与实际安装的应用。');
    } else {
      assert(args[kind] === undefined && args[kind + '-installer'] === undefined, '--only 只接受对应外壳的参数。');
    }
  }
  const installs = Object.fromEntries(await Promise.all(frameworks.map(async (kind) => {
    const executable = resolve(args[kind]);
    return [kind, { kind, executable, evidence: await describeInstall(kind, executable, resolve(args[kind + '-installer'])) }];
  })));
  if (!args.only) {
    assert.notEqual(installs.electron.executable.toLowerCase(), installs.tauri.executable.toLowerCase(),
      '两个框架必须使用各自安装的应用。');
    assert.equal(installs.electron.evidence.architecture, installs.tauri.evidence.architecture, '两个框架的可执行文件架构不同。');
    assert.equal(installs.electron.evidence.service.sha256, installs.tauri.evidence.service.sha256,
      '两个框架没有安装同一份本地服务。');
  }
  const machine = await native('machine');
  const startedAt = new Date().toISOString();
  const directory = resolve(args.reports ?? join(root, 'release', 'windows-evaluation'));
  await mkdir(directory, { recursive: true });
  const reportFolder = await mkdtemp(join(directory, startedAt.replace(/[:.]/g, '-') + '-'));
  const report = {
    state: 'running', startedAt, machine, architecture: installs[frameworks[0]].evidence.architecture,
    policy: { rounds, graphSeconds, idleSeconds, trayRequested: Boolean(args['use-tray']),
      focusAssistRequested: Boolean(args['focus-assist']),
      ...(args.only ? { only: args.only } : {}) },
    artifacts: Object.fromEntries(frameworks.map((kind) => [kind, installs[kind].evidence])), trials: [],
  };
  const persist = async () => {
    for (const [name, content] of [
      ['windows-evaluation.json', JSON.stringify(report, null, 2) + '\n'],
      ['windows-evaluation.md', windowsReportMarkdown(report)],
    ]) {
      const target = join(reportFolder, name);
      const temporary = target + '.tmp';
      await writeFile(temporary, content, 'utf8');
      await rename(temporary, target);
    }
  };

  console.log('保存评测结果至 ' + reportFolder);
  await persist();
  for (let round = 1; round <= rounds; round++) {
    const order = round % 2 ? frameworks : [...frameworks].reverse();
    for (const framework of order) {
      console.log(framework + ' 第 ' + round + ' 轮');
      const result = { framework, round, state: 'running', graph: [], native: {} };
      report.trials.push(result);
      await persist();
      try {
        await trial(installs[framework], args, machine, round, result);
      } catch (error) {
        result.state = 'failed';
        result.error = String(error.message ?? '运行出错。').replace(/\s+/g, ' ').replace(/\|/g, '/').slice(0, 400);
        console.error(result.error);
      }
      await persist();
    }
  }
  report.finishedAt = new Date().toISOString();
  const measured = report.trials.filter((item) => item.state === 'measured');
  const graphSurfaces = new Set(measured.flatMap((item) => item.graph.map((graph) => JSON.stringify(graph.surface))));
  const sameGraphSurface = graphSurfaces.size ? graphSurfaces.size === 1 : null;
  report.comparability = {
    sameGraphSurface, nativeTrayVerified: frameworks.every((framework) =>
      report.trials.some((item) => item.framework === framework && item.native.tray === '观察到恢复')),
  };
  report.state = report.trials.every((item) => item.state === 'measured') && sameGraphSurface ? 'complete' : 'incomplete';
  await persist();
  if (report.state !== 'complete') process.exitCode = 1;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    await main();
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
