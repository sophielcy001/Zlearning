import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, realpathSync,
  rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { test } from 'node:test';

const migration = 'the installed Electron app migrates, completes a review, and restores to a fresh workspace';
const windowRoundtrip = 'Electron hides and reopens the same window, then releases its service on quit';
const restoreRecovery = 'the installed Electron app recovers workspace switches across restarts';
const backupSelection = 'the installed Electron app finds a renamed backup past a same-size damaged copy';
const recognitionAuthorization = 'the installed Electron app enforces single-page recognition authorization';
const windowsOnly = 'installed Windows Electron recovers encrypted restores before and after commit';
const checksum = value => createHash('sha256').update(value).digest('hex');
const spec = (title, status = 'expected') => ({ id: title, title, tests: [{ projectId: 'fixture', status }] });

async function fixture(action) {
  const parent = realpathSync(tmpdir());
  const work = mkdtempSync(join(parent, 'zhixue-mac-qualify-test-'));
  try {
    const paths = {
      currentImage: join(work, 'Apple silicon release.dmg'),
      r2Image: join(work, 'r2.dmg'),
      legacyZip: join(work, 'legacy fixture.zip'),
      reports: join(work, 'existing reports'),
    };
    const inputs = { currentImage: 'current', r2Image: 'r2', legacyZip: 'archive', legacyService: 'service from 0051' };
    for (const key of ['currentImage', 'r2Image', 'legacyZip']) writeFileSync(paths[key], inputs[key]);
    writeFileSync(paths.currentImage + '.sha256', checksum(inputs.currentImage) + '  Apple silicon release.dmg\n');
    mkdirSync(paths.reports);
    writeFileSync(join(paths.reports, 'earlier.txt'), 'preserve this file');
    const fixtures = {
      r2Image: checksum(inputs.r2Image), legacyZip: checksum(inputs.legacyZip),
      legacyService: checksum(inputs.legacyService),
    };
    const calls = [];
    let mount, source;
    const run = (tool, args, fd) => {
      calls.push([tool, args]);
      if (tool === 'unzip') writeFileSync(fd, inputs.legacyService);
      if (tool === 'readlink') return '/Applications';
      if (tool === 'lipo') return 'arm64';
      if (tool === 'hdiutil' && args[0] === 'attach') {
        source = args[1];
        mount = args.at(-1);
        const contents = join(mount, 'Zhixue Electron.app', 'Contents');
        mkdirSync(join(contents, 'Resources'), { recursive: true });
        mkdirSync(join(contents, 'MacOS'));
        writeFileSync(join(contents, 'Resources', 'zhixue-service'), readFileSync(source));
        writeFileSync(join(contents, 'MacOS', 'Zhixue Electron'), readFileSync(source));
      }
      if (tool === 'ditto') {
        for (const parts of [['MacOS', 'Zhixue Electron'], ['Resources', 'zhixue-service']]) {
          const destination = join(args[1], 'Contents', ...parts);
          mkdirSync(dirname(destination), { recursive: true });
          copyFileSync(join(args[0], 'Contents', ...parts), destination);
        }
      }
      if (tool === 'hdiutil' && args[0] === 'detach') {
        const mountedApp = realpathSync(join(mount, 'Zhixue Electron.app'));
        assert.equal(dirname(mountedApp), realpathSync(mount));
        rmSync(mountedApp, { recursive: true });
      }
      return '';
    };
    const nativeRuns = [];
    const options = {
      fixtures,
      run,
      async startInstalled(app) {
        assert.equal(readFileSync(app, 'utf8'), 'current');
        assert(!existsSync(mount));
        nativeRuns.push('current-smoke');
      },
      async runTests({ phase, executable, legacyService, discovery = false }) {
        assert.equal(readFileSync(executable, 'utf8'), phase === 'r2' ? 'r2' : 'current');
        assert.equal(readFileSync(legacyService, 'utf8'), inputs.legacyService);
        assert(!existsSync(mount));
        if (!discovery) nativeRuns.push(phase);
        const entries = phase === 'r2' ? [spec(migration)] : [
          spec(migration), spec(windowRoundtrip), spec(windowsOnly, 'skipped'), spec(restoreRecovery),
          spec(recognitionAuthorization), spec(backupSelection),
        ];
        return {
          stats: { expected: phase === 'r2' ? 1 : 5, unexpected: 0, flaky: 0, skipped: phase === 'r2' ? 0 : 1 },
          suites: [{ specs: entries }],
        };
      },
    };
    await action(paths, options, calls, nativeRuns);
  } finally {
    assert.equal(dirname(realpathSync(work)), parent);
    rmSync(work, { recursive: true });
  }
}

function savedReport(parent) {
  const [name] = readdirSync(parent).filter(name => name.startsWith('mac-qualification-'));
  return JSON.parse(readFileSync(join(parent, name, 'qualification.json'), 'utf8'));
}

