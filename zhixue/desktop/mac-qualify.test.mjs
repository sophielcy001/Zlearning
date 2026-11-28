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
const installedOcr = 'the installed Electron app protects a concurrent OCR correction and its source file';
const ocrSteps = [
  'keeps a competing OCR correction and the unchanged original',
  'preserves the active search while saving the correction',
  'keeps the chosen page when a pending OCR save finishes',
  'reopens the corrected page and original after restart',
];
const installedVoice = 'Electron releases a late microphone stream when hidden and can record again locally';
const voiceSteps = [
  'ends the late microphone stream when the window is hidden',
  'handles a disconnected microphone during setup and capture',
  'releases the microphone when the window is minimized',
  'transcribes a new recording locally and applies only confirmed text',
  'releases recording when leaving and stops the local service',
];
const clockReplay = 'the installed Electron app recalibrates and replays an offline timer after restart';
const installedWiki = 'the installed Electron app preserves wiki search context and original location';
const installedTrash = 'the installed Electron app restores archived originals and reopens their old wiki paths';
const trashSteps = [
  'reads archived paths after opening an encrypted backup in a fresh workspace',
  'confirms unchanged trash bytes after a file identity change',
  'reads the recovered bytes and both old paths after restart',
];
const installedOriginal = 'the installed Electron app saves a returned original and reopens it after restart';
const originalSteps = [
  'returns the original and reads it with the same file identity',
  'saves a later replacement and opens it from the original link',
  'reopens the updated original and its old link after restart',
];
const delayedReview = 'the installed Electron app retains one overdue review with its grading delay after restart';
const formalReportStep = 'reads formal review details on the original submission day after restart';
const installedPairing = 'paired phone shares offline work, timers and reminders through an encrypted relay';
const phoneSteps = [
  'serves verified mobile assets from the installed service',
  'registers and removes a browser subscription through the encrypted pairing',
  'removing the local pairing discards an in-flight catalog response',
  'rejects an invitation that finishes preparation after a second page has paired',
];
const pushCases = ['unsubscribe-accepted', 'replacement-uncertain'];
const windowsOnly = 'installed Windows Electron recovers encrypted restores before and after commit';
const checksum = value => createHash('sha256').update(value).digest('hex');
const mobileAssets = Object.fromEntries(['mobile/index.html', 'mobile/sw.js', 'mobile/manifest.webmanifest',
  'mobile/icon-192.png', 'mobile/icon-512.png', 'zhixue.png'].map(name => [name, checksum(name)]));
const spec = (title, status = 'expected') => ({ id: title, title, tests: [{ projectId: 'fixture', status }] });

