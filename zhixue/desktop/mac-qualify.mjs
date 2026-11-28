import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { closeSync, mkdirSync, mkdtempSync, openSync, readFileSync, realpathSync,
  renameSync, rmSync, writeFileSync } from 'node:fs';
import { release, tmpdir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';
import { fileSHA256, verifyInstalledElectron, verifyMacDmg } from './mac-dmg.mjs';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const originalServiceMember = 'Zhixue Electron.app/Contents/Resources/zhixue-service';
const originalFixtures = {
  r2Image: 'e73255a89eff831bbfe959e7990e78fda4bea25a4a647b0105c58c59516a65ee',
  legacyZip: '520ec5f9bbcadf87364bd224e360f2d79e4f4351bc6e0e3f57a77f969c0482d5',
  legacyService: 'b4371ac64fbd0dfa253296ba7442e6f12cb269364227b1726427f64bbc41da4f',
};
const r2Schema = '0052_recognition_grants';
const migration = 'the installed Electron app migrates, completes a review, and restores to a fresh workspace';
const windowRoundtrip = 'Electron hides and reopens the same window, then releases its service on quit';
const restoreRecovery = 'the installed Electron app recovers workspace switches across restarts';
const backupSelection = 'the installed Electron app finds a renamed backup past a same-size damaged copy';
const recognitionAuthorization = 'the installed Electron app enforces single-page recognition authorization';
const installedOcr = 'the installed Electron app protects a concurrent OCR correction and its source file';
const installedOcrSteps = [
  'keeps a competing OCR correction and the unchanged original',
  'preserves the active search while saving the correction',
  'keeps the chosen page when a pending OCR save finishes',
  'reopens the corrected page and original after restart',
];
const installedVoice = 'Electron releases a late microphone stream when hidden and can record again locally';
const installedVoiceSteps = [
  'ends the late microphone stream when the window is hidden',
  'handles a disconnected microphone during setup and capture',
  'releases the microphone when the window is minimized',
  'transcribes a new recording locally and applies only confirmed text',
  'releases recording when leaving and stops the local service',
];
const clockReplay = 'the installed Electron app recalibrates and replays an offline timer after restart';
const installedWiki = 'the installed Electron app preserves wiki search context and original location';
const installedTrash = 'the installed Electron app restores archived originals and reopens their old wiki paths';
const installedTrashSteps = [
  'reads archived paths after opening an encrypted backup in a fresh workspace',
  'confirms unchanged trash bytes after a file identity change',
  'reads the recovered bytes and both old paths after restart',
];
const installedOriginal = 'the installed Electron app saves a returned original and reopens it after restart';
const installedOriginalSteps = [
  'returns the original and reads it with the same file identity',
  'saves a later replacement and opens it from the original link',
  'reopens the updated original and its old link after restart',
];
const delayedReview = 'the installed Electron app retains one overdue review with its grading delay after restart';
const formalReportStep = 'reads formal review details on the original submission day after restart';
const installedPairing = 'paired phone shares offline work, timers and reminders through an encrypted relay';
const installedPhoneSteps = [
  'serves verified mobile assets from the installed service',
  'registers and removes a browser subscription through the encrypted pairing',
  'removing the local pairing discards an in-flight catalog response',
  'rejects an invitation that finishes preparation after a second page has paired',
];
const pushCases = ['unsubscribe-accepted', 'replacement-uncertain'];
const windowsOnly = new Set([
  'installed Windows Electron recovers encrypted restores before and after commit',
  'installed Windows Electron keeps and saves timers through its native exit dialog',
  'Windows Electron releases its local service and reopens pending study after main process termination',
]);

function systemTool(tool, args, output = 'pipe') {
  return execFileSync(join('/usr/bin', tool), args, {
    encoding: 'utf8', timeout: 120_000, stdio: ['ignore', output, 'inherit'],
  })?.trim();
}

function currentChecksum(image) {
  const match = /^([a-f0-9]{64}) {2}(.+)$/.exec(readFileSync(image + '.sha256', 'utf8').trimEnd());
  assert(match && match[2] === basename(image), 'Current DMG needs its matching .sha256 file');
  return match[1];
}

function runPlaywright({ phase, executable, legacyService, service, mobileSite, directory, discovery = false }) {
  const files = ['e2e/installed-learning.spec.ts'];
  if (phase === 'current') files.push('e2e/electron.spec.ts', 'e2e/mobile-pairing.spec.ts');
  const label = phase + (discovery ? '-discovered-tests' : '-playwright');
  const summary = join(directory, label + '.json');
  const environment = {
    ...process.env, CI: '1', NO_PROXY: '127.0.0.1,localhost', no_proxy: '127.0.0.1,localhost',
    ZHIXUE_TEST_ELECTRON: executable, ZHIXUE_0051_SERVICE: legacyService, PLAYWRIGHT_JSON_OUTPUT_FILE: summary,
  };
  for (const name of ['ZHIXUE_0051_FIXTURE', 'ZHIXUE_TEST_SERVICE', 'ZHIXUE_TEST_MOBILE_SITE',
    'ZHIXUE_TEST_MOBILE_DEPLOYMENT', 'ZHIXUE_TEST_MIGRATION_HEAD', 'ELECTRON_RUN_AS_NODE', 'PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV']) {
    delete environment[name];
  }
  if (phase === 'current') {
    environment.ZHIXUE_TEST_SERVICE = service;
    environment.ZHIXUE_TEST_MOBILE_SITE = mobileSite;
  } else {
    environment.ZHIXUE_TEST_MIGRATION_HEAD = r2Schema;
  }
  const log = openSync(join(directory, label + '.txt'), 'wx');
  try {
    const result = spawnSync(process.execPath, [
      join(root, 'node_modules/playwright/cli.js'), 'test', ...files, '--workers=1', '--retries=0', '--forbid-only',
      '--reporter=line,json', ...(discovery ? ['--list'] : ['--output=' + join(directory, phase + '-artifacts')]),
      ...(phase === 'r2' ? ['--grep', migration] : []),
    ], { cwd: root, env: environment, timeout: 15 * 60_000, stdio: ['ignore', log, log] });
    if (result.error) throw result.error;
    assert.equal(result.status, 0, label + ' failed. See ' + label + '.txt');
  } finally {
    closeSync(log);
  }
  return JSON.parse(readFileSync(summary, 'utf8'));
}

function runInstalledCheck(directory, label, command) {
  const summary = join(directory, label + '.json');
  const environment = { ...process.env, PYTEST_DISABLE_PLUGIN_AUTOLOAD: '1' };
  for (const name of ['PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'PYTEST_ADDOPTS']) delete environment[name];
  const log = openSync(join(directory, label + '.txt'), 'wx');
  try {
    const result = spawnSync('uv', ['run', '--locked', '--group', 'packaging', '--python', '3.11',
      'python', ...command, summary],
    { cwd: root, env: environment, timeout: 5 * 60_000, stdio: ['ignore', log, log] });
    if (result.error) throw result.error;
    assert.equal(result.status, 0, 'Installed check failed. See ' + label + '.txt');
  } finally {
    closeSync(log);
  }
  return JSON.parse(readFileSync(summary, 'utf8'));
}

function runInstalledPush({ service, directory }) {
  return runInstalledCheck(directory, 'current-push-receipts', [join(root, 'desktop/verify_packaged_push.py'), service]);
}

function runInstalledMobile({ service, mobileZip, site, directory }) {
  return runInstalledCheck(directory, 'current-mobile-resources', [
    '-m', 'desktop.verify_packaged_mobile', service, mobileZip, '--export-site', site,
  ]);
}

function* testSpecifications(suites) {
  for (const suite of suites) {
    yield* suite.specs ?? [];
    yield* testSpecifications(suite.suites ?? []);
  }
}

function installedCases({ suites }) {
  const cases = new Map();
  for (const { id, title, tests } of testSpecifications(suites)) {
    for (const test of tests) {
      assert(typeof id === 'string' && id, 'Missing installed test identity: ' + title);
      const key = JSON.stringify([id, test.projectId]);
      assert(!cases.has(key), 'Duplicate installed test: ' + title);
      cases.set(key, { title, test });
    }
  }
  return cases;
}

function requireInstalledSteps(cases, passed, title, expectedSteps, label) {
  const installed = [...cases.values()].filter(candidate => candidate.title === title);
  assert(installed.length === 1 && passed.includes(title), 'Expected the installed ' + label + ' test to pass');
  const runs = installed[0].test.results;
  assert(runs?.length === 1 && runs[0].status === 'passed', 'Expected a single passing installed ' + label + ' run');
  for (const step of expectedSteps) {
    const found = (runs[0].steps ?? []).filter(candidate => candidate.title === step);
    assert.equal(found.length, 1, 'Missing or duplicate installed ' + label + ' step: ' + step);
    assert(!found[0].error, 'Failed installed ' + label + ' step: ' + step);
  }
}

function summarizeTests(report, discovery, phase) {
  const { stats } = report;
  const actual = installedCases(report);
  const expected = installedCases(discovery);
  const passed = [];
  const skipped = [];
  for (const { title, test } of actual.values()) {
    if (test.status === 'skipped') {
      assert(phase === 'current' && windowsOnly.has(title), 'Unexpected skipped macOS test: ' + title);
      skipped.push(title);
    } else {
      assert(test.status === 'expected' && (test.expectedStatus ?? 'passed') === 'passed',
        'Installed test did not pass: ' + title);
      passed.push(title);
    }
  }
  assert(stats.unexpected === 0 && stats.flaky === 0 && stats.expected === passed.length && stats.skipped === skipped.length,
    'Playwright summary counts do not match the installed tests');
  assert(passed.includes(migration), 'Expected the original 0051 migration test to pass');
  if (phase === 'r2') assert.deepEqual(passed, [migration], 'Run only the migration against r2');
  else {
    assert(passed.includes(windowRoundtrip), 'Expected the installed window lifecycle test to pass');
    assert(passed.includes(restoreRecovery), 'Expected the installed restore recovery test to pass');
    assert(passed.includes(backupSelection), 'Expected the installed backup selection test to pass');
    assert(passed.includes(recognitionAuthorization), 'Expected the installed recognition authorization test to pass');
    requireInstalledSteps(actual, passed, installedOcr, installedOcrSteps, 'OCR search continuity');
    requireInstalledSteps(actual, passed, installedVoice, installedVoiceSteps, 'voice capture lifecycle');
    assert(passed.includes(clockReplay), 'Expected the installed timer recalibration and replay test to pass');
    assert(passed.includes(installedWiki), 'Expected the installed wiki search context test to pass');
    requireInstalledSteps(actual, passed, installedTrash, installedTrashSteps, 'trash recovery');
    requireInstalledSteps(actual, passed, installedOriginal, installedOriginalSteps, 'original return');
    const reviews = [...actual.values()].filter(({ title }) => title === delayedReview);
    assert(reviews.length === 1 && passed.includes(delayedReview), 'Expected the installed delayed review test to pass');
    const reviewRuns = reviews[0].test.results;
    assert(reviewRuns?.length === 1 && reviewRuns[0].status === 'passed', 'Expected a single passing installed review run');
    const reports = (reviewRuns[0].steps ?? []).filter(step => step.title === formalReportStep);
    assert(reports.length === 1 && !reports[0].error, 'Expected the installed formal review report step to pass');
    requireInstalledSteps(actual, passed, installedPairing, installedPhoneSteps, 'phone pairing');
  }
  for (const [key, { title }] of expected) {
    assert(actual.get(key)?.title === title, 'Missing installed test: ' + title);
  }
  for (const [key, { title }] of actual) {
    assert(expected.has(key), 'Unexpected installed test: ' + title);
  }
  return { discovered: expected.size, passed: passed.length, skipped: skipped.length, passedTests: passed, skippedTests: skipped };
}

export async function qualifyMac({ currentImage, r2Image, legacyZip, mobileZip, reports }, {
  fixtures = originalFixtures, run = systemTool, startInstalled = verifyInstalledElectron, runTests = runPlaywright,
  checkPush = runInstalledPush, checkMobile = runInstalledMobile,
} = {}) {
  mkdirSync(reports, { recursive: true });
  const directory = mkdtempSync(join(realpathSync(reports), 'mac-qualification-'));
  const result = {
    status: 'running', host: { platform: process.platform, arch: process.arch, release: release() },
    inputs: {}, phases: {},
  };
  function save() {
    const pending = join(directory, 'qualification.json.pending');
    writeFileSync(pending, JSON.stringify(result, null, 2) + '\n');
    renameSync(pending, join(directory, 'qualification.json'));
  }
  let step = 'inputs';
  try {
    const candidate = currentChecksum(currentImage);
    assert.notEqual(candidate, fixtures.r2Image, 'Choose a new candidate for the current DMG');
    for (const [key, file, expected, label] of [
      ['currentImage', currentImage, candidate, 'Current DMG'],
      ['r2Image', r2Image, fixtures.r2Image, 'r2 DMG'],
      ['legacyZip', legacyZip, fixtures.legacyZip, 'Original 0051 ZIP'],
    ]) {
      assert.equal(await fileSHA256(file), expected, label + ' checksum mismatch');
      result.inputs[key] = { file: basename(file), sha256: expected };
    }
    result.inputs.mobileZip = { file: basename(mobileZip), sha256: await fileSHA256(mobileZip) };
    save();
    const parent = realpathSync(tmpdir());
    const work = mkdtempSync(join(parent, 'zhixue-mac-qualification-'));
    try {
      step = 'legacyService';
      const legacyService = join(work, 'zhixue-service-0051');
      const fd = openSync(legacyService, 'wx', 0o700);
      try {
        run('unzip', ['-p', legacyZip, originalServiceMember], fd);
      } finally {
        closeSync(fd);
      }
      assert.equal(await fileSHA256(legacyService), fixtures.legacyService, 'Original 0051 service checksum mismatch');
      run('codesign', ['--verify', '--strict', legacyService]);
      assert.equal(run('lipo', ['-archs', legacyService]), 'arm64', 'Original 0051 service must be arm64');
      result.inputs.legacyServiceSHA256 = fixtures.legacyService;

      for (const [phase, image, expected] of [['r2', r2Image, fixtures.r2Image], ['current', currentImage, candidate]]) {
        step = phase;
        const evidence = result.phases[phase] = { status: 'running', imageSHA256: expected };
        save();
        const digest = await verifyMacDmg(image, {
          run,
          async verifySidecar(service) {
            evidence.bundledServiceSHA256 = await fileSHA256(service);
          },
          async verifyApp(executable, workspace) {
            const bundledService = join(dirname(executable), '..', 'Resources', 'zhixue-service');
            assert.equal(await fileSHA256(bundledService), evidence.bundledServiceSHA256, 'Installed service differs from image');
            const mobileSite = phase === 'current' ? join(work, 'installed-mobile-site') : undefined;
            if (mobileSite) {
              const resources = await checkMobile({ service: bundledService, mobileZip, site: mobileSite, directory });
              assert.equal(resources?.serviceSHA256, evidence.bundledServiceSHA256,
                'Mobile resource service differs from the installed service');
              assert.equal(resources.deploymentSHA256, result.inputs.mobileZip.sha256,
                'Mobile resource deployment differs from the qualification input');
              const matched = resources.matchedResources;
              assert(matched && ['mobile/index.html', 'mobile/sw.js', 'mobile/manifest.webmanifest',
                'mobile/icon-192.png', 'mobile/icon-512.png', 'zhixue.png'].every(name => Object.hasOwn(matched, name)) &&
                Object.values(matched).every(value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)),
              'Missing mobile resource evidence');
              evidence.mobileResources = resources;
            }
            const options = { phase, executable, legacyService, service: bundledService, mobileSite, directory };
            const discovery = await runTests({ ...options, discovery: true });
            if (phase === 'current') {
              await startInstalled(executable, workspace);
              const push = await checkPush({ service: bundledService, directory });
              assert.equal(push?.serviceSHA256, evidence.bundledServiceSHA256,
                'Push receipt service differs from the installed service');
              assert.deepEqual(push.passed, pushCases, 'Expected both push receipt cases to pass');
              evidence.pushReceipts = push;
            }
            evidence.tests = summarizeTests(await runTests(options), discovery, phase);
            if (phase === 'current') {
              assert.equal(await fileSHA256(bundledService), evidence.bundledServiceSHA256, 'Installed service changed during browser tests');
              evidence.installedPhonePairing = { serviceSHA256: evidence.bundledServiceSHA256, passedSteps: installedPhoneSteps };
              evidence.installedTrashRecovery = { serviceSHA256: evidence.bundledServiceSHA256, passedSteps: installedTrashSteps };
              evidence.installedOriginalReturn = {
                serviceSHA256: evidence.bundledServiceSHA256, passedSteps: installedOriginalSteps,
              };
              evidence.installedOcrSearch = {
                serviceSHA256: evidence.bundledServiceSHA256, passedSteps: installedOcrSteps,
              };
              evidence.installedVoiceCapture = {
                serviceSHA256: evidence.bundledServiceSHA256, captureSource: 'synthetic', passedSteps: installedVoiceSteps,
              };
              evidence.installedLibraryAndReview = {
                serviceSHA256: evidence.bundledServiceSHA256, wiki: installedWiki, formalReview: formalReportStep,
              };
            }
          },
        });
        assert.equal(digest, expected, 'DMG changed during qualification');
        evidence.status = 'passed';
        save();
      }
    } finally {
      assert.equal(dirname(realpathSync(work)), parent);
      rmSync(work, { recursive: true, maxRetries: 3 });
    }
    result.status = 'passed';
    return { directory, result };
  } catch (error) {
    result.status = 'failed';
    result.error = { step, message: error.message };
    if (result.phases[step]?.status === 'running') result.phases[step].status = 'failed';
    throw new Error(error.message + '\nQualification evidence: ' + directory, { cause: error });
  } finally {
    save();
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  const { values } = parseArgs({ options: {
    dmg: { type: 'string' }, 'r2-dmg': { type: 'string' }, 'legacy-zip': { type: 'string' },
    'mobile-zip': { type: 'string' }, reports: { type: 'string' }, help: { type: 'boolean' },
  } });
  if (values.help) {
    console.log('node desktop/mac-qualify.mjs --dmg <current.dmg> --r2-dmg <r2.dmg> --legacy-zip <0051.zip> --mobile-zip <deployment.zip> --reports <directory>');
  } else {
    assert(process.platform === 'darwin' && process.arch === 'arm64',
      'macOS qualification requires an Apple silicon Mac and arm64 Node.js');
    for (const option of ['dmg', 'r2-dmg', 'legacy-zip', 'mobile-zip', 'reports']) {
      assert(values[option], 'Missing --' + option);
    }
    const { directory } = await qualifyMac({
      currentImage: resolve(values.dmg), r2Image: resolve(values['r2-dmg']),
      legacyZip: resolve(values['legacy-zip']), mobileZip: resolve(values['mobile-zip']), reports: resolve(values.reports),
    });
    console.log('macOS qualification passed. Evidence in ' + directory);
  }
}
