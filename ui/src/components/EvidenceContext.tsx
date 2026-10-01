import type { ControlPlaneState } from '../data/controlPlaneController.ts';
import { workspaceEvidenceMode } from '../domain/pipelineView.ts';
import { resolveProofFocus } from '../domain/proofFocus.ts';
import { scopeLabel, sourceAvailability } from '../domain/scope.ts';
import { connectionCopy, evidenceStateLabel } from './AppShell.model.ts';

const snapshotFormatter = new Intl.DateTimeFormat('fr-FR', {
  day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: 'UTC',
});

export interface EvidencePresentation {
  readonly label: string;
  readonly shortLabel: string;
  readonly tone: 'simulation' | 'live' | 'unknown';
}

export function evidencePresentation(state: ControlPlaneState): EvidencePresentation {
  if (state.status === 'loading' || state.status === 'failed') {
    return { label: 'État des données non confirmé', shortLabel: 'NON ÉTABLI', tone: 'unknown' };
  }

  const workspaceMode = workspaceEvidenceMode(state.overview);
  const proofFocus = resolveProofFocus(state);
  const proofScope = proofFocus.proofScope;
  const availability = sourceAvailability(state.overview);

  if (proofScope.kind === 'cached') {
    return { label: 'Dernier relevé conservé · hors connexion', shortLabel: 'CACHE', tone: 'unknown' };
  }
  if (availability === 'unestablished' || availability === 'unavailable' || workspaceMode === 'unestablished' || proofScope.kind === 'unavailable') {
    return { label: 'État des données non confirmé', shortLabel: 'NON ÉTABLI', tone: 'unknown' };
  }
  if (availability === 'partial' || proofScope.kind === 'partial') {
    return { label: 'Informations partielles', shortLabel: 'PARTIEL', tone: 'unknown' };
  }
  if (workspaceMode === 'mixed' || proofScope.kind === 'mixed') {
    return { label: 'Données de natures différentes', shortLabel: 'MIXTE', tone: 'unknown' };
  }
  if (proofScope.kind === 'stale') {
    return {
      label: workspaceMode === 'simulation'
        ? 'Démonstration · pas à jour'
        : workspaceMode === 'historical'
          ? 'Relevé historique · pas à jour'
          : 'Relevé pas à jour',
      shortLabel: workspaceMode === 'simulation' ? 'SIM' : workspaceMode === 'historical' ? 'HIST' : 'NON COURANT',
      tone: workspaceMode === 'live' ? 'unknown' : 'simulation',
    };
  }
  if (workspaceMode === 'simulation') return { label: 'Démonstration', shortLabel: 'SIM', tone: 'simulation' };
  if (workspaceMode === 'historical') return { label: 'Relevé historique', shortLabel: 'HIST', tone: 'simulation' };
  if (proofScope.kind === 'live' && workspaceMode === 'live') {
    return proofFocus.firstBreak
      ? { label: 'Données à vérifier', shortLabel: 'NON ÉTABLI', tone: 'unknown' }
      : { label: 'Relevé récent', shortLabel: 'LIVE', tone: 'live' };
  }
  return { label: 'État des données non confirmé', shortLabel: 'NON ÉTABLI', tone: 'unknown' };
}

export function EvidenceContext({ state }: { readonly state: ControlPlaneState }) {
  const hasSnapshot = state.status !== 'loading' && state.status !== 'failed';
  const evidence = evidencePresentation(state);
  const currentTransport = evidence.tone === 'live' && evidence.shortLabel === 'LIVE';
  const connection = connectionCopy(state.connection, state.status, currentTransport ? 'current' : 'technical');
  const scope = scopeLabel(hasSnapshot ? state.overview.scope : null);
  const snapshot = hasSnapshot ? state.overview.generatedAt : null;
  const receivedAt = hasSnapshot ? state.lastSuccessAt : null;

  return (
    <section className="evidence-context" aria-label="Détails des données">
      <header>
        <p>Détails des données</p>
        <strong>{scope}</strong>
      </header>
      <dl>
        <div><dt>État des données</dt><dd>{evidenceStateLabel(evidence.shortLabel)}</dd></div>
        <div><dt>Service</dt><dd>{connection.label}</dd></div>
        <div>
          <dt>Relevé du</dt>
          <dd>{snapshot ? <time dateTime={snapshot}>{snapshotFormatter.format(new Date(snapshot))} UTC</time> : 'Non disponible'}</dd>
        </div>
        <div>
          <dt>Reçu le</dt>
          <dd>{receivedAt ? <time dateTime={receivedAt.toISOString()}>{snapshotFormatter.format(receivedAt)} UTC</time> : 'Non reçu'}</dd>
        </div>
      </dl>
    </section>
  );
}
