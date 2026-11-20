import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { defineConfig, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

function mobileShell(): Plugin {
  return {
    name: 'zhixue-offline-shell',
    apply: 'build',
    writeBundle(options, bundle) {
      const directory = options.dir!;
      const shell = readFileSync('mobile/worker.js', 'utf8');
      const assets = [
        '/mobile/', '/mobile/manifest.webmanifest', '/mobile/icon-192.png', '/mobile/icon-512.png', '/zhixue.png',
        ...Object.keys(bundle).filter(name => name.startsWith('assets/')).sort().map(name => '/' + name),
      ];
      const integrity = Object.fromEntries(assets.map(path => {
        const file = resolve(directory, path === '/mobile/' ? 'mobile/index.html' : path.slice(1));
        return [path, 'sha256-' + createHash('sha256').update(readFileSync(file)).digest('base64')];
      }));
      const manifest = JSON.stringify(integrity);
      const version = createHash('sha256').update(shell).update(manifest).digest('hex').slice(0, 20);
      writeFileSync(resolve(directory, 'mobile/sw.js'), [
        `const CACHE = "zhixue-mobile-${version}";`,
        `const PRECACHE = ${JSON.stringify(assets)};`,
        `const INTEGRITY = ${manifest};`,
        shell,
      ].join('\n'));
    },
  };
}

export default defineConfig({
  plugins: [react(), tailwindcss(), mobileShell()],
  build: { rollupOptions: { input: { desktop: resolve('index.html'), mobile: resolve('mobile/index.html') } } },
});
