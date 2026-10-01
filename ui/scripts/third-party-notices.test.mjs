import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { buildNotices } from './third-party-notices.mjs';

test('les avis conservent les textes complets et les versions installées', () => {
  const ui = new URL('../', import.meta.url);
  const notices = buildNotices(ui);
  for (const name of ['react', 'react-dom', 'scheduler', '@fontsource-variable/ibm-plex-sans', '@fontsource/ibm-plex-mono']) {
    const packageRoot = new URL(`node_modules/${name}/`, ui);
    const metadata = JSON.parse(readFileSync(new URL('package.json', packageRoot), 'utf8'));
    assert.ok(notices.includes(`${name}@${metadata.version}`));
    assert.ok(notices.includes(readFileSync(new URL('LICENSE', packageRoot), 'utf8').trim()));
  }
  assert.match(notices, /vite@/);
  for (const file of ['LICENSE', 'NOTICE']) {
    assert.ok(notices.includes(readFileSync(new URL(`../${file}`, ui), 'utf8').trim()));
  }
});

test('une dépendance sans texte de licence bloque la génération', () => {
  const root = mkdtempSync(join(tmpdir(), 'quadringent-notices-'));
  try {
    mkdirSync(join(root, 'node_modules', 'example'), { recursive: true });
    writeFileSync(join(root, 'package-lock.json'), JSON.stringify({ packages: {
      'node_modules/example': { version: '1.0.0', license: 'MIT' },
    } }));
    writeFileSync(join(root, 'node_modules', 'example', 'package.json'), JSON.stringify({ name: 'example', version: '1.0.0' }));
    assert.throws(() => buildNotices(root), /Licence absente.*example/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
