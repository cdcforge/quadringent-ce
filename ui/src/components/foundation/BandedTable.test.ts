import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createElement, type ComponentType } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';

interface Row { readonly id: string; readonly name: string; readonly rows: number; }

let vite: ViteDevServer;
let BandedTable: ComponentType<any>;

before(async () => {
  vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'silent' });
  BandedTable = (await vite.ssrLoadModule('/src/components/foundation/BandedTable.tsx')).BandedTable;
});

after(async () => { await vite.close(); });

const rows: readonly Row[] = [
  { id: 'a', name: 'CLIENT', rows: 1204 },
  { id: 'b', name: 'COMMANDE', rows: 88031 },
];

const columns = [
  { key: 'name', header: 'Table', render: (row: Row) => row.name },
  { key: 'rows', header: 'Lignes', numeric: true, render: (row: Row) => row.rows.toLocaleString('fr-FR') },
];

test('les colonnes numériques rendent leurs cellules en mono, alignées à droite', () => {
  const markup = renderToStaticMarkup(createElement(BandedTable, {
    caption: 'Tables sources', columns, rows, rowKey: (row: Row) => row.id,
  }));
  assert.match(markup, /banded-table__col--numeric mono/);
  assert.match(markup, /1(\s| | )204/);
});

test('chaque ligne est focalisable au clavier, une seule à la fois dans l’ordre de tabulation', () => {
  const markup = renderToStaticMarkup(createElement(BandedTable, {
    caption: 'Tables sources', columns, rows, rowKey: (row: Row) => row.id,
  }));
  const tabIndexes = [...markup.matchAll(/tabindex="(-?\d)"/g)].map((match) => match[1]);
  assert.deepEqual(tabIndexes, ['0', '-1']);
});

test('un emplacement d’actions par ligne se rend en dernière colonne quand il est fourni', () => {
  const markup = renderToStaticMarkup(createElement(BandedTable, {
    caption: 'Tables sources', columns, rows, rowKey: (row: Row) => row.id,
    rowActions: (row: Row) => createElement('button', { type: 'button' }, `Inspecter ${row.name}`),
  }));
  assert.match(markup, /Inspecter CLIENT/);
  assert.match(markup, /banded-table__col--actions/);
});

test('sans emplacement d’actions, aucune colonne d’actions n’est rendue', () => {
  const markup = renderToStaticMarkup(createElement(BandedTable, {
    caption: 'Tables sources', columns, rows, rowKey: (row: Row) => row.id,
  }));
  assert.doesNotMatch(markup, /banded-table__col--actions/);
});
