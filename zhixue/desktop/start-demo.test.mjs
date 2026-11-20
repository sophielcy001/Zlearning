import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { copyFileSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { delimiter, join } from 'node:path';
import { test } from 'node:test';

const entry = new URL('../start-demo.cmd', import.meta.url);
const share = process.env.ZHIXUE_TEST_UNC_SOURCE_ROOT;

function launch(t, parent) {
  const project = mkdtempSync(join(parent, 'zhixue launch project '));
  const caller = mkdtempSync(join(tmpdir(), 'zhixue-launch-caller-'));
  t.after(() => {
    rmSync(project, { recursive: true, force: true });
    rmSync(caller, { recursive: true, force: true });
  });
  const launcher = join(project, 'start-demo.cmd');
  copyFileSync(entry, launcher);
  writeFileSync(join(project, 'project.marker'), '');
  writeFileSync(join(caller, 'caller.marker'), '');
  writeFileSync(join(caller, 'npm.cmd'), [
    '@echo off',
    'echo npm-arguments:%*',
    'if exist project.marker echo started-in-project',
    '> "%ZHIXUE_LAUNCH_MARKER%" echo %CD%\\project.marker',
    'exit /b 23',
  ].join('\r\n'));
  writeFileSync(join(caller, 'invoke.cmd'), [
    '@echo off',
    'call "%ZHIXUE_LAUNCH_ENTRY%"',
    'echo launch-status:%ERRORLEVEL%',
    'if exist caller.marker echo caller-restored',
    'set /p projectMarker=<"%ZHIXUE_LAUNCH_MARKER%"',
    'if exist "%projectMarker%" echo project-still-reachable',
  ].join('\r\n'));
  const env = Object.fromEntries(Object.entries(process.env).filter(([name]) => name.toLowerCase() !== 'path'));
  const result = spawnSync(process.env.ComSpec || 'cmd.exe', ['/d', '/e:on', '/c', 'invoke.cmd'], {
    cwd: caller, encoding: 'utf8', input: '\r\n', timeout: 15000, windowsHide: true,
    env: { ...env, PATH: caller + delimiter + process.env.PATH, ZHIXUE_LAUNCH_ENTRY: launcher,
      ZHIXUE_LAUNCH_MARKER: join(caller, 'mapping.txt') },
  });
  assert.ifError(result.error);
  assert.equal(result.status, 0, result.stdout + result.stderr);
  return result.stdout;
}

function assertInstallFailureInProject(output) {
  assert.match(output, /^npm-arguments:ci\r?$/m);
  assert.match(output, /^started-in-project\r?$/m);
  assert.match(output, /^launch-status:1\r?$/m);
  assert.match(output, /^caller-restored\r?$/m);
}

test('Windows source launch restores its caller directory after a dependency failure', {
  skip: process.platform !== 'win32',
}, t => {
  assertInstallFailureInProject(launch(t, tmpdir()));
});

test('Windows source launch temporarily maps a UNC project and releases the drive on failure', {
  skip: process.platform !== 'win32' || !share,
}, t => {
  assert.ok(share.startsWith('\\\\'), 'ZHIXUE_TEST_UNC_SOURCE_ROOT must be a writable UNC directory');
  const output = launch(t, share);
  assertInstallFailureInProject(output);
  assert.doesNotMatch(output, /^project-still-reachable\r?$/m);
});

