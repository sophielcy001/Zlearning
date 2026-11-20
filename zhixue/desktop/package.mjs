import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { copyFileSync, cpSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import electronBuilder from 'electron-builder';
import { verifyMacDmg } from './mac-dmg.mjs';
import { publishWindowsInstaller, tauriBuildDirectory } from './tauri-build-files.mjs';
import { verifyService } from './verify-service.mjs';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const release = join(root, 'release');
const work = join(root, '.desktop-build');
const windows = process.platform === 'win32';
const project = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'));
const serviceDirectory = join(release, 'service');
const serviceName = windows ? 'zhixue-service.exe' : 'zhixue-service';
const binary = join(serviceDirectory, serviceName);
const iconDirectory = join(release, 'icons');
const tauri = join(root, 'node_modules', '@tauri-apps', 'cli', 'tauri.js');

function run(program, args, environment = {}) {
  const result = spawnSync(program, args, {
    cwd: root, stdio: 'inherit', env: { ...process.env, ...environment }, windowsHide: true,
  });
  if (result.error) throw result.error;
  assert.equal(result.status, 0, program + ' did not complete successfully');
}

async function buildService() {
  mkdirSync(work, { recursive: true });
  const uv = ['run', '--locked', '--no-dev', '--group', 'packaging', '--python', '3.11', 'python', '-m'];
  const environment = { UV_PROJECT_ENVIRONMENT: join(work, 'venv') };
  run('uv', [...uv, 'backend.voice_model'], environment);
  run('uv', [...uv, 'PyInstaller', '--noconfirm', '--workpath', join(work, 'pyinstaller'),
    '--distpath', serviceDirectory, join(root, 'desktop', 'service.spec')], environment);
  await verifyService(binary);
}

async function buildElectron({ output = join(release, 'electron'), diskImage } = {}) {
  const electron = join(root, 'node_modules', 'electron');
  run(process.execPath, [join(electron, 'install.js')], {
    ELECTRON_INSTALL_PLATFORM: process.platform, ELECTRON_INSTALL_ARCH: process.arch, ELECTRON_OVERRIDE_DIST_PATH: '',
  });
  const staging = join(work, 'electron-app');
  mkdirSync(join(staging, 'desktop'), { recursive: true });
  mkdirSync(join(staging, 'icons'), { recursive: true });
  copyFileSync(join(root, 'desktop', 'electron.cjs'), join(staging, 'desktop', 'electron.cjs'));
  copyFileSync(join(root, 'desktop', 'window-preload.cjs'), join(staging, 'desktop', 'window-preload.cjs'));
  copyFileSync(join(root, 'desktop', 'offline-store.cjs'), join(staging, 'desktop', 'offline-store.cjs'));
  copyFileSync(join(root, 'src-tauri', 'icons', 'icon.ico'), join(staging, 'icons', 'icon.ico'));
  copyFileSync(join(root, 'public', 'zhixue.png'), join(staging, 'icons', 'icon.png'));
  writeFileSync(join(staging, 'package.json'), JSON.stringify({
    name: 'zhixue-electron', version: project.version, description: 'Zhixue desktop trial',
    main: 'desktop/electron.cjs',
  }, null, 2));

  const { Platform, archFromString, build } = electronBuilder;
  const appTarget = windows ? 'nsis' : 'dir';
  await build({
    projectDir: staging,
    targets: Platform.current().createTarget(diskImage ? 'dmg' : appTarget, archFromString(process.arch)),
    publish: 'never',
    config: {
      appId: 'local.zhixue.desktop.electron',
      productName: 'Zhixue Electron',
      electronVersion: project.devDependencies.electron,
      electronDist: join(electron, 'dist'),
      directories: { output },
      files: ['desktop/electron.cjs', 'desktop/window-preload.cjs', 'desktop/offline-store.cjs', 'icons/**'],
      extraResources: [{ from: binary, to: serviceName }],
      npmRebuild: false,
      ...(diskImage && { forceCodeSigning: true, dmg: { artifactName: diskImage, writeUpdateInfo: false } }),
      mac: {
        identity: diskImage ? '-' : null, icon: join(iconDirectory, 'icon.icns'), category: 'public.app-category.education',
        extendInfo: { NSMicrophoneUsageDescription: '智学使用麦克风将你主动录制的学习问题和回答转成文字。' },
        ...(diskImage && { hardenedRuntime: false, notarize: false, binaries: ['Contents/Resources/zhixue-service'] }),
      },
      win: { icon: join(root, 'src-tauri', 'icons', 'icon.ico'), signAndEditExecutable: false },
      nsis: {
        oneClick: true, perMachine: false, deleteAppDataOnUninstall: false,
        artifactName: 'Zhixue-Electron-' + project.version + '-' + process.arch + '.exe',
      },
    },
  });
}

async function buildElectronDmg() {
  const output = join(release, 'electron');
  mkdirSync(output, { recursive: true });
  const staging = mkdtempSync(join(output, '.dmg-'));
  const name = 'Zhixue-Electron-' + project.version + '-macos-arm64.dmg';
  const image = join(staging, name);
  const checksumFile = name + '.sha256';

  await buildElectron({ output: staging, diskImage: name });
  const digest = await verifyMacDmg(image);
  writeFileSync(join(staging, checksumFile), digest + '  ' + name + '\n');
  renameSync(image, join(output, name));
  renameSync(join(staging, checksumFile), join(output, checksumFile));
  assert.equal(dirname(staging), output);
  rmSync(staging, { recursive: true });
  console.log('Verified and published ' + join(output, name));
}

function tauriTarget() {
  const host = spawnSync('rustc', ['--print', 'host-tuple'], { cwd: root, encoding: 'utf8', windowsHide: true });
  if (host.error?.code === 'ENOENT') throw new Error('Tauri needs rustc and Cargo for the native MSVC or Apple toolchain.');
  if (host.error) throw host.error;
  assert.equal(host.status, 0, 'Could not determine the Rust target');
  const triple = host.stdout.trim();
  const cpu = process.arch === 'arm64' ? 'aarch64' : 'x86_64';
  const expected = cpu + (windows ? '-pc-windows-msvc' : '-apple-darwin');
  assert.equal(triple, expected, 'Use the matching native Rust toolchain and Node architecture');
  return triple;
}

function checkTauriLinker(triple) {
  const parent = resolve(tmpdir());
  const directory = mkdtempSync(join(parent, 'zhixue-tauri-check-'));
  try {
    const manifest = join(directory, 'Cargo.toml');
    mkdirSync(join(directory, 'src'));
    writeFileSync(manifest, '[package]\nname = "zhixue-toolchain-probe"\nversion = "0.0.0"\nedition = "2021"\n');
    writeFileSync(join(directory, 'src', 'main.rs'), 'fn main() {}\n');
    const result = spawnSync('cargo', [
      'build', '--offline', '--release', '--target', triple,
      '--manifest-path', manifest, '--target-dir', join(directory, 'target'),
    ], { cwd: join(root, 'src-tauri'), encoding: 'utf8', windowsHide: true });
    if (result.error?.code === 'ENOENT') throw new Error('Tauri needs Cargo for the native Rust toolchain.');
    if (result.error) throw result.error;
    if (result.status !== 0) {
      const detail = result.stderr?.trim() || String(result.signal ?? result.status);
      throw new Error('Tauri could not link a native Windows executable. Check the Visual Studio C++ Build Tools and Windows SDK.\n' + detail);
    }
  } finally {
    assert.equal(dirname(directory), parent);
    rmSync(directory, { recursive: true, force: true, maxRetries: 3 });
  }
}

function buildTauri(triple) {
  copyFileSync(binary, join(serviceDirectory, 'zhixue-service-' + triple + (windows ? '.exe' : '')));
  const target = tauriBuildDirectory(root, { configured: process.env.CARGO_TARGET_DIR });
  console.log('Tauri Cargo output directory ' + target);
  const bundle = windows ? 'nsis' : 'app';
  run(process.execPath, [tauri, 'build', '--config', 'src-tauri/tauri.package.json', '--features', 'desktop-bundle',
    '--target', triple, '--bundles', bundle, '--no-sign', '--ci', '--', '--locked'],
  { CARGO_TARGET_DIR: target, CARGO_INCREMENTAL: '0' });

  const results = join(target, triple, 'release', 'bundle', windows ? 'nsis' : 'macos');
  const deliverables = readdirSync(results).filter(name => windows
    ? name.includes('_' + project.version + '_') && name.endsWith('-setup.exe')
    : name === 'Zhixue Tauri.app');
  assert.equal(deliverables.length, 1, 'Could not find the expected Tauri bundle');
  const output = join(release, 'tauri');
  mkdirSync(output, { recursive: true });
  const [deliverable] = deliverables;
  const published = windows ? 'Zhixue-Tauri-' + project.version + '-' + process.arch + '.exe' : deliverable;
  const source = join(results, deliverable);
  const destination = join(output, published);
  if (windows) publishWindowsInstaller(source, destination);
  else cpSync(source, destination, { recursive: true, verbatimSymlinks: true });
}

const requested = process.argv[2] ?? 'all';
assert(['all', 'electron', 'electron-dmg', 'tauri', 'service'].includes(requested),
  'Choose all, electron, electron-dmg, tauri or service');
if (requested === 'electron-dmg') {
  assert(process.platform === 'darwin' && process.arch === 'arm64', 'electron-dmg requires an Apple silicon Mac and arm64 Node.js');
}
assert(['darwin', 'win32'].includes(process.platform) && ['x64', 'arm64'].includes(process.arch),
  'Build desktop packages natively on Windows or macOS');
const triple = requested === 'all' || requested === 'tauri' ? tauriTarget() : null;
if (triple && windows) checkTauriLinker(triple);
await buildService();
if (requested !== 'service') {
  run(process.execPath, [tauri, 'icon', join(root, 'public', 'zhixue.png'), '-o', iconDirectory]);
  if (requested === 'all' || requested === 'electron') await buildElectron();
  if (requested === 'electron-dmg') await buildElectronDmg();
  if (triple) buildTauri(triple);
}