test('binds both native phases to the verified image copies and original 0051 service', async () => {
  const { qualifyMac } = await import('./mac-qualify.mjs');
  await fixture(async (paths, options, calls, nativeRuns) => {
    const output = await qualifyMac(paths, options);
    assert.deepEqual(nativeRuns, ['r2', 'current-smoke', 'current']);
    assert.deepEqual(savedReport(paths.reports), output.result);
    assert.equal(output.result.status, 'passed');
    assert.equal(output.result.inputs.legacyServiceSHA256, options.fixtures.legacyService);
    assert.equal(output.result.phases.r2.bundledServiceSHA256, checksum('r2'));
    assert.equal(output.result.phases.current.bundledServiceSHA256, checksum('current'));
    assert.equal(output.result.phases.current.tests.skipped, 1);
    assert.equal(output.result.phases.current.tests.discovered, 6);
    assert(output.result.phases.current.tests.passedTests.includes(windowRoundtrip));
    assert(output.result.phases.current.tests.passedTests.includes(backupSelection));
    assert(calls.some(([tool, args]) => tool === 'codesign' && args.at(-1).endsWith('zhixue-service-0051')));
    assert.equal(readFileSync(join(paths.reports, 'earlier.txt'), 'utf8'), 'preserve this file');
    const temporary = calls.find(([tool]) => tool === 'unzip')[1];
    assert(temporary.includes(paths.legacyZip));
  });
});

test('rejects a changed candidate before extraction or mount and saves the failure', async () => {
  const { qualifyMac } = await import('./mac-qualify.mjs');
  await fixture(async (paths, options, calls) => {
    writeFileSync(paths.currentImage, 'changed after publishing checksum');
    await assert.rejects(qualifyMac(paths, options), /Current DMG checksum mismatch/);
    assert.deepEqual(calls, []);
    assert.equal(savedReport(paths.reports).status, 'failed');
  });
});

test('stops before mounting when the extracted 0051 service has the wrong digest', async () => {
  const { qualifyMac } = await import('./mac-qualify.mjs');
  await fixture(async (paths, options, calls) => {
    options.run = (tool, args, fd) => {
      calls.push([tool, args]);
      writeFileSync(fd, 'another service');
    };
    await assert.rejects(qualifyMac(paths, options), /Original 0051 service checksum mismatch/);
    assert.deepEqual(calls.map(([tool]) => tool), ['unzip']);
    assert.equal(savedReport(paths.reports).error.step, 'legacyService');
  });
});

test('retains the historical result and rejects an unapproved skipped check in the current run', async () => {
  const { qualifyMac } = await import('./mac-qualify.mjs');
  await fixture(async (paths, options) => {
    const runTests = options.runTests;
    options.runTests = async args => {
      const result = await runTests(args);
      if (args.phase === 'current') result.suites[0].specs[2].title = 'restore was skipped';
      return result;
    };
    await assert.rejects(qualifyMac(paths, options), /Unexpected skipped macOS test/);
    const report = savedReport(paths.reports);
    assert.equal(report.status, 'failed');
    assert.equal(report.phases.r2.status, 'passed');
    assert.equal(report.phases.current.status, 'failed');
  });
});

test('CLI exits before mounting anything on a host without native arm64 macOS', {
  skip: process.platform === 'darwin' && process.arch === 'arm64',
}, () => {
  const processResult = spawnSync(process.execPath, ['desktop/mac-qualify.mjs', '--dmg', 'bad',
    '--r2-dmg', 'bad', '--legacy-zip', 'bad', '--reports', 'bad'], { encoding: 'utf8' });
  assert.notEqual(processResult.status, 0);
  assert.match(processResult.stderr, /Apple silicon Mac and arm64 Node\.js/);
});

for (const [title, error] of [
  [restoreRecovery, /Expected the installed restore recovery test to pass/],
  [recognitionAuthorization, /Expected the installed recognition authorization test to pass/],
  [backupSelection, /Expected the installed backup selection test to pass/],
]) {
  test('rejects a candidate report missing "' + title + '"', async () => {
    const { qualifyMac } = await import('./mac-qualify.mjs');
    await fixture(async (paths, options) => {
      const runTests = options.runTests;
      options.runTests = async args => {
        const result = await runTests(args);
        if (args.phase === 'current') {
          result.suites[0].specs = result.suites[0].specs.filter(entry => entry.title !== title);
          result.stats.expected--;
        }
        return result;
      };
      await assert.rejects(qualifyMac(paths, options), error);
      assert.equal(savedReport(paths.reports).phases.current.status, 'failed');
    });
  });
}

test('rejects a current report that omits a discovered installed search test', async () => {
  const { qualifyMac } = await import('./mac-qualify.mjs');
  const search = 'the installed Electron app locates mixed-script search hits and recovers from an invalid anchor';
  await fixture(async (paths, options) => {
    const runTests = options.runTests;
    options.runTests = async args => {
      const report = await runTests(args);
      if (args.phase === 'current' && args.discovery) {
        report.suites[0].specs.push(spec(search, 'skipped'));
      }
      return report;
    };
    await assert.rejects(qualifyMac(paths, options), /Missing installed test: .*mixed-script search hits/);
    const report = savedReport(paths.reports);
    assert.equal(report.status, 'failed');
    assert.equal(report.phases.r2.status, 'passed');
    assert.equal(report.phases.current.status, 'failed');
  });
});

test('rejects repeated results for the same installed test', async () => {
  const { qualifyMac } = await import('./mac-qualify.mjs');
  await fixture(async (paths, options) => {
    const runTests = options.runTests;
    options.runTests = async args => {
      const report = await runTests(args);
      if (args.phase === 'current' && !args.discovery) {
        report.suites[0].specs.push(spec(windowRoundtrip));
        report.stats.expected++;
      }
      return report;
    };
    await assert.rejects(qualifyMac(paths, options), /Duplicate installed test: Electron hides and reopens/);
    assert.equal(savedReport(paths.reports).phases.current.status, 'failed');
  });
});
