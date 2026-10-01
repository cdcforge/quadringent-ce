/**
 * Cockpit — Connexion : liste bandée des tables de la connexion, triable et
 * filtrable, avec les contrôles aux trois échelles pertinentes ici :
 * cette connexion (toutes ses tables), sa destination, et « tout »
 * (rappel du contrôle flotte de l'Accueil — même emplacement, disponible
 * partout, design §3).
 */
import { useEffect, useState } from 'react';
import { BandedTable, type BandedTableColumn } from '../../components/foundation/BandedTable.tsx';
import { StatusWord } from '../../components/foundation/StatusWord.tsx';
import { FunctionKeyBar } from '../../components/foundation/FunctionKeyBar.tsx';
import { useFunctionKeys, type FunctionKeyBinding } from '../../components/foundation/useFunctionKeys.ts';
import { ControlsPanel } from '../../components/ControlsPanel.tsx';
import { connectionNeedsAttention, sortAndFilterTables, type CockpitConnection, type CockpitTable, type ConnectionTableSortKey, type SortDirection } from '../../domain/cockpit.ts';
import { getCockpitClient, useCockpitConnections } from '../../data/useCockpit.ts';
import { href } from '../../router.ts';
import { requiresConfirmation, type ControlActionKind } from '../../domain/controlsPanel.ts';
import type { CockpitConnectionsState } from '../../data/useCockpit.ts';
import { focusPrimaryControls, navigateBack } from './cockpitFunctionKeys.ts';

export function CockpitConnectionScreen({ connectionId }: { readonly connectionId: string }) {
  const { state, reload } = useCockpitConnections();
  return <CockpitConnectionView state={state} connectionId={connectionId} onRefresh={reload} />;
}

export function CockpitConnectionView({
  state,
  connectionId,
  onRefresh,
}: {
  readonly state: CockpitConnectionsState;
  readonly connectionId: string;
  readonly onRefresh: () => void;
}) {
  const [query, setQuery] = useState('');
  const [sortKey, setSortKey] = useState<ConnectionTableSortKey>('name');
  const [direction, setDirection] = useState<SortDirection>('asc');
  const [scope, setScope] = useState<ControlScope>('connection');

  const ready = state.status === 'ready';
  const connection = ready ? state.connections.find((item) => item.id === connectionId) ?? null : null;

  useEffect(() => {
    document.title = `Quadringent · Cockpit · ${connection ? connection.label : connectionId}`;
  }, [connection, connectionId]);

  const bindings: readonly FunctionKeyBinding[] = [
    { key: 'F3', onTrigger: navigateBack },
    { key: 'F5', onTrigger: onRefresh },
    { key: 'F9', onTrigger: focusPrimaryControls, disabled: !connection },
    { key: 'F12', onTrigger: () => {}, disabled: true },
  ];
  const entries = useFunctionKeys(bindings);
  const functionKeyBar = <FunctionKeyBar entries={entries} onTrigger={(key) => bindings.find((binding) => binding.key === key)?.onTrigger()} />;

  if (state.status === 'loading') {
    return (
      <div className="board cockpit-connection">
        <section className="board__empty"><h2>Lecture en cours</h2><p>Quadringent interroge le service.</p></section>
        {functionKeyBar}
      </div>
    );
  }
  if (state.status === 'failed') {
    return (
      <div className="board cockpit-connection">
        <section className="board__empty" role="alert"><h2>Service indisponible</h2><p>{state.message}</p></section>
        {functionKeyBar}
      </div>
    );
  }

  if (!connection) {
    return (
      <div className="board cockpit-connection">
        <section className="board__empty"><h2>Connexion introuvable</h2><p>Retournez au <a href={href({ name: 'cockpit' })}>cockpit</a>.</p></section>
        {functionKeyBar}
      </div>
    );
  }

  const rows = sortAndFilterTables(connection.tables, query, sortKey, direction);

  return (
    <div className="board cockpit-connection">
      <header className="board__head">
        <div>
          <p className="board__kicker"><a href={href({ name: 'cockpit' })}>Cockpit</a></p>
          <h1 className="board__title">{connection.label}</h1>
          <p className="board__summary">{connection.tables.length} table(s).</p>
        </div>
        <button type="button" className="board__refresh" onClick={onRefresh}>Actualiser</button>
      </header>

      <ScopedControls connection={connection} scope={scope} onScopeChange={setScope} onRefresh={onRefresh} />

      {connectionNeedsAttention(connection) ? (
        <ConnectionAttentionBanner connection={connection} />
      ) : null}

      <div className="cockpit-connection__toolbar">
        <input
          type="search"
          value={query}
          placeholder="Chercher une table"
          aria-label="Chercher une table"
          onChange={(event) => setQuery(event.target.value)}
        />
        <label>
          Trier par
          <select value={sortKey} onChange={(event) => setSortKey(event.target.value as ConnectionTableSortKey)}>
            <option value="name">Nom</option>
            <option value="state">État</option>
          </select>
        </label>
        <button type="button" onClick={() => setDirection((d) => (d === 'asc' ? 'desc' : 'asc'))}>
          {direction === 'asc' ? 'Croissant' : 'Décroissant'}
        </button>
      </div>

      <ConnectionTable rows={rows} />

      {functionKeyBar}
    </div>
  );
}

