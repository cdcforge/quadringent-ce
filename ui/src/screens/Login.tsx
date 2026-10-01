import { useState } from 'react';
import { ActionButton } from '../components/foundation/ActionButton.tsx';
import { WizardHeader, WizardField } from './wizard/WizardShell.tsx';
import { ControlPlaneV2Error, type AuthUser, type ControlPlaneV2Client } from '../data/controlPlaneV2Client.ts';

/**
 * Écran de connexion (tâche « auth-login », contrat §6.2/§9.2). Réutilise
 * les composants/tokens de l'assistant (`WizardHeader`/`WizardField`/
 * `ActionButton`, `wizard.css`) — même sobriété rétro AS400, aucune
 * dépendance ajoutée. Deux origines : un lien d'activation sans session
 * établie (`prefilledEmail`), ou une redirection sur `401` depuis n'importe
 * quel écran `/v2` (`data/authRedirect.ts`) avec l'écran demandé à
 * retrouver après connexion (`onLoggedIn`).
 */
export function Login({
  client,
  prefilledEmail = '',
  onLoggedIn,
}: {
  readonly client: ControlPlaneV2Client;
  readonly prefilledEmail?: string;
  readonly onLoggedIn: (user: AuthUser) => void;
}) {
  const [email, setEmail] = useState(prefilledEmail);
  const [password, setPassword] = useState('');
  const [status, setStatus] = useState<'idle' | 'pending' | 'error'>('idle');
  const [error, setError] = useState<string | null>(null);

  const canSubmit = email.trim().length > 0 && password.length > 0 && status !== 'pending';

  const submit = async () => {
    if (!canSubmit) return;
    setStatus('pending');
    setError(null);
    try {
      const user = await client.login(email.trim(), password);
      setStatus('idle');
      onLoggedIn(user);
    } catch (cause) {
      setStatus('error');
      setError(
        cause instanceof ControlPlaneV2Error
          ? `${cause.message} ${cause.nextAction}`
          : 'La connexion a échoué. Réessayez.',
      );
    }
  };

  return (
    <div className="wizard">
      <WizardHeader
        kicker="Connexion"
        title="Se connecter"
        lead="Identifiez-vous pour accéder au cockpit Quadringent."
      />
      <WizardField id="login-email" label="Adresse e-mail" value={email} onChange={setEmail} />
      <WizardField id="login-password" label="Mot de passe" type="password" value={password} onChange={setPassword} />
      {error ? (
        <p className="wizard__field-error" role="alert">{error}</p>
      ) : null}
      <ActionButton
        label="Se connecter"
        onAction={() => { void submit(); }}
        disabled={!canSubmit}
        primary
      />
    </div>
  );
}
