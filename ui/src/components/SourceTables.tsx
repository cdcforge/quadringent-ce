import { Fragment, useState } from 'react';
import type { FluxIdentity } from '../domain/controlPlane.ts';
import { sequences } from '../domain/format.ts';
import type { BoardTableRow } from '../domain/liveBoard.ts';
import { fluxObjectCoverage } from '../domain/sourceDetail.ts';
import '../styles/source-tables.css';

/**
 * Grille des tables — l'objet signature, partagée entre le plateau (L0) et
 * l'onglet Tables du détail source (L2).
 *
 * Une ligne par table : nom métier · état fusionné (phase + verdict) · lignes
 * copiées/total avec barre de volume relatif · images journal. En mode détail
 * (`detailed`), une colonne Identité et une ligne de couverture par le flux
 * rejoignent les faits d'expansion. Tout ce qui n'est pas mesuré par table
 * est dit « non instrumenté », jamais zéro.
 */

export function copiedCopy(row: BoardTableRow): string {
  if (row.copiedRows === null && row.totalRows === null) return 'Inconnu — non mesuré';
  const copied = row.copiedRows === null ? 'inconnu' : sequences(row.copiedRows);
  const total = row.totalRows === null ? 'inconnu' : sequences(row.totalRows);
  return `${copied} / ${total}`;
}

export function journalImagesLabel(row: BoardTableRow): string {
  if (row.journalImages === null) return 'Inconnu — catalogue non lu';
  return row.journalImages;
}

export function identityStatusLabel(row: BoardTableRow): string {
  switch (row.identityStatus) {
    case 'keyed': return 'Clé métier';
    case 'rrn': return 'RRN';
    case 'blocked': return 'Non prouvée';
    default: return 'Inconnu — catalogue non lu';
  }
}

