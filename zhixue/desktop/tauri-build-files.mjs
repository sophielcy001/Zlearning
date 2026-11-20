import { createHash } from 'node:crypto';
import { copyFileSync, mkdtempSync, renameSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';

export function tauriBuildDirectory(root, { configured, platform = process.platform, temp = tmpdir() } = {}) {
  if (configured) return resolve(root, configured);
  if (platform === 'win32') {
    // Cargo adds deep dependency paths, which Windows linkers may be unable to open.
    const workspace = createHash('sha256').update(resolve(root)).digest('hex').slice(0, 16);
    return resolve(temp, 'zhixue-tauri-' + workspace);
  }
  return join(root, '.desktop-build', 'tauri-target');
}

export function publishWindowsInstaller(source, destination) {
  const stage = mkdtempSync(join(dirname(destination), '.tauri-installer-'));
  const pending = join(stage, 'installer.exe');
  try {
    copyFileSync(source, pending);
    renameSync(pending, destination);
  } finally {
    rmSync(stage, { recursive: true, force: true, maxRetries: 3 });
  }
}
