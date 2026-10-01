import type { WindowDelivery as WindowEvidence } from '../domain/controlPlane.ts';

const dates = new Intl.DateTimeFormat('fr-FR', { dateStyle: 'medium', timeStyle: 'medium', timeZone: 'UTC' });
const provenance = { live: 'Observation réelle', historical: 'Preuve historique', simulation: 'Simulation' };
const freshness = { fresh: 'Observation fraîche', late: 'Observation en retard', stale: 'Preuve ancienne', clock_untrusted: 'Horloge non fiable' };

export function WindowDelivery({ evidence, cached }: { readonly evidence: WindowEvidence | null | undefined; readonly cached: boolean }) {
  if (!evidence) return null;
  const chain=evidence.chain;
  const progress=chain ? <p>{chain.matchedWindows} sur {chain.declaredWindows} fenêtres avec preuve de réconciliation · {provenance[chain.evidenceKind]}{cached ? ' · Dernier état conservé' : ''}</p> : null;
  if (evidence.state === 'invalid' || evidence.state === 'unavailable') {
    return <section className="window-delivery" aria-label="Preuve de fenêtre">
      <h2>{evidence.state === 'invalid' ? 'Preuve de fenêtre invalide' : chain ? 'Chaîne de preuves incomplète' : 'Preuve de fenêtre indisponible'}</h2>
      {progress}
      <p>{chain ? 'Toutes les fenêtres ne sont pas qualifiées. Aucune fenêtre manquante n’est ignorée.' : 'La réconciliation de cette fenêtre ne peut pas être établie.'} Actualisez la preuve ; l’état de capture reste distinct.</p>
    </section>;
  }
  if (!('quality' in evidence)) return null;
  return <section className="window-delivery" aria-label="Preuve de fenêtre">
    <p className="section-kicker">Fenêtre fermée · {provenance[evidence.quality.evidenceKind]} · {cached ? 'Snapshot conservé' : freshness[evidence.quality.freshness]}</p>
    {progress}
    <h2>{evidence.state === 'matched' ? `${new Intl.NumberFormat('fr-FR').format(evidence.eventCount)} ${evidence.eventCount === 1 ? 'événement réconcilié' : 'événements réconciliés'}` : 'Fenêtre vide — livraison non testée'}</h2>
    <p>Du <time dateTime={evidence.startedAt}>{dates.format(new Date(evidence.startedAt))}</time> au <time dateTime={evidence.closedAt}>{dates.format(new Date(evidence.closedAt))}</time> UTC.</p>
    <p>Cette preuve couvre uniquement cette fenêtre, pas la capture actuelle ni les événements suivants.</p>
    {chain && <p>Le nombre d’événements ci-dessus concerne uniquement la dernière fenêtre affichée, pas le total de la chaîne.</p>}
    <details><summary>Provenance de la réconciliation</summary>
      <p>Archive {evidence.archiveRunId} · fenêtre {evidence.windowId}</p>
      <p>Destination observée le <time dateTime={evidence.destinationObservedAt}>{dates.format(new Date(evidence.destinationObservedAt))}</time> UTC.</p>
    </details>
  </section>;
}
