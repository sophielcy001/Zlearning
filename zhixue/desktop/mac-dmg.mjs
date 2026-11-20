import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { createReadStream, existsSync, mkdirSync, mkdtempSync, realpathSync, rmdirSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { serviceStopped } from './installed-probe.mjs';
import { verifyService } from './verify-service.mjs';

const appName = 'Zhixue Electron.app';
const executableParts = ['Contents', 'MacOS', 'Zhixue Electron'];

export async function fileSHA256(path) {
  const checksum = createHash('sha256');
  for await (const chunk of createReadStream(path)) checksum.update(chunk);
  return checksum.digest('hex');
}

function systemTool(tool, args) {
  return execFileSync(join('/usr/bin', tool), args, { encoding: 'utf8', timeout: 120_000 }).trim();
}

export async function verifyInstalledElectron(executable, workspace) {
  const { _electron } = await import('playwright');
  const directory = realpathSync(workspace);
  const env = { ...process.env, ZHIXUE_DEMO_DATA: directory };
  delete env.ELECTRON_RUN_AS_NODE;
  const app = await _electron.launch({ executablePath: executable, env, timeout: 45_000 });
  const child = app.process();
  let health;
  try {
    const launched = await app.evaluate(({ app }) => ({
      packaged: app.isPackaged, executable: process.execPath, profile: app.getPath('userData'),
    }));
    assert(launched.packaged, 'Expected a packaged Electron app');
    assert.equal(realpathSync(launched.executable), realpathSync(executable), 'Started the wrong Electron executable');
    assert.equal(realpathSync(launched.profile), realpathSync(join(directory, 'profile')));
    const page = await app.firstWindow({ timeout: 25_000 });
    page.setDefaultTimeout(20_000);
    await page.getByRole('heading', { name: '选择知识库位置', exact: true }).waitFor();
    assert.equal(realpathSync(await page.getByLabel('保存文件夹', { exact: true }).inputValue()), directory);
    const address = URL.parse(page.url());
    assert(address?.protocol === 'http:' && address.hostname === '127.0.0.1' && address.port,
      'Installed Electron did not load its loopback service');
    health = new URL('/healthz', address);
    const response = await fetch(health, { redirect: 'error', signal: AbortSignal.timeout(4000) });
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { app: 'zhixue-demo', status: 'ready' });
    await page.getByRole('button', { name: '使用此位置', exact: true }).click();
    await page.getByRole('button', { name: '稍后配置模型', exact: true }).click();
    await page.getByRole('heading', { name: '今天，想弄懂什么？', exact: true }).waitFor();
    assert(statSync(join(directory, 'zhixue.sqlite3')).size > 0, 'First launch did not create a workspace database');
  } finally {
    const limit = setTimeout(() => child.kill('SIGKILL'), 18_000);
    try {
      await app.close();
    } finally {
      clearTimeout(limit);
    }
  }
  assert.equal(child.exitCode, 0, 'Installed Electron did not quit cleanly');
  await serviceStopped({ origin: health.origin });
  console.log('Installed Electron passed isolated startup, workspace creation and bundled service shutdown.');
}

async function copyVerifiedApp(image, copy, run, verifySidecar) {
  run('hdiutil', ['verify', image]);
  const mount = mkdtempSync(join(tmpdir(), 'zhixue-dmg-mount-'));
  let attached = false;
  try {
    run('hdiutil', ['attach', image, '-readonly', '-nobrowse', '-mountpoint', mount]);
    attached = true;
    const app = join(mount, appName);
    const executable = join(app, ...executableParts);
    const service = join(app, 'Contents', 'Resources', 'zhixue-service');

    assert.equal(run('readlink', [join(mount, 'Applications')]), '/Applications', 'DMG must link to Applications');
    run('codesign', ['--verify', '--deep', '--strict', app]);
    run('codesign', ['--verify', '--strict', service]);
    assert.equal(run('lipo', ['-archs', executable]), 'arm64', 'App executable must be arm64');
    assert.equal(run('lipo', ['-archs', service]), 'arm64', 'Bundled service must be arm64');
    await verifySidecar(service);
    run('ditto', [app, copy]);
  } finally {
    if (attached) run('hdiutil', ['detach', mount]);
    if (existsSync(mount)) rmdirSync(mount);
  }
}

export async function verifyMacDmg(image, {
  run = systemTool, verifySidecar = verifyService, verifyApp = verifyInstalledElectron,
} = {}) {
  const trial = mkdtempSync(join(tmpdir(), 'zhixue-dmg-trial-'));
  try {
    const copy = join(trial, appName);
    const workspace = join(trial, 'workspace');
    await copyVerifiedApp(image, copy, run, verifySidecar);
    run('codesign', ['--verify', '--deep', '--strict', copy]);
    mkdirSync(workspace);
    await verifyApp(join(copy, ...executableParts), workspace);
  } finally {
    rmSync(trial, { recursive: true, maxRetries: 3 });
  }

  return fileSHA256(image);
}