async function fixture(action) {
  const parent = realpathSync(tmpdir());
  const work = mkdtempSync(join(parent, 'zhixue-mac-qualify-test-'));
  try {
    const paths = {
      currentImage: join(work, 'Apple silicon release.dmg'),
      r2Image: join(work, 'r2.dmg'),
      legacyZip: join(work, 'legacy fixture.zip'),
      mobileZip: join(work, 'mobile deployment.zip'),
      reports: join(work, 'existing reports'),
    };
    const inputs = { currentImage: 'current', r2Image: 'r2', legacyZip: 'archive', legacyService: 'service from 0051', mobileZip: 'mobile bundle' };
    for (const key of ['currentImage', 'r2Image', 'legacyZip', 'mobileZip']) writeFileSync(paths[key], inputs[key]);
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
      async checkMobile({ service, mobileZip, site, directory }) {
        assert.equal(readFileSync(service, 'utf8'), 'current');
        assert.equal(readFileSync(mobileZip, 'utf8'), inputs.mobileZip);
        assert(!existsSync(mount));
        assert(existsSync(directory));
        mkdirSync(site);
        writeFileSync(join(site, 'worker-proof'), checksum('mobile/sw.js'));
        nativeRuns.push('current-mobile');
        return { serviceSHA256: checksum('current'), deploymentSHA256: checksum(inputs.mobileZip),
          matchedResources: mobileAssets };
      },
      async checkPush({ service, directory }) {
        assert.equal(readFileSync(service, 'utf8'), 'current');
        assert(!existsSync(mount));
        assert(existsSync(directory));
        nativeRuns.push('current-push');
        return { serviceSHA256: checksum('current'), passed: pushCases };
      },
      async runTests({ phase, executable, legacyService, service, mobileSite, discovery = false }) {
        assert.equal(readFileSync(executable, 'utf8'), phase === 'r2' ? 'r2' : 'current');
        assert.equal(readFileSync(legacyService, 'utf8'), inputs.legacyService);
        assert(!existsSync(mount));
        assert.equal(readFileSync(service, 'utf8'), phase === 'r2' ? 'r2' : 'current');
        if (phase === 'current') {
          assert.equal(readFileSync(join(mobileSite, 'worker-proof'), 'utf8'), checksum('mobile/sw.js'));
        } else {
          assert.equal(mobileSite, undefined);
        }
        if (!discovery) nativeRuns.push(phase);
        const pairing = spec(installedPairing);
        pairing.tests[0].results = [{ status: 'passed', steps: phoneSteps.map(title => ({ title })) }];
        const report = spec(delayedReview);
        report.tests[0].results = [{ status: 'passed', steps: [{ title: formalReportStep }] }];
        const trash = spec(installedTrash);
        trash.tests[0].results = [{ status: 'passed', steps: trashSteps.map(title => ({ title })) }];
        const original = spec(installedOriginal);
        original.tests[0].results = [{ status: 'passed', steps: originalSteps.map(title => ({ title })) }];
        const ocr = spec(installedOcr);
        ocr.tests[0].results = [{ status: 'passed', steps: ocrSteps.map(title => ({ title })) }];
        const voice = spec(installedVoice);
        voice.tests[0].results = [{ status: 'passed', steps: voiceSteps.map(title => ({ title })) }];
        const entries = phase === 'r2' ? [spec(migration)] : [
          spec(migration), spec(windowRoundtrip), spec(windowsOnly, 'skipped'), spec(restoreRecovery),
          spec(recognitionAuthorization), ocr, voice, spec(backupSelection), spec(clockReplay), pairing, spec(installedWiki), report, trash, original,
        ];
        return {
          stats: { expected: phase === 'r2' ? 1 : 13, unexpected: 0, flaky: 0, skipped: phase === 'r2' ? 0 : 1 },
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
    assert.deepEqual(nativeRuns, ['r2', 'current-mobile', 'current-smoke', 'current-push', 'current']);
    assert.deepEqual(savedReport(paths.reports), output.result);
    assert.equal(output.result.status, 'passed');
    assert.equal(output.result.inputs.legacyServiceSHA256, options.fixtures.legacyService);
    assert.equal(output.result.phases.r2.bundledServiceSHA256, checksum('r2'));
    assert.equal(output.result.phases.current.bundledServiceSHA256, checksum('current'));
    assert.deepEqual(output.result.phases.current.pushReceipts.passed, pushCases);
    assert.deepEqual(output.result.phases.current.installedLibraryAndReview, {
      serviceSHA256: checksum('current'), wiki: installedWiki, formalReview: formalReportStep,
    });
    assert.equal(output.result.inputs.mobileZip.sha256, checksum('mobile bundle'));
    assert.deepEqual(output.result.phases.current.mobileResources.matchedResources, mobileAssets);
    assert.equal(output.result.phases.current.mobileResources.serviceSHA256, checksum('current'));
    assert.equal(output.result.phases.current.tests.skipped, 1);
    assert.equal(output.result.phases.current.tests.discovered, 14);
    assert.deepEqual(output.result.phases.current.installedTrashRecovery,
      { serviceSHA256: checksum('current'), passedSteps: trashSteps });
    assert(!Object.hasOwn(output.result.phases.r2, 'installedTrashRecovery'));
    assert.deepEqual(output.result.phases.current.installedOriginalReturn,
      { serviceSHA256: checksum('current'), passedSteps: originalSteps });
    assert(!Object.hasOwn(output.result.phases.r2, 'installedOriginalReturn'));
    assert.deepEqual(output.result.phases.current.installedOcrSearch,
      { serviceSHA256: checksum('current'), passedSteps: ocrSteps });
    assert(!Object.hasOwn(output.result.phases.r2, 'installedOcrSearch'));
    assert.deepEqual(output.result.phases.current.installedVoiceCapture,
      { serviceSHA256: checksum('current'), captureSource: 'synthetic', passedSteps: voiceSteps });
    assert(!Object.hasOwn(output.result.phases.r2, 'installedVoiceCapture'));
    assert.deepEqual(output.result.phases.current.installedPhonePairing,
      { serviceSHA256: checksum('current'), passedSteps: phoneSteps });
    assert(!Object.hasOwn(output.result.phases.r2, 'installedPhonePairing'));
    assert(output.result.phases.current.tests.passedTests.includes(windowRoundtrip));
    assert(output.result.phases.current.tests.passedTests.includes(backupSelection));
    assert(output.result.phases.current.tests.passedTests.includes(clockReplay));
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
  [installedOcr, /Expected the installed OCR search continuity test to pass/],
  [installedVoice, /Expected the installed voice capture lifecycle test to pass/],
  [backupSelection, /Expected the installed backup selection test to pass/],
  [clockReplay, /Expected the installed timer recalibration and replay test to pass/],
  [installedWiki, /Expected the installed wiki search context test to pass/],
  [installedTrash, /Expected the installed trash recovery test to pass/],
  [delayedReview, /Expected the installed delayed review test to pass/],
  [installedOriginal, /Expected the installed original return test to pass/],
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

test('rejects a passing trash recovery result without evidence from each installed phase', async () => {
  const { qualifyMac } = await import('./mac-qualify.mjs');
  await fixture(async (paths, options) => {
    const runTests = options.runTests;
    options.runTests = async args => {
      const report = await runTests(args);
      if (args.phase === 'current' && !args.discovery) {
        const result = report.suites[0].specs.find(entry => entry.title === installedTrash).tests[0].results[0];
        result.steps = result.steps.filter(step => step.title !== trashSteps[2]);
      }
      return report;
    };
    await assert.rejects(qualifyMac(paths, options), /Missing or duplicate installed trash recovery step/);
    const report = savedReport(paths.reports);
    assert.equal(report.phases.r2.status, 'passed');
    assert.equal(report.phases.current.status, 'failed');
  });
});

for (const [description, alter, expectation] of [
  ['missing restart evidence', steps => steps.pop(), /Missing or duplicate installed original return step/],
  ['duplicate return evidence', steps => steps.push(steps[0]), /Missing or duplicate installed original return step/],
  ['failed save evidence', steps => { steps[1].error = { message: 'save failed' }; }, /Failed installed original return step/],
]) {
  test('rejects installed original return with ' + description, async () => {
    const { qualifyMac } = await import('./mac-qualify.mjs');
    await fixture(async (paths, options) => {
      const runTests = options.runTests;
      options.runTests = async args => {
        const report = await runTests(args);
        if (args.phase === 'current' && !args.discovery) {
          const result = report.suites[0].specs.find(entry => entry.title === installedOriginal).tests[0].results[0];
          alter(result.steps);
        }
        return report;
      };
      await assert.rejects(qualifyMac(paths, options), expectation);
      const report = savedReport(paths.reports);
      assert.equal(report.phases.r2.status, 'passed');
      assert.equal(report.phases.current.status, 'failed');
    });
  });
}

for (const [description, alter, expectation] of [
  ['missing search evidence', steps => { steps.splice(1, 1); }, /Missing or duplicate installed OCR search continuity step/],
  ['duplicate page evidence', steps => { steps.push(steps[2]); }, /Missing or duplicate installed OCR search continuity step/],
  ['failed restart evidence', steps => { steps[3].error = { message: 'restart failed' }; }, /Failed installed OCR search continuity step/],
]) {
  test('rejects installed OCR search with ' + description, async () => {
    const { qualifyMac } = await import('./mac-qualify.mjs');
    await fixture(async (paths, options) => {
      const runTests = options.runTests;
      options.runTests = async args => {
        const report = await runTests(args);
        if (args.phase === 'current' && !args.discovery) {
          const result = report.suites[0].specs.find(entry => entry.title === installedOcr).tests[0].results[0];
          alter(result.steps);
        }
        return report;
      };
      await assert.rejects(qualifyMac(paths, options), expectation);
      const report = savedReport(paths.reports);
      assert.equal(report.phases.r2.status, 'passed');
      assert.equal(report.phases.current.status, 'failed');
    });
  });
}

for (const [description, alter, expectation] of [
  ['missing disconnect evidence', steps => { steps.splice(1, 1); }, /Missing or duplicate installed voice capture lifecycle step/],
  ['duplicate teardown evidence', steps => { steps.push(steps[4]); }, /Missing or duplicate installed voice capture lifecycle step/],
  ['failed local transcription evidence', steps => { steps[3].error = { message: 'transcription failed' }; },
    /Failed installed voice capture lifecycle step/],
]) {
  test('rejects installed voice capture with ' + description, async () => {
    const { qualifyMac } = await import('./mac-qualify.mjs');
    await fixture(async (paths, options) => {
      const runTests = options.runTests;
      options.runTests = async args => {
        const report = await runTests(args);
        if (args.phase === 'current' && !args.discovery) {
          const result = report.suites[0].specs.find(entry => entry.title === installedVoice).tests[0].results[0];
          alter(result.steps);
        }
        return report;
      };
      await assert.rejects(qualifyMac(paths, options), expectation);
      const report = savedReport(paths.reports);
      assert.equal(report.phases.r2.status, 'passed');
      assert.equal(report.phases.current.status, 'failed');
    });
  });
}

test('rejects a delayed review result without the formal report step after restart', async () => {
  const { qualifyMac } = await import('./mac-qualify.mjs');
  await fixture(async (paths, options) => {
    const runTests = options.runTests;
    options.runTests = async args => {
      const report = await runTests(args);
      if (args.phase === 'current' && !args.discovery) {
        const result = report.suites[0].specs.find(entry => entry.title === delayedReview).tests[0].results[0];
        result.steps = [];
      }
      return report;
    };
    await assert.rejects(qualifyMac(paths, options), /Expected the installed formal review report step to pass/);
    const report = savedReport(paths.reports);
    assert.equal(report.phases.current.status, 'failed');
    assert.equal(report.phases.r2.status, 'passed');
  });
});

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

for (const [label, report, expected] of [
  ['missing push case', { serviceSHA256: checksum('current'), passed: [pushCases[0]] }, /both push receipt cases/],
  ['different push service', { serviceSHA256: checksum('r2'), passed: pushCases }, /Push receipt service differs/],
]) {
  test('rejects a current candidate with ' + label, async () => {
    const { qualifyMac } = await import('./mac-qualify.mjs');
    await fixture(async (paths, options) => {
      options.checkPush = async () => report;
      await assert.rejects(qualifyMac(paths, options), expected);
      const result = savedReport(paths.reports);
      assert.equal(result.phases.r2.status, 'passed');
      assert.equal(result.phases.current.status, 'failed');
    });
  });
}

for (const [label, change, expected] of [
  ['different embedded mobile service', { serviceSHA256: checksum('r2') }, /Mobile resource service differs/],
  ['different mobile deployment', { deploymentSHA256: checksum('older mobile bundle') }, /Mobile resource deployment differs/],
  ['an incomplete mobile receipt', { matchedResources: { 'mobile/sw.js': checksum('worker') } },
    /Missing mobile resource evidence/],
]) {
  test('rejects a current candidate with ' + label, async () => {
    const { qualifyMac } = await import('./mac-qualify.mjs');
    await fixture(async (paths, options, _calls, nativeRuns) => {
      const verified = options.checkMobile;
      options.checkMobile = async args => ({ ...await verified(args), ...change });
      await assert.rejects(qualifyMac(paths, options), expected);
      const result = savedReport(paths.reports);
      assert.equal(result.phases.r2.status, 'passed');
      assert.equal(result.phases.current.status, 'failed');
      assert.deepEqual(nativeRuns, ['r2', 'current-mobile']);
    });
  });
}


test('rejects a current report that omits notification cleanup on the installed phone site', async () => {
  const { qualifyMac } = await import('./mac-qualify.mjs');
  await fixture(async (paths, options) => {
    const original = options.runTests;
    options.runTests = async args => {
      const report = await original(args);
      if (args.phase === 'current') {
        report.suites[0].specs = report.suites[0].specs.filter(row => row.title !== installedPairing);
        report.stats.expected--;
      }
      return report;
    };
    await assert.rejects(qualifyMac(paths, options), /Expected the installed phone pairing test to pass/);
    assert.equal(savedReport(paths.reports).phases.current.status, 'failed');
  });
});

test('rejects a passing installed pairing report that omits a notification cleanup step', async () => {
  const { qualifyMac } = await import('./mac-qualify.mjs');
  await fixture(async (paths, options) => {
    const original = options.runTests;
    options.runTests = async args => {
      const report = await original(args);
      if (args.phase === 'current' && !args.discovery) {
        const result = report.suites[0].specs.find(row => row.title === installedPairing).tests[0].results[0];
        result.steps = result.steps.filter(step => step.title !== phoneSteps[2]);
      }
      return report;
    };
    await assert.rejects(qualifyMac(paths, options), /Missing or duplicate installed phone pairing step: removing the local pairing/);
    assert.equal(savedReport(paths.reports).phases.current.status, 'failed');
  });
});
