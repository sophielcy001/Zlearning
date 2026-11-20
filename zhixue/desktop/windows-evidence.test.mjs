import assert from 'node:assert/strict';
import { test } from 'node:test';
import { graphSummary, processSample, resourceSummary, windowsArchitecture } from './windows-evidence.mjs';

function pe(machine, offset = 128) {
  const binary = Buffer.alloc(256);
  binary.write('MZ');
  binary.writeUInt32LE(offset, 0x3c);
  binary.write('PE\0\0', 128);
  binary.writeUInt16LE(machine, 132);
  return binary;
}

test('recognizes native PE architectures and rejects mislabeled or broken artifacts', () => {
  assert.equal(windowsArchitecture(pe(0x8664)), 'x64');
  assert.equal(windowsArchitecture(pe(0xaa64)), 'arm64');
  assert.throws(() => windowsArchitecture(pe(0x014c)));
  assert.throws(() => windowsArchitecture(pe(0x8664, 0xfffffff0)));
  assert.throws(() => windowsArchitecture(Buffer.alloc(8)));
});

const proc = (pid, parent, born, cpuMs, working, privateBytes, webview = false) =>
  ({ pid, parent, born, cpuMs, working, privateBytes, webview });

test('measures only the owned family, including validated detached WebView2 processes', () => {
  const rows = [
    proc(10, 1, 500, 20, 20, 12),
    proc(11, 10, 510, 30, 30, 18),
    proc(12, 11, 530, 10, 5, 4),
    proc(13, 10, 300, 150, 80, 44),
    proc(14, 1, 540, 11, 12, 6, true),
    proc(15, 14, 568, 11, 8, 5),
    proc(16, 1, 550, 11, 20, 8),
  ];
  const gpu = [
    { pid: 11, engine: 'physical_0_engine_0', percent: 10 },
    { pid: 14, engine: 'physical_0_engine_0', percent: 15 },
    { pid: 12, engine: 'physical_0_engine_1', percent: 14 },
    { pid: 16, engine: 'physical_0_engine_0', percent: 80 },
  ];
  const sample = processSample({ rows, gpu, at: 1000 }, 10);
  assert.deepEqual(sample.members.map((row) => row.pid), [10, 11, 12, 14, 15]);
  assert.equal(sample.working, 75);
  assert.equal(sample.privateBytes, 45);
  assert.equal(sample.gpuPercent, 25);
  assert.equal(processSample({ rows, gpu: null, at: 1000 }, 10).gpuPercent, null);
  assert.throws(() => processSample({ rows, gpu, at: 1000 }, 20));
});

test('normalizes CPU by interval and logical cores and treats missing GPU as unavailable', () => {
  const previous = processSample({ at: 1000, rows: [proc(10, 5, 500, 250, 60, 30)], gpu: null }, 10);
  const latest = processSample({ at: 2000, rows: [proc(10, 5, 500, 750, 100, 40)], gpu: null }, 10);
  const result = resourceSummary([previous, latest], 4);
  assert.equal(result.cpuMeanPercent, 12.5);
  assert.equal(result.workingSetPeakBytes, 100);
  assert.equal(result.privatePeakBytes, 40);
  assert.equal(result.gpuPeakPercent, null);
  assert.equal(resourceSummary([latest], 4).cpuMeanPercent, null);
  assert.deepEqual(graphSummary([16, 16, 68, 20]), {
    frames: 4, durationMs: 120, meanFps: 33.3, p95FrameMs: 68, framesOver50ms: 1,
  });
});

test('Windows report suppresses graph comparisons for unlike drawing surfaces', async () => {
  const { windowsReportMarkdown } = await import('./windows-qualify.mjs');
  const trial = {
    state: 'measured', round: 1, coldSetupMs: 500, idle: { gpuPeakPercent: null },
    graph: [{ nodes: 1000, meanFps: 60, resources: {} }], native: { tray: '未验证' },
  };
  const fake = { setup: { bytes: 1048576 }, installedBytes: 2000000 };
  const report = {
    state: 'incomplete', startedAt: '2026-01-01T00:00:00Z', architecture: 'x64',
    machine: { os: 'Test Windows', build: 'fixture', logical: 8 },
    artifacts: { electron: fake, tauri: fake },
    trials: ['electron', 'tauri'].map((framework) => ({ ...trial, framework })),
    comparability: { sameGraphSurface: false, nativeTrayVerified: false },
  };
  const text = windowsReportMarkdown(report);
  assert.match(text, /空闲最忙 GPU 引擎峰值 \| 未测 \| 未测/);
  assert.match(text, /图谱可见尺寸或像素比不一致/);
  assert.match(text, /通知区域图标恢复尚未完成实机观察/);
  assert.doesNotMatch(text, /1000 节点平均帧率/);
});

