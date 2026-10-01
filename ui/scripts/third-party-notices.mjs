/** Les licences des dépendances accompagnent leur code et leurs polices redistribués. */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export function buildNotices(directory) {
  const root = directory instanceof URL ? fileURLToPath(directory) : directory;
  const lock = JSON.parse(readFileSync(join(root, 'package-lock.json'), 'utf8'));
  const packages = Object.entries(lock.packages).filter(([path, item]) =>
    path && (!item.dev || path === 'node_modules/vite'));
  const notices = ['Avis des dépendances du cockpit Quadringent',
    'Ces composants conservent leurs licences respectives. La licence de Quadringent ne les remplace pas.'];
  for (const [path, item] of packages.sort(([a], [b]) => a.localeCompare(b))) {
    const packageRoot = join(root, path);
    const metadata = JSON.parse(readFileSync(join(packageRoot, 'package.json'), 'utf8'));
    if (metadata.version !== item.version) throw new Error(`Version installée différente du lockfile : ${path}`);
    const license = ['LICENSE', 'LICENSE.md', 'LICENSE.txt', 'license'].find(name => existsSync(join(packageRoot, name)));
    if (!license) throw new Error(`Licence absente : ${metadata.name}`);
    const text = readFileSync(join(packageRoot, license), 'utf8').trim();
    if (!text) throw new Error(`Licence absente : ${metadata.name}`);
    notices.push(`${metadata.name}@${metadata.version} — ${item.license ?? 'voir le texte ci-dessous'}\n\n${text}`);
  }
  const project = ['LICENSE', 'NOTICE'].map(name =>
    `Quadringent — ${name}\n\n${readFileSync(join(root, '..', name), 'utf8').trim()}`);
  return [...project, ...notices].join('\n\n' + '='.repeat(72) + '\n\n') + '\n';
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
  const target = join(root, 'dist', 'assets', 'third-party-notices.txt');
  const notices = buildNotices(root);
  mkdirSync(dirname(target), { recursive: true });
  writeFileSync(target, notices);
}
