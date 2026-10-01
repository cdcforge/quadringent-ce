/**
 * Tables — où en est la copie, table par table.
 *
 * Une seule question par ligne : est-ce que cette table est là ? Les réglages
 * de capture (images de journal, mode d'identification des lignes) restent
 * dans l'onglet Mesures de la liaison.
 */

import { useState } from 'react';
import type { ControlPlaneState } from '../data/useControlPlane.ts';
import type { Overview, Pipeline } from '../domain/controlPlane.ts';
import { tableRows, tablesSummary, type TableRow } from '../domain/tableView.ts';
import { href } from '../router.ts';
import '../styles/liaison.css';
import '../styles/tables.css';

export function Pipelines({
  state,
  pipelines,
  onRefresh,
}: {
  readonly state: ControlPlaneState;
  readonly overview: Overview | null;
  readonly pipelines: readonly Pipeline[];
  readonly onRefresh: () => void;
}) {
  const [query, setQuery] = useState('');
  const ready = state.status !== 'loading' && state.status !== 'failed';
  const needle = query.trim().toLowerCase();

  return (
    <div className="board">
      <header className="board__head">
        <div>
          <h1 className="board__title">Tables</h1>
          <p className="board__summary">Vos tables AS400 et l’avancement de leur copie.</p>
        </div>
        <button type="button" className="board__refresh" onClick={onRefresh}>
          Actualiser
        </button>
      </header>

      {!ready || pipelines.length === 0 ? (
        <section className="board__empty">
          <h2>{ready ? 'Aucune table' : 'Lecture en cours'}</h2>
          <p>
            {ready
              ? 'Les tables apparaîtront dès qu’une liaison en déclarera.'
              : 'Quadringent interroge le service.'}
          </p>
        </section>
      ) : (
        <div className="board__list">
          {pipelines.map((pipeline) => {
            const rows = tableRows(pipeline);
            const shown = needle ? rows.filter((row) => row.name.toLowerCase().includes(needle)) : rows;
            return (
              <section className="tables" key={pipeline.id} aria-labelledby={`tables-${pipeline.id}`}>
                <header className="tables__head">
                  <div>
                    <h2 className="tables__title" id={`tables-${pipeline.id}`}>
                      <a href={href({ name: 'pipeline', id: pipeline.id })}>{pipeline.id}</a>
                    </h2>
                    <p className="tables__summary">{tablesSummary(rows)}</p>
                  </div>
                  {rows.length > 8 && (
                    <input
                      className="tables__search"
                      type="search"
                      value={query}
                      placeholder="Chercher une table"
                      aria-label={`Chercher une table de ${pipeline.id}`}
                      onChange={(event) => setQuery(event.target.value)}
                    />
                  )}
                </header>

                {shown.length === 0 ? (
                  <p className="tables__none">Aucune table ne correspond à « {query} ».</p>
                ) : (
                  <ul className="tables__list">
                    {shown.map((row) => (
                      <TableLine row={row} key={row.name} />
                    ))}
                  </ul>
                )}
              </section>
            );
          })}
        </div>
      )}
    </div>
  );
}

function TableLine({ row }: { readonly row: TableRow }) {
  return (
    <li className={`tables__row tables__row--${row.state}`}>
      <span className="tables__name">{row.name}</span>
      <span className="tables__state">{row.stateLabel}</span>
      {row.progress !== null && (
        <span
          className="tables__progress"
          role="img"
          aria-label={`Copie à ${Math.round(row.progress * 100)} %`}
        >
          <span className="tables__progress-fill" style={{ width: `${row.progress * 100}%` }} />
        </span>
      )}
      <span className={row.rows === null ? 'tables__rows tables__rows--absent' : 'tables__rows'} data-numeric="">
        {row.rows ?? 'Non mesuré'}
        {row.rows !== null && <span className="tables__rows-unit"> lignes</span>}
      </span>
    </li>
  );
}
