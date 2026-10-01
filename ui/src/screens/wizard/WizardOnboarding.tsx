import { useEffect, useState } from 'react';
import type { WizardStep } from '../../router.ts';
import { href } from '../../router.ts';
import { createWizardClient } from '../../data/wizardClient.ts';
import type { ControlPlaneV2Client, SourceRecord } from '../../data/controlPlaneV2Client.ts';
import { WizardActivate } from './WizardActivate.tsx';
import { WizardSource } from './WizardSource.tsx';
import { WizardSnowflake } from './WizardSnowflake.tsx';
import { WizardTables } from './WizardTables.tsx';

/** L'activation précède la session : aucune lecture protégée à cette étape. */
export function shouldLoadExistingSource(step: WizardStep): boolean {
  return step !== 'activate';
}

/** L'API renvoie les sources par date de création croissante. */
export function selectResumeSource<T>(sources: readonly T[]): T | null {
  return sources.at(-1) ?? null;
}

/**
 * Orchestrateur de l'assistant v2 : un client `/v2` partagé et la navigation
 * entre les quatre écrans du premier lancement. Le client par défaut est
 * chargé de façon paresseuse par `createWizardClient()` (démo en développement,
 * réel sinon — voir `wizardClient.ts`) ; un `client` injecté prend le pas
 * dessus, ce qui garde cet écran testable en rendu serveur synchrone, sans
 * jamais référencer les fixtures depuis ce module.
 *
 * Aucun identifiant de source n'est fixé en dur : `GET /v2/sources` retrouve
 * une source déjà créée pour cette installation (reprise après rechargement,
 * `docs/api-v2.md` §« Enveloppe d'action générique ») ; `WizardSource`
 * notifie l'identifiant réel dès qu'il existe (création ou réutilisation)
 * via `onSourceReady`, conservé ici pour l'écran « Tables » suivant.
 */
export function WizardOnboarding({
  step,
  activationToken = null,
  client: injectedClient,
}: {
  readonly step: WizardStep;
  /** Jeton du lien d'activation à usage unique (`?token=...`) — lu par l'appelant
   *  depuis `window.location.hash`, jamais depuis ce composant, pour rester
   *  testable côté serveur sans DOM. */
  readonly activationToken?: string | null;
  /** Injection pour les tests ; en production, laissé vide pour charger le
   *  client par défaut de façon paresseuse. */
  readonly client?: ControlPlaneV2Client;
}) {
  const [client, setClient] = useState<ControlPlaneV2Client | null>(injectedClient ?? null);
  const [sourceId, setSourceId] = useState<string | null>(null);
  const [existingSource, setExistingSource] = useState<SourceRecord | null>(null);

  useEffect(() => {
    if (injectedClient) return;
    let cancelled = false;
    void createWizardClient().then((created) => { if (!cancelled) setClient(created); });
    return () => { cancelled = true; };
  }, [injectedClient]);

  // Reprise après rechargement : retrouve une source déjà créée pour cette
  // installation (`GET /v2/sources`) — jamais son mot de passe, jamais
  // persisté côté client. Si un nouvel identifiant a été créé pour changer
  // les paramètres, reprendre le plus récent. Ne bloque
  // jamais le premier rendu (SSR synchrone, tests d'écran) : l'écran
  // « Source » se réaffiche simplement une fois la réponse arrivée.
  useEffect(() => {
    if (!client || !shouldLoadExistingSource(step)) return;
    let cancelled = false;
    void client.listSources()
      .then((sources) => {
        if (cancelled) return;
        const latest = selectResumeSource(sources);
        setExistingSource(latest);
        if (latest) setSourceId((current) => current ?? latest.id);
      })
      .catch(() => { /* pas de source existante ou service indisponible — reprise à zéro */ });
    return () => { cancelled = true; };
  }, [client, step]);

  const goto = (next: WizardStep) => {
    if (typeof window !== 'undefined') window.location.hash = href({ name: 'wizard', step: next });
  };

  if (!client) {
    return (
      <div className="wizard">
        <p role="status">Préparation de l’assistant…</p>
      </div>
    );
  }

  switch (step) {
    case 'activate':
      return <WizardActivate client={client} token={activationToken} onActivated={() => goto('source')} />;
    case 'source':
      return (
        <WizardSource
          client={client}
          existingSource={existingSource}
          onSourceReady={setSourceId}
          onBack={() => { if (typeof window !== 'undefined') window.location.hash = href({ name: 'cockpit' }); }}
          onContinue={() => goto('snowflake')}
        />
      );
    case 'snowflake':
      return (
        <WizardSnowflake
          client={client}
          onBack={() => goto('source')}
          onContinue={() => goto('tables')}
        />
      );
    case 'tables':
      return (
        <WizardTables
          client={client}
          sourceId={sourceId ?? existingSource?.id ?? null}
          onBack={() => goto('snowflake')}
          onStarted={(tableIds) => {
            const firstTableId = tableIds[0];
            if (firstTableId && typeof window !== 'undefined') {
              window.location.hash = href({ name: 'pipeline', id: firstTableId, tab: 'live' });
            }
          }}
        />
      );
  }
  return null;
}
