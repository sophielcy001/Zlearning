import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { existsSync, mkdtempSync, readdirSync, rmdirSync, unlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { test } from 'node:test';

async function exampleImage(action) {
  const directory = mkdtempSync(join(tmpdir(), 'zhixue-dmg-fixture-'));
  const image = join(directory, 'trial.dmg');
  const content = 'simulated disk image bytes';
  writeFileSync(image, content);
  try {
    await action(image, createHash('sha256').update(content).digest('hex'));
  } finally {
    unlinkSync(image);
    rmdirSync(directory);
  }
}

test('DMG verification exercises both the embedded service and a signed app copy after unmounting', async () => {
  const { verifyMacDmg } = await import('./mac-dmg.mjs');
  await exampleImage(async (image, digest) => {
    const calls = [];
    let mount, copy, data;
    const recorded = await verifyMacDmg(image, {
      run(tool, args) {
        calls.push([tool, args]);
        if (tool === 'hdiutil' && args[0] === 'attach') mount = args.at(-1);
        if (tool === 'ditto') copy = args[1];
        if (tool === 'readlink') return '/Applications';
        if (tool === 'lipo') return 'arm64';
        return '';
      },
      async verifySidecar(executable) {
        calls.push(['sidecar', [executable]]);
      },
      async verifyApp(executable, workspace) {
        data = workspace;
        assert.equal(existsSync(mount), false);
        assert.deepEqual(readdirSync(workspace), []);
        calls.push(['app', [executable, workspace]]);
      },
    });
    assert.equal(recorded, digest);
    assert(mount);
    assert(copy && data);
    assert.equal(dirname(copy), dirname(data));
    assert.notEqual(dirname(copy), mount);
    const app = join(mount, 'Zhixue Electron.app');
    const service = join(app, 'Contents', 'Resources', 'zhixue-service');
    assert.deepEqual(calls, [
      ['hdiutil', ['verify', image]],
      ['hdiutil', ['attach', image, '-readonly', '-nobrowse', '-mountpoint', mount]],
      ['readlink', [join(mount, 'Applications')]],
      ['codesign', ['--verify', '--deep', '--strict', app]],
      ['codesign', ['--verify', '--strict', service]],
      ['lipo', ['-archs', join(app, 'Contents', 'MacOS', 'Zhixue Electron')]],
      ['lipo', ['-archs', service]],
      ['sidecar', [service]],
      ['ditto', [app, copy]],
      ['hdiutil', ['detach', mount]],
      ['codesign', ['--verify', '--deep', '--strict', copy]],
      ['app', [join(copy, 'Contents', 'MacOS', 'Zhixue Electron'), data]],
    ]);
    assert.equal(existsSync(mount), false);
    assert.equal(existsSync(dirname(copy)), false);
  });
});

test('an invalid embedded architecture cannot pass verification and leaves no mounted verification directory', async () => {
  const { verifyMacDmg } = await import('./mac-dmg.mjs');
  await exampleImage(async (image) => {
    let mount, detached;
    await assert.rejects(verifyMacDmg(image, {
      run(tool, args) {
        if (tool === 'hdiutil' && args[0] === 'attach') mount = args.at(-1);
        if (tool === 'hdiutil' && args[0] === 'detach') detached = args[1];
        if (tool === 'readlink') return '/Applications';
        if (tool === 'lipo') return 'x86_64';
        return '';
      },
      async verifySidecar() {
        assert.fail('An invalid app must be rejected before starting the service');
      },
      async verifyApp() {
        assert.fail('An invalid image must be rejected before starting its app');
      },
    }), /App executable must be arm64/);
    assert(mount);
    assert.equal(detached, mount);
    assert.equal(existsSync(mount), false);
  });
});

test('a copied app that cannot start fails DMG verification and its temporary workspace is cleaned', async () => {
  const { verifyMacDmg } = await import('./mac-dmg.mjs');
  await exampleImage(async (image) => {
    let trial;
    await assert.rejects(verifyMacDmg(image, {
      run(tool) {
        if (tool === 'readlink') return '/Applications';
        if (tool === 'lipo') return 'arm64';
        return '';
      },
      async verifySidecar() {},
      async verifyApp(_executable, workspace) {
        trial = dirname(workspace);
        writeFileSync(join(workspace, 'fixture.sqlite3'), 'open failed');
        throw new Error('Copied app did not start');
      },
    }), /Copied app did not start/);
    assert(trial);
    assert.equal(existsSync(trial), false);
  });
});