type ControlScope = 'connection' | 'destination' | 'fleet';

/**
 * Barre d'actions compacte, en tête de page (même emplacement qu'à
 * l'accueil et sur l'écran table — design §1, point 2). Un sélecteur de
 * portée choisit laquelle des trois échelles pertinentes ici (connexion,
 * destination couplée, ou flotte entière) le `ControlsPanel` affiché agit
 * — jamais les trois panneaux empilés en même temps.
 */
function ScopedControls({
  connection,
  scope,
  onScopeChange,
  onRefresh,
}: {
  readonly connection: CockpitConnection;
  readonly scope: ControlScope;
  readonly onScopeChange: (scope: ControlScope) => void;
  readonly onRefresh: () => void;
}) {
  const effectiveScope = scope === 'destination' && !connection.destinationId ? 'connection' : scope;

  return (
    <div id="cockpit-primary-controls" className="cockpit-connection__scoped-controls">
      <label className="cockpit-connection__scope-select">
        Portée
        <select value={effectiveScope} onChange={(event) => onScopeChange(event.target.value as ControlScope)}>
          <option value="connection">Cette connexion</option>
          {connection.destinationId ? <option value="destination">Destination de {connection.label}</option> : null}
          <option value="fleet">Toutes les connexions</option>
        </select>
      </label>

      {effectiveScope === 'connection' ? (
        <ControlsPanel
          level="connection"
          targetLabel={connection.label}
          availableActions={['pause', 'resume'] as readonly ControlActionKind[]}
          dryRun={async (action) => {
            const client = await getCockpitClient();
            return client.runSourceAction(connection.id, action as 'pause' | 'resume', { dryRun: true });
          }}
          execute={async (action) => {
            if (requiresConfirmation(action)) throw new Error('Action non prise en charge au niveau connexion.');
            const client = await getCockpitClient();
            await client.runSourceAction(connection.id, action as 'pause' | 'resume');
            return { pendingConfirmationId: null };
          }}
          verify={async () => { onRefresh(); }}
        />
      ) : null}

      {effectiveScope === 'destination' && connection.destinationId ? (
        <ControlsPanel
          level="destination"
          targetLabel={`Destination de ${connection.label}`}
          availableActions={['pause', 'resume'] as readonly ControlActionKind[]}
          dryRun={async (action) => {
            const client = await getCockpitClient();
            return client.runDestinationAction(connection.destinationId!, action as 'pause' | 'resume', { dryRun: true });
          }}
          execute={async (action) => {
            if (requiresConfirmation(action)) throw new Error('Action non prise en charge au niveau destination.');
            const client = await getCockpitClient();
            await client.runDestinationAction(connection.destinationId!, action as 'pause' | 'resume');
            return { pendingConfirmationId: null };
          }}
          verify={async () => { onRefresh(); }}
        />
      ) : null}

      {effectiveScope === 'fleet' ? (
        <ControlsPanel
          level="fleet"
          targetLabel="Toutes les connexions"
          availableActions={['pause', 'resume'] as readonly ControlActionKind[]}
          dryRun={async (action) => {
            const client = await getCockpitClient();
            return client.runFleetAction(action === 'pause' ? 'pause_all' : 'resume_all', { dryRun: true });
          }}
          execute={async (action) => {
            if (requiresConfirmation(action)) throw new Error('Action non prise en charge au niveau flotte.');
            const client = await getCockpitClient();
            await client.runFleetAction(action === 'pause' ? 'pause_all' : 'resume_all');
            return { pendingConfirmationId: null };
          }}
          verify={async () => { onRefresh(); }}
        />
      ) : null}
    </div>
  );
}

