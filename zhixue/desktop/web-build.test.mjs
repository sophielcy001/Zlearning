import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { createHash } from 'node:crypto';
import { cp, mkdir, mkdtemp, readFile, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';

const run = promisify(execFile);
const project = fileURLToPath(new URL('..', import.meta.url));

test('packaging artifacts do not change the mobile worker or web asset digests', async t => {
  const parent = resolve(tmpdir());
  const directory = await mkdtemp(join(parent, 'zhixue-web-build-'));
  t.after(async () => {
    assert.equal(dirname(directory), parent);
    await rm(directory, { recursive: true });
  });

  for (const input of ['.gitignore', 'index.html', 'package.json', 'vite.config.ts', 'mobile', 'public', 'src']) {
    await cp(join(project, input), join(directory, input), { recursive: true });
  }
  await symlink(join(project, 'node_modules'), join(directory, 'node_modules'), 'junction');

  const workerDigest = async () => {
    await run(process.execPath, [join(directory, 'node_modules/vite/bin/vite.js'), 'build'], {
      cwd: directory,
    });
    const worker = await readFile(join(directory, 'dist/mobile/sw.js'));
    return createHash('sha256').update(worker).digest('hex');
  };

  const clean = await workerDigest();
  const unpacked = join(directory, 'release/electron/win-unpacked');
  await mkdir(unpacked, { recursive: true });
  await writeFile(join(unpacked, 'LICENSES.chromium.html'), '<p>grayscale w-[13789px]</p>\n');

  assert.equal(await workerDigest(), clean, 'previous packaging output changed the offline worker manifest');
});