export function SourceTables({
  rows,
  pipelineId,
  footer,
  detailHref,
  detailed = false,
  flux,
  footnote = null,
}: {
  readonly rows: readonly BoardTableRow[];
  readonly pipelineId: string;
  readonly footer: string;
  /** L0 uniquement — lien vers le détail de la source. */
  readonly detailHref?: string;
  /** L2 — ajoute la colonne Identité et la couverture par les objets du flux. */
  readonly detailed?: boolean;
  readonly flux?: FluxIdentity | null;
  /** Note de distinction sous le footer (ex. catalogue déclaré vs copiées
   *  mesurées) — jamais un avertissement inventé. */
  readonly footnote?: string | null;
}) {
  const [open, setOpen] = useState<string | null>(null);
  const columnCount = detailed ? 5 : 4;

  return (
    <table className="source-tables">
      <caption className="sr-only">{`Tables de ${pipelineId} — problèmes en tête`}</caption>
      <thead>
        <tr>
          <th scope="col">Table</th>
          <th scope="col">État</th>
          <th scope="col">Copiées / total · volume relatif</th>
          <th scope="col">Images journal</th>
          {detailed && <th scope="col">Identité</th>}
        </tr>
      </thead>
      <tbody>
        {rows.length === 0 && (
          <tr>
            <td colSpan={columnCount} className="source-tables__empty">
              Aucune table dans le relevé — catalogue et exécution non projetés
            </td>
          </tr>
        )}
        {rows.map((row) => {
          const expanded = open === row.name;
          const detailId = `${pipelineId}-table-${row.name}`;
          return (
            <Fragment key={row.name}>
              <tr
                className={`source-table${row.problem !== null ? ' source-table--problem' : ''}`}
                data-table={row.name}
              >
                <th scope="row">
                  <button
                    type="button"
                    className="source-table__toggle"
                    aria-expanded={expanded}
                    aria-controls={detailId}
                    onClick={() => setOpen(expanded ? null : row.name)}
                  >
                    <span className="source-table__chevron" aria-hidden="true">
                      {expanded ? '▾' : '▸'}
                    </span>
                    <span className="mono">{row.name}</span>
                  </button>
                </th>
                <td>
                  <span className={`table-verdict table-verdict--${row.state.tone}`}>
                    {row.state.primary}
                  </span>
                  {row.state.secondary !== null && (
                    <span className="source-table__phase-sub">{row.state.secondary}</span>
                  )}
                </td>
                <td className="source-table__copied">
                  <span className="source-table__count" data-numeric>
                    {copiedCopy(row)}
                  </span>
                  {row.volumeShare !== null && (
                    <span
                      className="source-progress"
                      role="img"
                      aria-label={`Volume relatif ${Math.round(row.volumeShare * 100)} % de la plus grande table`}
                    >
                      <i style={{ width: `${Math.max(1, Math.round(row.volumeShare * 100))}%` }} />
                    </span>
                  )}
                </td>
                <td className={row.problem === 'journal' ? 'source-table__flag' : undefined}>
                  {journalImagesLabel(row)}
                </td>
                {detailed && (
                  <td className={row.identityStatus === 'blocked' ? 'source-table__flag' : undefined}>
                    {identityStatusLabel(row)}
                  </td>
                )}
              </tr>
              {expanded && (
                <tr className="source-table__detail" id={detailId}>
                  <td colSpan={columnCount}>
                    <dl className="source-table__facts">
                      <div>
                        <dt>Volume catalogue</dt>
                        <dd>
                          {row.totalRows !== null
                            ? `${sequences(row.totalRows)} lignes${
                                row.cataloguedAt !== null ? ` · relevé ${row.cataloguedAt}` : ''
                              }`
                            : 'Inconnu — catalogue non lu'}
                        </dd>
                      </div>
                      <div>
                        <dt>Copie mesurée</dt>
                        <dd>
                          {row.progress !== null
                            ? `${Math.round(row.progress * 100)} % de la table copiée`
                            : row.copiedRows !== null
                              ? `${sequences(row.copiedRows)} lignes copiées — total inconnu`
                              : 'Inconnue — copie non mesurée'}
                        </dd>
                      </div>
                      <div>
                        <dt>Images journal</dt>
                        <dd>
                          {row.journalImages === '*BOTH'
                            ? 'Avant + après (*BOTH)'
                            : row.journalImages === '*AFTER'
                              ? 'Après chaque changement (*AFTER)'
                              : row.journalImages === '*BEFORE'
                                ? 'Avant chaque changement (*BEFORE) — non exploitable'
                                : 'Inconnu — catalogue non lu'}
                        </dd>
                      </div>
                      <div>
                        <dt>Copie historique</dt>
                        <dd>
                          {row.cataloguedAt === null
                            ? 'Inconnue — plan non lu'
                            : row.admitted
                              ? 'Admise au manifeste'
                              : 'Non admise au manifeste'}
                        </dd>
                      </div>
                      <div>
                        <dt>Phase mesurée</dt>
                        <dd>{`${row.phaseLabel} · ${copiedCopy(row)}`}</dd>
                      </div>
                      <div>
                        <dt>Identité</dt>
                        <dd>
                          {row.identityStatus === 'proven' || row.identityStatus === 'keyed'
                            ? 'Prouvée — clé métier'
                            : row.identityStatus === 'rrn'
                              ? 'Prouvée — position physique (RRN)'
                              : row.identityStatus === 'blocked'
                                ? `Non prouvée${
                                    row.blockedReasons.length > 0
                                      ? ` — ${row.blockedReasons.join(', ')}`
                                      : ''
                                  }`
                                : 'Inconnue — catalogue non lu'}
                        </dd>
                      </div>
                      {detailed && (
                        <div>
                          <dt>Couverture du flux</dt>
                          <dd>{fluxObjectCoverage(flux, row.name)}</dd>
                        </div>
                      )}
                      <div>
                        <dt>Mesures par table</dt>
                        <dd>
                          Non mesurées par table — débit et destination agrégés à l’ensemble du flux
                        </dd>
                      </div>
                    </dl>
                    {detailHref !== undefined && (
                      <a className="source-table__more" href={detailHref}>
                        Ouvrir le détail de la source
                      </a>
                    )}
                  </td>
                </tr>
              )}
            </Fragment>
          );
        })}
      </tbody>
      <tfoot>
        <tr>
          <td colSpan={columnCount} className="source-tables__footer">
            {footer}
            {footnote !== null && (
              <span className="source-tables__note">{footnote}</span>
            )}
          </td>
        </tr>
      </tfoot>
    </table>
  );
}
