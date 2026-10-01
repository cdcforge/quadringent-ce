import { lstatSync, readFileSync, readdirSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { dirname, join, posix, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const scriptDir = dirname(fileURLToPath(import.meta.url));
const distDir = process.argv[2]
  ? resolve(process.cwd(), process.argv[2])
  : join(scriptDir, '..', 'dist');

if (!readdirSync(distDir, { withFileTypes: true }).length) {
  throw new Error('Le build UI n’a produit aucun artefact dans dist/');
}

const offenders = [];
const forbiddenFragments = [
  'console-dev.json',
  'vite_use_fixture',
  'fixture://',
  '/fixtures/',
  'ui/fixtures',
  'data/fixtures',
  'fixture_fallback',
];

for (const file of walk(distDir)) {
  if (file.toLowerCase().includes('fixture')) {
    offenders.push(file);
    continue;
  }

  try {
    const content = readFileSync(file, 'utf8').toLowerCase();
    if (forbiddenFragments.some((fragment) => content.includes(fragment))) {
      offenders.push(file);
    }
  } catch {
    continue;
  }
}

if (offenders.length > 0) {
  throw new Error(`Le build ne doit contenir aucune fixture ou fallback de démonstration : ${offenders.join(', ')}`);
}

verifyViteManifest();

function verifyViteManifest() {
  const sourceIndex = readFileSync(join(scriptDir, '..', 'index.html'), 'utf8');
  const sourceEntries = moduleSources(sourceIndex);
  if (sourceEntries.length !== 1 || sourceEntries[0] !== '/src/main.tsx') {
    throw new Error('ui/index.html doit référencer uniquement /src/main.tsx');
  }

  const manifest = JSON.parse(readFileSync(join(distDir, '.vite', 'manifest.json'), 'utf8'));
  const entries = Object.entries(manifest).filter(([, item]) => item?.isEntry === true);
  if (entries.length !== 1 || entries[0][0] !== 'index.html' || entries[0][1]?.src !== 'index.html') {
    throw new Error('Le manifeste Vite doit contenir l’entrée canonique index.html');
  }
  const entryFile = entries[0][1].file;
  const builtEntries = moduleSources(readFileSync(join(distDir, 'index.html'), 'utf8'));
  if (!isCanonicalPath(entryFile) || builtEntries.length !== 1 || builtEntries[0] !== `/${entryFile}`) {
    throw new Error('L’entrée JavaScript du manifeste doit être référencée par dist/index.html');
  }

  const pending = ['index.html'];
  const visited = new Set();
  while (pending.length > 0) {
    const key = pending.pop();
    if (visited.has(key)) continue;
    visited.add(key);
    if (!isCanonicalPath(key)) {
      throw new Error(`Clé logique de chunk non canonique : ${key}`);
    }
    const item = manifest[key];
    if (!item || typeof item !== 'object' || !isCanonicalPath(item.file)) {
      throw new Error(`Chunk Vite absent ou invalide : ${key}`);
    }
    if (!item.file.endsWith('.js') && !item.file.endsWith('.mjs')) {
      throw new Error(`Tout chunk atteint doit être JavaScript : ${item.file}`);
    }
    const syntax = spawnSync(process.execPath, ['--check', join(distDir, item.file)], {
      encoding: 'utf8',
    });
    if (syntax.status !== 0) {
      throw new Error(`Chunk JavaScript invalide : ${item.file}`);
    }
    for (const field of ['imports', 'dynamicImports']) {
      const dependencies = item[field] ?? [];
      if (!Array.isArray(dependencies) || dependencies.some((value) => typeof value !== 'string')) {
        throw new Error(`Champ manifeste invalide : ${key}.${field}`);
      }
      pending.push(...dependencies);
    }
  }
}

function moduleSources(markup) {
  return scriptStartTags(markup)
    .map((tag) => {
      const attributes = parseAttributes(tag);
      const type = attributes.get('type');
      if (type === undefined) return undefined;
      if (type === null || type.includes('&')) {
        throw new Error('Attribut type ambigu ou encodé sur une balise script');
      }
      if (type.toLowerCase() !== 'module') return undefined;
      const source = attributes.get('src');
      if (source?.includes('&')) {
        throw new Error('Attribut src encodé sur une balise script module');
      }
      return source ?? null;
    })
    .filter((value) => value !== undefined);
}

function scriptStartTags(markup) {
  const lower = markup.toLowerCase();
  const tags = [];
  let cursor = 0;
  while (cursor < markup.length) {
    const start = lower.indexOf('<script', cursor);
    if (start < 0) break;
    const boundary = markup[start + '<script'.length];
    if (boundary && !/[\s/>]/.test(boundary)) {
      cursor = start + '<script'.length;
      continue;
    }
    let quote = null;
    let end = start + '<script'.length;
    for (; end < markup.length; end += 1) {
      const character = markup[end];
      if (quote !== null) {
        if (character === quote) quote = null;
      } else if (character === '"' || character === "'") {
        quote = character;
      } else if (character === '>') {
        break;
      }
    }
    if (end >= markup.length) {
      throw new Error('Balise script non terminée dans index.html');
    }
    tags.push(markup.slice(start, end + 1));
    cursor = end + 1;
  }
  return tags;
}

function parseAttributes(tag) {
  const attributes = new Map();
  let cursor = '<script'.length;
  while (cursor < tag.length) {
    while (/\s/.test(tag[cursor] ?? '')) cursor += 1;
    if (cursor >= tag.length || tag[cursor] === '>' || tag[cursor] === '/') break;
    const nameStart = cursor;
    while (cursor < tag.length && !/[\s=/>]/.test(tag[cursor])) cursor += 1;
    const name = tag.slice(nameStart, cursor).toLowerCase();
    if (!name) throw new Error('Attribut script invalide');
    while (/\s/.test(tag[cursor] ?? '')) cursor += 1;
    let value = null;
    if (tag[cursor] === '=') {
      cursor += 1;
      while (/\s/.test(tag[cursor] ?? '')) cursor += 1;
      const quote = tag[cursor];
      if (quote === '"' || quote === "'") {
        cursor += 1;
        const valueStart = cursor;
        while (cursor < tag.length && tag[cursor] !== quote) cursor += 1;
        if (cursor >= tag.length) throw new Error(`Attribut script non terminé : ${name}`);
        value = tag.slice(valueStart, cursor);
        cursor += 1;
      } else {
        const valueStart = cursor;
        while (cursor < tag.length && !/[\s>]/.test(tag[cursor])) cursor += 1;
        value = tag.slice(valueStart, cursor);
      }
    }
    // HTML conserve la première occurrence d'un attribut dupliqué.
    if (!attributes.has(name)) attributes.set(name, value);
  }
  return attributes;
}

function isCanonicalPath(value) {
  if (typeof value !== 'string' || value.length === 0 || value.includes('\\') || value.includes('%')) {
    return false;
  }
  const segments = value.split('/');
  return segments.every((segment) => segment !== '' && segment !== '.' && segment !== '..')
    && posix.normalize(value) === value;
}

function walk(directory) {
  const entries = readdirSync(directory, { withFileTypes: true });
  const files = [];

  for (const entry of entries) {
    const path = join(directory, entry.name);
    const metadata = lstatSync(path);
    if (metadata.isSymbolicLink()) {
      throw new Error(`Le build ne doit contenir aucun lien symbolique : ${path}`);
    }
    if (entry.isDirectory()) {
      files.push(...walk(path));
    } else if (entry.isFile()) {
      files.push(path);
    }
  }

  return files;
}
