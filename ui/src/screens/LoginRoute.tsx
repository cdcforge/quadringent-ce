import { useEffect, useState } from 'react';
import { Login } from './Login.tsx';
import { createCockpitClient } from '../data/cockpitClient.ts';
import type { AuthUser, ControlPlaneV2Client } from '../data/controlPlaneV2Client.ts';

/**
 * Chargement paresseux du client `/v2` pour l'écran de connexion — même
 * principe que `WizardOnboarding` (`createWizardClient`) : un client réel en
 * production, démo en développement sans control plane déployé. Après
 * connexion, retourne à l'écran demandé (`returnTo`, posé par
 * `data/authRedirect.ts` sur une redirection `401`) ou, à défaut, au
 * cockpit v2.
 */
export function LoginRoute({
  returnTo,
  prefilledEmail,
}: {
  readonly returnTo?: string;
  readonly prefilledEmail?: string;
}) {
  const [client, setClient] = useState<ControlPlaneV2Client | null>(null);

  useEffect(() => {
    let cancelled = false;
    void createCockpitClient().then((created) => {
      if (!cancelled) setClient(created);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const onLoggedIn = (_user: AuthUser) => {
    if (typeof window === 'undefined') return;
    window.location.hash = returnTo || '#/cockpit';
  };

  if (!client) {
    return (
      <div className="wizard">
        <p role="status">Préparation de la connexion…</p>
      </div>
    );
  }

  return <Login client={client} prefilledEmail={prefilledEmail ?? ''} onLoggedIn={onLoggedIn} />;
}
