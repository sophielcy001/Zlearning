import { createHash } from 'node:crypto';
import { createReadStream } from 'node:fs';
import { open, readdir, stat } from 'node:fs/promises';
import { basename, join } from 'node:path';

const machines = new Map([[0x8664, 'x64'], [0xaa64, 'arm64']]);
const round = (number) => Math.round(number * 10) / 10;

function peOffset(bytes) {
  if (bytes.length < 64 || bytes.toString('ascii', 0, 2) !== 'MZ') throw new Error('可执行文件缺少 Windows PE 头。');
  const offset = bytes.readUInt32LE(0x3c);
  if (offset < 64) throw new Error('Windows PE 头的位置无效。');
  return offset;
}

function peMachine(bytes, setup = false) {
  if (bytes.length < 6 || bytes.readUInt32LE(0) !== 0x00004550) throw new Error('Windows PE 签名无效。');
  const identifier = bytes.readUInt16LE(4);
  if (setup && identifier === 0x014c) return 'x86';
  const architecture = machines.get(identifier);
  if (!architecture) throw new Error('只支持原生 x64 或 ARM64 智学可执行文件。');
  return architecture;
}

export function windowsArchitecture(bytes) {
  const offset = peOffset(bytes);
  return peMachine(bytes.subarray(offset, offset + 6));
}

export async function installedArchitecture(file, { setup = false } = {}) {
  const descriptor = await open(file);
  try {
    const head = Buffer.alloc(64);
    const read = await descriptor.read(head, 0, head.length, 0);
    const header = Buffer.alloc(6);
    const next = await descriptor.read(header, 0, 6, peOffset(head.subarray(0, read.bytesRead)));
    return peMachine(header.subarray(0, next.bytesRead), setup);
  } finally {
    await descriptor.close();
  }
}

export async function fileEvidence(file) {
  const metadata = await stat(file);
  if (!metadata.isFile()) throw new Error('需要指定实际的文件。');
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(file)) hash.update(chunk);
  return { name: basename(file), bytes: metadata.size, sha256: hash.digest('hex') };
}

export async function directoryBytes(directory) {
  let size = 0;
  for (const item of await readdir(directory, { withFileTypes: true })) {
    const target = join(directory, item.name);
    if (item.isDirectory()) size += await directoryBytes(target);
    if (item.isFile()) size += (await stat(target)).size;
  }
  return size;
}

export function processSample({ rows, gpu, at }, rootId) {
  const root = rows.find((row) => row.pid === rootId);
  if (!root) throw new Error('采样时未发现评测进程。');
  const parents = new Map(rows.map((row) => [row.pid, row]));
  const owned = new Set([rootId]);
  for (const row of rows) {
    if (row.webview && row.born >= root.born) owned.add(row.pid);
  }
  let added;
  do {
    added = false;
    for (const row of rows) {
      if (!owned.has(row.pid) && owned.has(row.parent) && row.born >= parents.get(row.parent).born) {
        owned.add(row.pid);
        added = true;
      }
    }
  } while (added);
  const members = rows.filter((row) => owned.has(row.pid));
  let gpuPercent = null;
  if (gpu !== null) {
    const engines = new Map();
    for (const row of gpu) {
      if (owned.has(row.pid)) engines.set(row.engine, (engines.get(row.engine) ?? 0) + row.percent);
    }
    gpuPercent = round(Math.min(100, Math.max(0, ...engines.values())));
  }
  return {
    at, members, gpuPercent,
    working: members.reduce((sum, row) => sum + row.working, 0),
    privateBytes: members.reduce((sum, row) => sum + row.privateBytes, 0),
  };
}

export function resourceSummary(samples, logicalProcessors) {
  let observedCpu = 0;
  let interval = 0;
  for (const [index, current] of samples.entries()) {
    const previous = samples[index - 1];
    if (!previous || current.at <= previous.at) continue;
    const previousPids = new Map(previous.members.map((row) => [row.pid, row]));
    for (const member of current.members) {
      const earlier = previousPids.get(member.pid);
      if (earlier && earlier.born === member.born) {
        observedCpu += Math.max(0, member.cpuMs - earlier.cpuMs);
      } else if (member.born >= previous.at && member.born <= current.at) {
        observedCpu += member.cpuMs;
      }
    }
    interval += current.at - previous.at;
  }
  const gpu = samples.map((sample) => sample.gpuPercent).filter((number) => number !== null);
  return {
    samples: samples.length,
    observedIntervalMs: interval,
    cpuMeanPercent: interval && logicalProcessors ? round(100 * observedCpu / interval / logicalProcessors) : null,
    workingSetPeakBytes: samples.length ? Math.max(...samples.map((sample) => sample.working)) : null,
    privatePeakBytes: samples.length ? Math.max(...samples.map((sample) => sample.privateBytes)) : null,
    gpuPeakPercent: gpu.length ? Math.max(...gpu) : null,
    processNames: [...new Set(samples.flatMap((sample) => sample.members.map((row) => row.name).filter(Boolean)))].sort(),
  };
}

export function median(values) {
  const sorted = values.toSorted((a, b) => a - b);
  if (!sorted.length) return null;
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : round((sorted[middle - 1] + sorted[middle]) / 2);
}

export function graphSummary(periods) {
  const duration = periods.reduce((sum, elapsed) => sum + elapsed, 0);
  const sorted = periods.toSorted((a, b) => a - b);
  if (!periods.length || !duration) throw new Error('图谱窗口没有收集到可用帧。');
  return {
    frames: periods.length, durationMs: round(duration), meanFps: round(periods.length * 1000 / duration),
    p95FrameMs: round(sorted[Math.ceil(sorted.length * .95) - 1]),
    framesOver50ms: periods.filter((period) => period > 50).length,
  };
}
