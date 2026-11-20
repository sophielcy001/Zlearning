import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const script = fileURLToPath(new URL('./package.mjs', import.meta.url));
const env = Object.fromEntries(Object.entries(process.env).filter(([name]) => name.toLowerCase() !== 'path'));
env.PATH = '';

function packageWithNoTools(target) {
  const result = spawnSync(process.execPath, [script, target], { env, encoding: 'utf8', windowsHide: true });
  assert.ifError(result.error);
  assert.equal(result.status, 1);
  return result.stderr;
}

test('Tauri build reports missing Rust before Python packaging starts', { skip: process.platform !== 'win32' }, () => {
  assert.match(packageWithNoTools('tauri'), /Tauri needs rustc/);
  assert.match(packageWithNoTools('all'), /Tauri needs rustc/);
});

test('Electron packaging does not need the Tauri toolchain', { skip: process.platform !== 'win32' }, () => {
  assert.match(packageWithNoTools('electron'), /spawnSync uv ENOENT/);
});

test('the macOS DMG target rejects other hosts before packaging dependencies are started', {
  skip: process.platform === 'darwin' && process.arch === 'arm64',
}, () => {
  assert.match(packageWithNoTools('electron-dmg'), /electron-dmg requires an Apple silicon Mac/);
});


test('Windows packages keep Cargo targets short and separate for each checkout', async () => {
  const { join, relative, resolve } = await import('node:path');
  const { tauriBuildDirectory } = await import('./tauri-build-files.mjs');
  const temp = resolve('tmp');
  const project = resolve('workspace', 'a-long-deep-root'.repeat(9), 'zhixue');
  const options = { platform: 'win32', temp };
  const selected = tauriBuildDirectory(project, options);
  assert.match(relative(temp, selected), /^zhixue-tauri-[a-f0-9]{16}$/);
  assert.equal(tauriBuildDirectory(project, options), selected);
  assert.notEqual(tauriBuildDirectory(join(project, 'another'), options), selected);
  assert(selected.length < join(project, '.desktop-build', 'tauri-target').length);
});

test('an explicit Cargo target overrides the platform defaults', async () => {
  const { join, resolve } = await import('node:path');
  const { tauriBuildDirectory } = await import('./tauri-build-files.mjs');
  const project = resolve('workspace', 'zhixue');
  const absolute = resolve('explicit-target');
  for (const platform of ['win32', 'darwin']) {
    assert.equal(tauriBuildDirectory(project, { platform, configured: absolute }), absolute);
    assert.equal(tauriBuildDirectory(project, { platform, configured: 'custom-target' }),
      join(project, 'custom-target'));
  }
  assert.equal(tauriBuildDirectory(project, { platform: 'darwin' }),
    join(project, '.desktop-build', 'tauri-target'));
});


test('Windows installer publication replaces an existing file after staging and preserves it on failure', async () => {
  const { join } = await import('node:path');
  const { tmpdir } = await import('node:os');
  const { mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } = await import('node:fs');
  const { publishWindowsInstaller } = await import('./tauri-build-files.mjs');
  const stage = mkdtempSync(join(tmpdir(), 'zhixue-installer-test-'));
  try {
    const source = join(stage, 'new.exe');
    const target = join(stage, 'release.exe');
    writeFileSync(source, 'new setup');
    writeFileSync(target, 'previous setup');
    assert.throws(() => publishWindowsInstaller(join(stage, 'missing.exe'), target), { code: 'ENOENT' });
    assert.equal(readFileSync(target, 'utf8'), 'previous setup');
    publishWindowsInstaller(source, target);
    assert.equal(readFileSync(target, 'utf8'), 'new setup');
    assert.equal(readFileSync(source, 'utf8'), 'new setup');
    assert.deepEqual(readdirSync(stage).sort(), ['new.exe', 'release.exe']);
  } finally {
    rmSync(stage, { recursive: true, force: true });
  }
});