test('single-shell evidence reports only the measured application and labels the scope', async () => {
  const { windowsReportMarkdown } = await import('./windows-qualify.mjs');
  const report = {
    state: 'complete', startedAt: '2026-01-01T00:00:00Z', architecture: 'x64',
    machine: { os: 'Test Windows', build: 'fixture', logical: 8 },
    policy: { only: 'tauri' },
    artifacts: { tauri: { setup: { bytes: 1048576 }, installedBytes: 2000000 } },
    trials: [{ framework: 'tauri', state: 'measured', round: 1, coldSetupMs: 500,
      idle: {}, graph: [], native: {} }],
    comparability: { sameGraphSurface: true, nativeTrayVerified: false },
  };
  const text = windowsReportMarkdown(report);
  assert.match(text, /^# 智学 Windows 桌面 Tauri 单壳预验/);
  assert.match(text, /\| 指标 \| Tauri \|/);
  assert.match(text, /\| 完成轮数 \| 1 \|/);
  assert.match(text, /同机对比待双壳采集/);
  assert.doesNotMatch(text, /\| Electron \|/);
});

test('keyboard focus assistance is opt-in and recorded in the Windows evaluation', async () => {
  const { execFileSync } = await import('node:child_process');
  const { fileURLToPath } = await import('node:url');
  const { windowsReportMarkdown } = await import('./windows-qualify.mjs');
  const script = fileURLToPath(new URL('./windows-qualify.mjs', import.meta.url));
  assert.match(execFileSync(process.execPath, [script, '--focus-assist', '--help'], { encoding: 'utf8' }), /--focus-assist/);
  const report = {
    state: 'incomplete', startedAt: '2026-01-01T00:00:00Z', architecture: 'x64',
    machine: { os: 'Test Windows', build: 'fixture', logical: 8 },
    policy: { only: 'electron', focusAssistRequested: true },
    artifacts: { electron: { setup: { bytes: 1048576 }, installedBytes: 2000000 } },
    trials: [],
    comparability: { sameGraphSurface: null, nativeTrayVerified: false },
  };
  assert.match(windowsReportMarkdown(report), /键盘焦点协助已启用/);
  assert.doesNotMatch(windowsReportMarkdown({ ...report, policy: { only: 'electron' } }), /键盘焦点协助已启用/);
});

test('partial Windows samples remain visible without entering the successful-round median', async () => {
  const { windowsReportMarkdown } = await import('./windows-qualify.mjs');
  const report = {
    state: 'incomplete', startedAt: '2026-01-01T00:00:00Z', architecture: 'x64',
    machine: { os: 'Test Windows', build: 'fixture', logical: 8 },
    artifacts: { electron: { setup: { bytes: 1048576 }, installedBytes: 2000000 } },
    trials: [
      { framework: 'electron', round: 1, state: 'failed', coldSetupMs: 1635,
        idle: { workingSetPeakBytes: 2097152, cpuMeanPercent: 0 },
        graph: [{ nodes: 1000, meanFps: 87.6, framesOver50ms: 0, surface: { width: 1000, height: 640, dpr: 1 },
          resources: { gpuPeakPercent: null } }],
        native: { tray: '未验证' }, error: '键盘焦点未取得' },
      { framework: 'electron', round: 2, state: 'measured', coldSetupMs: 1500, idle: {}, graph: [], native: {} },
      { framework: 'electron', round: 3, state: 'failed', graph: [], native: {}, error: '首启未完成' },
    ],
    comparability: { sameGraphSurface: false, nativeTrayVerified: false },
  };
  const text = windowsReportMarkdown(report);
  assert.match(text, /\| 完成轮数 \| 1 \|/);
  assert.match(text, /\| 空白数据进程启动至首启界面 \| 1500 ms \|/);
  assert.match(text, /已取得的采样.*1635 ms/);
  assert.match(text, /空闲.*2\.0 MiB.*CPU 平均占用 0%/);
  assert.match(text, /1000 节点.*1000 × 640.*87\.6 fps.*超过 50 ms 帧数 0/);
  assert.doesNotMatch(text, /最忙 GPU 引擎峰值 (?:null|0%)/);
  assert.equal((text.match(/已取得的采样/g) || []).length, 1);
});


test('idle CPU still has a sampling interval after a slow first Windows snapshot', async () => {
  const { setTimeout: wait } = await import('node:timers/promises');
  const { sampleResources } = await import('./windows-qualify.mjs');
  let snapshots = 0;
  const readSample = async () => {
    const index = snapshots++;
    if (index === 0) await wait(45);
    return {
      at: 1000 + 600 * index, working: 1234, privateBytes: 800, gpuPercent: null,
      members: [{ pid: 123, born: 100, cpuMs: 12 * index }],
    };
  };

  const result = await sampleResources(readSample, 20, 4, { baseline: true });
  assert.equal(result.samples, 2);
  assert.equal(result.observedIntervalMs, 600);
  assert.equal(result.cpuMeanPercent, 0.5);
});
