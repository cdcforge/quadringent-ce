import type { ReactNode } from 'react';
import { wizardCheckLabel } from '../../domain/operator.ts';
import type { CheckResult } from '../../data/controlPlaneV2Client.ts';

/** En-tête commun aux quatre écrans de l'assistant v2 — un `<h1>` focusable
 *  hors ordre de tabulation, comme le parcours v1 (`Setup.tsx`). */
export function WizardHeader({ kicker, title, lead }: { readonly kicker: string; readonly title: string; readonly lead: string }) {
  return (
    <header>
      <p className="wizard__kicker">{kicker}</p>
      <h1 className="wizard__title" tabIndex={-1}>{title}</h1>
      <p className="wizard__lead">{lead}</p>
    </header>
  );
}

export function WizardField({
  id,
  label,
  value,
  onChange,
  error,
  type = 'text',
}: {
  readonly id: string;
  readonly label: string;
  readonly value: string;
  readonly onChange: (value: string) => void;
  readonly error?: string | null;
  readonly type?: 'text' | 'password';
}) {
  const errorId = `${id}-error`;
  return (
    <p className="wizard__field">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        type={type}
        value={value}
        aria-invalid={error ? 'true' : undefined}
        aria-describedby={error ? errorId : undefined}
        onChange={(event) => onChange(event.target.value)}
      />
      {error ? <span id={errorId} className="wizard__field-error" role="alert">{error}</span> : null}
    </p>
  );
}

/** Une ligne « à la StatusWord » pour un contrôle de vérification
 *  (réseau/certificat/authentification/rôle/utilisateur/…) : le mot est
 *  toujours visible à côté de la couleur (WCAG 1.4.1). */
export function CheckLine({ label, check }: { readonly label: string; readonly check: CheckResult }) {
  return (
    <li className={`wizard-check-line wizard-check-line--${check.state}`}>
      <span aria-hidden="true" className="wizard-check-line__mark" />
      <span className="wizard-check-line__label">
        {label} — {wizardCheckLabel(check.state)}
      </span>
      <span className="sr-only"> : {check.message}</span>
      {check.message ? <span aria-hidden="true"> ({check.message})</span> : null}
    </li>
  );
}

export function WizardPrimaryRow({ children }: { readonly children: ReactNode }) {
  return <div className="wizard__primary-row">{children}</div>;
}