function ConnectionAttentionBanner({ connection }: { readonly connection: CockpitConnection }) {
  const target = connection.tables.find((table) => table.state === 'attention');
  return (
    <p className="cockpit-attention-banner" role="alert">
      <span aria-hidden="true" />
      <span>Au moins une table de cette connexion demande une vérification.</span>
      {target ? (
        <a className="cockpit-banner-link" href={href({ name: 'cockpit-table', id: target.pipelineId })}>
          Voir
        </a>
      ) : null}
    </p>
  );
}

const ROWS_FORMAT = new Intl.NumberFormat('fr-FR');
const ARRIVAL_FORMAT = new Intl.DateTimeFormat('fr-FR', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: 'UTC' });

/** Une figure absente se dit — jamais un zéro fabriqué (règle produit
 *  répétée dans tout le cockpit) — avec sa raison disponible au survol
 *  (`title`), lue dans `absentReasons` publié par le service. */
function AbsentCell({ reason }: { readonly reason: string | undefined }) {
  return <span title={reason ?? 'Non mesuré sur cette fenêtre.'}>—</span>;
}

function ConnectionTable({ rows }: { readonly rows: readonly CockpitTable[] }) {
  const columns: readonly BandedTableColumn<CockpitTable>[] = [
    {
      key: 'name',
      header: 'Table',
      render: (row) => <a href={href({ name: 'cockpit-table', id: row.pipelineId })}>{row.name}</a>,
    },
    {
      key: 'state',
      header: 'État',
      render: (row) => <StatusWord state={row.state} compact />,
    },
    {
      key: 'lag',
      header: 'Retard',
      numeric: true,
      render: (row) => (row.lagSeconds === null ? <AbsentCell reason={row.absentReasons.lag_seconds} /> : `${row.lagSeconds} s`),
    },
    {
      key: 'throughput',
      header: 'Débit',
      numeric: true,
      render: (row) => (row.throughputRowsPerSecond === null
        ? <AbsentCell reason={row.absentReasons.throughput_rows_per_second} />
        : `${ROWS_FORMAT.format(Math.round(row.throughputRowsPerSecond * 60))} lignes/min`),
    },
    {
      key: 'rows',
      header: 'Lignes source / Snowflake',
      numeric: true,
      render: (row) => (
        <>
          {row.rowsSource === null ? <AbsentCell reason={row.absentReasons.rows_source} /> : ROWS_FORMAT.format(row.rowsSource)}
          {' / '}
          {row.rowsDestination === null ? <AbsentCell reason={row.absentReasons.rows_destination} /> : ROWS_FORMAT.format(row.rowsDestination)}
        </>
      ),
    },
    {
      key: 'lastArrival',
      header: 'Dernière arrivée',
      numeric: true,
      render: (row) => (row.lastArrivalAt === null
        ? <AbsentCell reason={row.absentReasons.last_arrival_at} />
        : <time dateTime={row.lastArrivalAt}>{ARRIVAL_FORMAT.format(new Date(row.lastArrivalAt))}</time>),
    },
  ];

  if (rows.length === 0) {
    return <p className="cockpit-connection__none">Aucune table ne correspond à la recherche.</p>;
  }

  return <BandedTable caption="Tables de la connexion" columns={columns} rows={rows} rowKey={(row) => row.pipelineId} />;
}

// Réexporté pour clarté d'import ailleurs (CockpitApp.tsx).
export type { CockpitConnection };
