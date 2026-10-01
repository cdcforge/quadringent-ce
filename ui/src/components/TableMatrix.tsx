import { useState } from 'react';
import { sequences } from '../domain/format.ts';
import type { BoardTableRow } from '../domain/liveBoard.ts';
import { copiedCopy, identityStatusLabel, journalImagesLabel } from './SourceTables.tsx';

/**
 * Matrice des tables — le remplacement visuel du tableau texte.
 *
 * Une cellule par table : la couleur porte l'état (vert certifiée, ambre
 * live ou à vérifier, gris non mesuré/préparé, rouge bloquée), la hauteur
 * de remplissage code le volume relatif de la table dans le catalogue.
 * Le nom tient en un mot sous la cellule ; les chiffres exacts vivent en
 * tooltip et dans le panneau d'expansion — jamais en surface.
 *
 * L'honnêteté est intacte : « non mesuré » rend « — », jamais 0 ni vert.
 */

type CellTone = 'ok' | 'live' | 'warn' | 'down' | 'off';

/* La couleur porte l'état MESURÉ (row.phase), pas le ton écran (muté quand
 * le relevé est retenu). Une table certifiée figée rend une cellule verte
 * que la classe `tmat--held` désature — « photo fanée » de l'état connu,
 * jamais une case vide qui ferait croire à une absence de donnée. */
function cellTone(row: BoardTableRow): CellTone {
  if (row.problem === 'blocked') return 'down';
  if (row.problem === 'journal') return 'warn';
  if (row.problem === 'unknown' || row.phase === null) return 'off';
  switch (row.phase) {
    case 'CERTIFIED': return 'ok';
    case 'LIVE':
    case 'CATCHING_UP':
    case 'RECONCILING':
    case 'HISTORICAL': return 'live';
    case 'PAUSED': return 'warn';
    default: return 'off'; // READY, PREPARED, UNKNOWN — pas encore mesuré
  }
}

/** Glyphe minimal dans la cellule — la couleur parle d'abord, le glyphe
 *  nomme ce que la couleur ne peut pas : blocage, pause, non mesuré. */
function cellGlyph(row: BoardTableRow): string | null {
  if (row.problem === 'blocked') return '!';
  if (row.problem === 'journal') return '~';
  if (row.paused) return '‖';
  if (row.problem === 'unknown' || row.phase === null) return '—';
  return null;
}

function cellTitle(row: BoardTableRow): string {
  const state = row.state.secondary !== null ? `${row.state.primary} · ${row.state.secondary}` : row.state.primary;
  return `${row.name} — ${state} · ${copiedCopy(row)} · images ${journalImagesLabel(row)} · identité ${identityStatusLabel(row)}`;
}

export function TableMatrix({
  rows,
  pipelineId,
  detailHref,
  held = false,
}: {
  readonly rows: readonly BoardTableRow[];
  readonly pipelineId: string;
  /** L0 — lien vers le détail de la source dans le panneau d'expansion. */
  readonly detailHref?: string;
  /** Relevé retenu/figé — la matrice se désature en « photo fanée ». */
  readonly held?: boolean;
}) {
  const [open, setOpen] = useState<string | null>(null);
  const detailId = `${pipelineId}-tables-detail`;
  const openRow = rows.find((row) => row.name === open) ?? null;

  return (
    <div className={`tmat${held ? ' tmat--held' : ''}`}>
      <p className="sr-only">{`Tables de ${pipelineId} — problèmes en tête`}</p>
      <ul className="tmat__cells">
        {rows.length === 0 && (
          <li className="tmat__empty">
            Aucune table dans le relevé — catalogue et exécution non projetés
          </li>
        )}
        {rows.map((row) => {
          const tone = cellTone(row);
          const glyph = cellGlyph(row);
          const expanded = open === row.name;
          return (
            <li key={row.name} className="tmat__item">
              <button
                type="button"
                className={`tcell tcell--${tone}${row.problem !== null ? ' tcell--problem' : ''}`}
                data-table={row.name}
                aria-expanded={expanded}
                aria-controls={detailId}
                aria-label={`${row.name} — ${row.state.primary}`}
                title={cellTitle(row)}
                onClick={() => setOpen(expanded ? null : row.name)}
              >
                <span className="tcell__box" aria-hidden="true">
                  {row.volumeShare !== null && (
                    <i
                      className="tcell__fill"
                      style={{ height: `${Math.max(4, Math.round(row.volumeShare * 100))}%` }}
                    />
                  )}
                  {glyph !== null && <i className="tcell__glyph">{glyph}</i>}
                </span>
                <span className="tcell__name">{row.name}</span>
                {row.volumeShare !== null && (
                  <span className="sr-only">{`Volume relatif ${Math.round(row.volumeShare * 100)} % de la plus grande table`}</span>
                )}
              </button>
            </li>
          );
        })}
      </ul>
      <div className="tmat__detail" id={detailId}>
        {openRow !== null && <TableFacts row={openRow} detailHref={detailHref} />}
      </div>
    </div>
  );
}

/** Faits exacts d'une table — la couche chiffres, à la demande. */
function TableFacts({ row, detailHref }: { readonly row: BoardTableRow; readonly detailHref?: string }) {
  return (
    <div className="tmat__facts">
      <dl className="source-table__facts">
        <div>
          <dt>État</dt>
          <dd>
            {row.state.primary}
            {row.state.secondary !== null ? ` · ${row.state.secondary}` : ''}
          </dd>
        </div>
        <div>
          <dt>Volume catalogue</dt>
          <dd>
            {row.totalRows !== null
              ? `${sequences(row.totalRows)} lignes${row.cataloguedAt !== null ? ` · relevé ${row.cataloguedAt}` : ''}`
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
          <dt>Identité</dt>
          <dd>
            {row.identityStatus === 'proven' || row.identityStatus === 'keyed'
              ? 'Prouvée — clé métier'
              : row.identityStatus === 'rrn'
                ? 'Prouvée — position physique (RRN)'
                : row.identityStatus === 'blocked'
                  ? `Non prouvée${row.blockedReasons.length > 0 ? ` — ${row.blockedReasons.join(', ')}` : ''}`
                  : 'Inconnue — catalogue non lu'}
          </dd>
        </div>
        <div>
          <dt>Mesures par table</dt>
          <dd>Non mesurées par table — débit et destination agrégés à l’ensemble du flux</dd>
        </div>
      </dl>
      {detailHref !== undefined && (
        <a className="source-table__more" href={detailHref}>
          Ouvrir le détail de la source
        </a>
      )}
    </div>
  );
}
