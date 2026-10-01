import { useState } from 'react';
import { ActionButton } from '../../components/foundation/ActionButton.tsx';
import { WIZARD_COPY } from '../../domain/operator.ts';
import { ControlPlaneV2Error, type ControlPlaneV2Client } from '../../data/controlPlaneV2Client.ts';
import { WizardHeader } from './WizardShell.tsx';

const copy = WIZARD_COPY.activate;

/**
 * Premier écran du premier lancement : activation du compte administrateur
 * via un lien à usage unique (contrat v2 §2.5, §6.4). Le jeton vient du lien
 * reçu par courriel (`?token=...`), jamais saisi à la main.
 */
export function WizardActivate({
  client,
  token,
  onActivated,
}: {
  readonly client: ControlPlaneV2Client;
  readonly token: string | null;
  readonly onActivated: () => void;
}) {
  const [password, setPassword] = useState('');
  const [status, setStatus] = useState<'idle' | 'pending' | 'error'>('idle');
  const [error, setError] = useState<string | null>(null);

  const canSubmit = Boolean(token) && password.trim().length >= 8 && status !== 'pending';

  const activate = async () => {
    if (!token) return;
    setStatus('pending');
    setError(null);
    try {
      const user = await client.activateAdmin(token, password);
      setStatus('idle');
      // Tâche « auth-login » : l'activation seule ne pose plus de session
      // (mode « authentification exigée » côté serveur) — on se connecte
      // immédiatement avec le mot de passe qui vient d'être choisi, pour
      // que la suite de l'assistant (source/snowflake/tables, toutes
      // protégées) continue sans interruption. Si cette connexion
      // automatique échoue pour une raison quelconque, l'écran de
      // connexion prend le relais, email déjà rempli — jamais une erreur
      // technique opaque ici.
      try {
        await client.login(user.email, password);
        onActivated();
      } catch {
        if (typeof window !== 'undefined') {
          window.location.hash = `#/login?email=${encodeURIComponent(user.email)}`;
        }
      }
    } catch (cause) {
      setStatus('error');
      setError(cause instanceof ControlPlaneV2Error ? `${cause.message} ${cause.nextAction}` : 'L’activation a échoué. Réessayez.');
    }
  };

  return (
    <div className="wizard">
      <WizardHeader kicker={copy.kicker} title={copy.title} lead={copy.lead} />
      {!token ? (
        <p className="wizard__field-error" role="alert">
          Ce lien d’activation est invalide ou a déjà été utilisé. Demandez un nouveau lien à votre administrateur.
        </p>
      ) : (
        <>
          <p className="wizard__field">
            <label htmlFor="wizard-activate-password">{copy.passwordLabel}</label>
            <input
              id="wizard-activate-password"
              type="password"
              value={password}
              minLength={8}
              onChange={(event) => setPassword(event.target.value)}
            />
          </p>
          {error ? <p className="wizard__field-error" role="alert">{error}</p> : null}
          <ActionButton label={copy.primary} onAction={() => { void activate(); }} disabled={!canSubmit} primary />
        </>
      )}
    </div>
  );
}
