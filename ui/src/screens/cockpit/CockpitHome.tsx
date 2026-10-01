/**
 * Cockpit — Accueil : une ligne par connexion (design §3, niveau accueil).
 * Un seul bandeau d'attention pour tout l'écran, jamais un par connexion.
 *
 * `CockpitHomeView` est pur (état en prop, comme `Overview.tsx` avec
 * `ControlPlaneState`) : testable sans réseau. `CockpitHome` fait le lien
 * avec le hook de données.
 */
import { BandedTable, type BandedTableColumn } from '../../components/foundation/BandedTable.tsx';
import { StatusWord } from '../../components/foundation/StatusWord.tsx';
import { FunctionKeyBar } from '../../components/foundation/FunctionKeyBar.tsx';
import { useFunctionKeys, type FunctionKeyBinding } from '../../components/foundation/useFunctionKeys.ts';
import { ControlsPanel } from '../../components/ControlsPanel.tsx';
import { connectionAggregateState, fleetNeedsAttention } from '../../domain/cockpit.ts';
import { getCockpitClient, useCockpitHome, type CockpitHomeState, type ConnectionHomeRow } from '../../data/useCockpit.ts';
import { href } from '../../router.ts';
import { requiresConfirmation, type ControlActionKind } from '../../domain/controlsPanel.ts';
import { focusPrimaryControls, navigateBack } from './cockpitFunctionKeys.ts';

export function CockpitHome() {
  const { state, reload } = useCockpitHome();
  return <CockpitHomeView state={state} onRefresh={reload} />;
}

export function CockpitHomeView({ state, onRefresh }: { readonly state: CockpitHomeState; readonly onRefresh: () => void }) {
  const bindings: readonly FunctionKeyBinding[] = [
    { key: 'F3', onTrigger: navigateBack },
    { key: 'F5', onTrigger: onRefresh },
    { key: 'F9', onTrigger: focusPrimaryControls, disabled: state.status !== 'ready' || state.rows.length === 0 },
    { key: 'F12', onTrigger: () => {}, disabled: true },
  ];
  const entries = useFunctionKeys(bindings);

  return (
    <div className="board cockpit-home">
      <header className="board__head">
        <div>
          <h1 className="board__title">Cockpit</h1>
          <p className="board__summary">Une ligne par connexion.</p>
        </div>
        <div className="cockpit-home__header-actions">
          <a className="board__refresh" href={href({ name: 'cockpit-confirmations' })}>Confirmations</a>
          <button type="button" className="board__refresh" onClick={onRefresh}>Actualiser</button>
        </div>
      </header>

      {state.status === 'loading' ? (
        <section className="board__empty">
          <h2>Lecture en cours</h2>
          <p>Quadringent interroge le service.</p>
        </section>
      ) : state.status === 'failed' ? (
        <section className="board__empty" role="alert">
          <h2>Service indisponible</h2>
          <p>{state.message}</p>
        </section>
      ) : state.rows.length === 0 ? (
        <section className="board__empty">
          <h2>Aucune connexion</h2>
          <p>Lancez l’assistant pour connecter une première source.</p>
          <a className="board__refresh" href={href({ name: 'wizard', step: 'source' })}>Lancer l’assistant</a>
        </section>
      ) : (
        <>
          <div id="cockpit-primary-controls">
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
          </div>

          {fleetNeedsAttention(state.rows.map((row) => row.connection)) ? (
            <AttentionBanner rows={state.rows} />
          ) : null}

          <HomeTable rows={state.rows} />
        </>
      )}

      <FunctionKeyBar entries={entries} onTrigger={(key) => bindings.find((binding) => binding.key === key)?.onTrigger()} />
    </div>
  );
}

/** Un seul bandeau, jamais un par connexion (design §3) — mais il pointe
 *  vers la première connexion qui demande une vérification, pour que
 *  « Voir » mène quelque part plutôt que de forcer à chercher dans la
 *  liste. */
function AttentionBanner({ rows }: { readonly rows: readonly ConnectionHomeRow[] }) {
  const target = rows.find((row) => connectionAggregateState(row.connection) === 'attention');
  return (
    <p className="cockpit-attention-banner" role="alert">
      <span aria-hidden="true" />
      <span>Au moins une connexion demande une vérification.</span>
      {target ? (
        <a className="cockpit-banner-link" href={href({ name: 'cockpit-connection', id: target.connection.id })}>
          Voir
        </a>
      ) : null}
    </p>
  );
}

function HomeTable({ rows }: { readonly rows: readonly ConnectionHomeRow[] }) {
  const columns: readonly BandedTableColumn<ConnectionHomeRow>[] = [
    {
      key: 'label',
      header: 'Connexion',
      render: (row) => <a href={href({ name: 'cockpit-connection', id: row.connection.id })}>{row.connection.label}</a>,
    },
    {
      key: 'state',
      header: 'État',
      render: (row) => <StatusWord state={connectionAggregateState(row.connection)} compact />,
    },
    {
      key: 'lag',
      header: 'Retard',
      numeric: true,
      render: (row) => (row.lagSeconds === null ? '—' : `${row.lagSeconds} s`),
    },
    {
      key: 'throughput',
      header: 'Lignes/min',
      numeric: true,
      render: (row) => (row.throughputRowsPerSecond === null ? '—' : Math.round(row.throughputRowsPerSecond * 60).toLocaleString('fr-FR')),
    },
    {
      key: 'cost',
      header: 'Coût du jour',
      numeric: true,
      render: (row) => costCell(row.costToday),
    },
  ];

  return (
    <BandedTable
      caption="Connexions"
      columns={columns}
      rows={rows}
      rowKey={(row) => row.connection.id}
    />
  );
}

function costCell(cost: ConnectionHomeRow['costToday']): string {
  if (!cost || cost.status === 'absent' || cost.amount === null) return 'Absent';
  const amount = `${cost.amount.toFixed(2)} ${cost.currency ?? ''}`.trim();
  return cost.status === 'estimated' ? `${amount} (estimé)` : amount;
}
