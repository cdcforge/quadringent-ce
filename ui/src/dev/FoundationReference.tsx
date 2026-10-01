import { useEffect, useState } from 'react';
import { ActionButton } from '../components/foundation/ActionButton.tsx';
import { BandedTable, type BandedTableColumn } from '../components/foundation/BandedTable.tsx';
import { FunctionKeyBar } from '../components/foundation/FunctionKeyBar.tsx';
import { useFunctionKeys } from '../components/foundation/useFunctionKeys.ts';
import { MetricTile } from '../components/foundation/MetricTile.tsx';
import { StatusWord, type StatusWordState } from '../components/foundation/StatusWord.tsx';

/**
 * Page de référence visuelle, atteignable uniquement en développement
 * (`import.meta.env.DEV`, voir `src/main.tsx`) via `#/_fondation`. Jamais
 * incluse dans le contrat produit ni dans le build de production : elle sert
 * la revue du product owner sur les cinq composants de fondation, en clair
 * et en sombre, à la largeur de travail desktop (1440 px).
 */

const STATUS_STATES: readonly StatusWordState[] = ['live', 'copying', 'paused', 'attention', 'stopped'];

interface TableRow {
  readonly id: string;
  readonly library: string;
  readonly table: string;
  readonly rows: number;
}

const TABLE_ROWS: readonly TableRow[] = [
  { id: '1', library: 'QGPL', table: 'CLIENT', rows: 1_204 },
  { id: '2', library: 'QGPL', table: 'COMMANDE', rows: 88_031 },
  { id: '3', library: 'QGPL', table: 'LIGNE_CDE', rows: 412_558 },
  { id: '4', library: 'FACTURE', table: 'ENTETE', rows: 22_910 },
  { id: '5', library: 'FACTURE', table: 'DETAIL', rows: 190_442 },
];

const columns: readonly BandedTableColumn<TableRow>[] = [
  { key: 'library', header: 'Bibliothèque', render: (row) => <span className="mono">{row.library}</span> },
  { key: 'table', header: 'Table', render: (row) => <span className="mono">{row.table}</span> },
  { key: 'rows', header: 'Lignes', numeric: true, render: (row) => row.rows.toLocaleString('fr-FR') },
];

export function FoundationReference() {
  const [theme, setTheme] = useState<'light' | 'dark'>('light');
  const [log, setLog] = useState<string[]>([]);
  const pushLog = (message: string) => setLog((previous) => [message, ...previous].slice(0, 6));

  const functionKeyBindings = [
    { key: 'F3' as const, onTrigger: () => pushLog('F3 — Revenir') },
    { key: 'F5' as const, onTrigger: () => pushLog('F5 — Actualiser') },
    { key: 'F9' as const, onTrigger: () => pushLog('F9 — Pause/Reprise') },
    { key: 'F12' as const, onTrigger: () => pushLog('F12 — Journaux'), disabled: true },
  ];
  const entries = useFunctionKeys(functionKeyBindings);

  // Les jetons de thème sont scopés à :root ; le bascule pose donc l'attribut
  // sur <html>, pas sur ce conteneur, pour que toute la page (y compris la
  // barre de touches, position fixe) suive le thème choisi.
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    return () => { delete document.documentElement.dataset.theme; };
  }, [theme]);

  return (
    <div className="foundation-reference">
      <header className="foundation-reference__header">
        <div>
          <p className="eyebrow">Quadringent — fondation visuelle</p>
          <h1>Composants de fondation</h1>
          <p>Page de revue, développement uniquement. Non atteignable en production.</p>
        </div>
        <button type="button" className="action-button" onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')}>
          Thème : {theme === 'light' ? 'clair' : 'sombre'}
        </button>
      </header>

      <section aria-labelledby="section-status">
        <h2 id="section-status">StatusWord</h2>
        <div className="foundation-reference__row">
          {STATUS_STATES.map((state) => <StatusWord key={state} state={state} />)}
        </div>
        <div className="foundation-reference__row">
          {STATUS_STATES.map((state) => <StatusWord key={state} state={state} compact />)}
        </div>
      </section>

      <section aria-labelledby="section-metric">
        <h2 id="section-metric">MetricTile</h2>
        <div className="foundation-reference__grid">
          <MetricTile label="Retard de lecture" value="42" unit="s" provenance="mesuré" age="il y a 3 min" />
          <MetricTile label="Coût mensuel estimé" value="128,40" unit="€" provenance="estimé" age="il y a 1 h" />
          <MetricTile label="Lignes chargées" value={null} provenance="absent" />
        </div>
      </section>

      <section aria-labelledby="section-table">
        <h2 id="section-table">BandedTable</h2>
        <BandedTable
          caption="Tables sources de démonstration"
          columns={columns}
          rows={TABLE_ROWS}
          rowKey={(row) => row.id}
          rowActions={(row) => (
            <ActionButton label="Inspecter" onAction={() => pushLog(`Inspecter ${row.table}`)} />
          )}
        />
      </section>

      <section aria-labelledby="section-action">
        <h2 id="section-action">ActionButton</h2>
        <div className="foundation-reference__row">
          <ActionButton label="Actualiser" onAction={() => pushLog('Actualiser')} primary />
          <ActionButton label="Mettre en pause" onAction={() => pushLog('Mise en pause confirmée')} requiresConfirmation />
          <ActionButton label="Indisponible" onAction={() => {}} disabled />
        </div>
      </section>

      <section aria-labelledby="section-log">
        <h2 id="section-log">Journal des interactions</h2>
        <ul className="foundation-reference__log">
          {log.length === 0 ? <li>Aucune interaction pour l’instant.</li> : log.map((entry, index) => <li key={index}>{entry}</li>)}
        </ul>
      </section>

      <FunctionKeyBar
        entries={entries}
        onTrigger={(key) => functionKeyBindings.find((binding) => binding.key === key)?.onTrigger()}
      />
    </div>
  );
}
