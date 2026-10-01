import { useEffect, useState } from 'react';
import { ActionButton } from '../../components/foundation/ActionButton.tsx';
import { useFunctionKeys } from '../../components/foundation/useFunctionKeys.ts';
import { FunctionKeyBar } from '../../components/foundation/FunctionKeyBar.tsx';
import { WIZARD_COPY } from '../../domain/operator.ts';
import { validateSourceField, canTestSource, prefillExistingSource, shouldCreateSource, type SourceFormValues } from '../../domain/wizardSource.ts';
import { ControlPlaneV2Error, type ControlPlaneV2Client, type SourceRecord, type SourceTestResult } from '../../data/controlPlaneV2Client.ts';
import { CheckLine, WizardHeader, WizardPrimaryRow } from './WizardShell.tsx';

const copy = WIZARD_COPY.source;

export function WizardSource({
  client,
  existingSource,
  onSourceReady,
  onBack,
  onContinue,
}: {
  readonly client: ControlPlaneV2Client;
  /** Source déjà connue pour cette installation, retrouvée par le parent via
   *  `GET /v2/sources` (reprise après rechargement) — `null` au tout premier
   *  lancement. Le mot de passe n'y figure jamais (jamais persisté côté
   *  client) : seuls l'hôte et le compte sont pré-remplis, en rappel. */
  readonly existingSource: SourceRecord | null;
  /** Notifie le parent de l'identifiant réel de la source dès qu'elle existe
   *  (création ou réutilisation), pour l'écran « Tables » suivant. */
  readonly onSourceReady: (sourceId: string) => void;
  readonly onBack: () => void;
  readonly onContinue: () => void;
}) {
  const [values, setValues] = useState<SourceFormValues>({
    host: existingSource?.host ?? '',
    account: existingSource?.ibmiUser ?? '',
    password: '',
  });
  const [touched, setTouched] = useState<Record<keyof SourceFormValues, boolean>>({ host: false, account: false, password: false });
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [port, setPort] = useState('');
  const [sslPort, setSslPort] = useState('');
  const [testResult, setTestResult] = useState<SourceTestResult | null>(null);
  const [testStatus, setTestStatus] = useState<'idle' | 'pending' | 'error'>('idle');
  const [testError, setTestError] = useState<string | null>(null);

  // Aucune route PATCH n'existe pour `/v2/sources` (`routes/sources.py` ne
  // porte que GET/POST/test) : une nouvelle vérification avec des champs
  // modifiés crée une nouvelle source plutôt que de mettre à jour
  // l'existante — choix documenté dans le rapport de session. `sourceId`
  // et `createdWith` suivent la dernière source créée dans cette session ;
  // tant que host/compte n'ont pas changé, un nouveau clic sur « Tester »
  // réutilise le même identifiant plutôt que d'en recréer un à chaque fois.
  const [sourceId, setSourceId] = useState<string | null>(existingSource?.id ?? null);
  const [createdWith, setCreatedWith] = useState<{ readonly host: string; readonly account: string } | null>(
    existingSource ? { host: existingSource.host, account: existingSource.ibmiUser ?? '' } : null,
  );

  // La liste des sources arrive après le premier rendu. Reprendre son identité
  // sans effacer un champ que l'opérateur a déjà commencé à modifier.
  useEffect(() => {
    if (!existingSource) return;
    setValues((current) => prefillExistingSource(current, existingSource));
    setSourceId((current) => current ?? existingSource.id);
    setCreatedWith((current) => current ?? {
      host: existingSource.host,
      account: existingSource.ibmiUser ?? '',
    });
  }, [existingSource]);

  const fieldError = (field: keyof SourceFormValues) => {
    if (!touched[field]) return null;
    const result = validateSourceField(field, values[field]);
    return result.valid ? null : result.message;
  };

  const setField = (field: keyof SourceFormValues) => (value: string) => {
    setValues((current) => ({ ...current, [field]: value }));
    setTestResult(null);
  };
  const markTouched = (field: keyof SourceFormValues) => setTouched((current) => ({ ...current, [field]: true }));

  const savedIdentity = sourceId !== null ? createdWith : null;
  const canTest = canTestSource(values, savedIdentity);

  const runTest = async () => {
    if (!canTest) return;
    setTestStatus('pending');
    setTestError(null);
    setTestResult(null);
    try {
      const needsNewSource = shouldCreateSource(values, savedIdentity);
      let currentSourceId = sourceId;
      if (needsNewSource) {
        // `display_name` est obligatoire côté serveur (`validation.validated_display_name`,
        // motif repris de `connections.py` v1) : l'assistant ne demande pas
        // ce champ à l'opérateur (hors périmètre produit), un nom dérivé de
        // l'hôte est donc généré ici plutôt que d'envoyer `null` (rejeté en
        // `400 invalid_request`).
        const record = await client.createSource({ host: values.host, account: values.account, password: values.password, displayName: `IBM i — ${values.host}` });
        currentSourceId = record.id;
        setSourceId(record.id);
        setCreatedWith({ host: values.host, account: values.account });
        setValues((current) => ({ ...current, password: '' }));
        onSourceReady(record.id);
      }
      const result = await client.testSource(currentSourceId!);
      setTestResult(result);
      setTestStatus('idle');
    } catch (cause) {
      setTestStatus('error');
      setTestError(cause instanceof ControlPlaneV2Error ? `${cause.message} ${cause.nextAction}` : 'Le test a échoué. Réessayez.');
    }
  };

  const canContinue = testResult !== null && sourceId !== null && !shouldCreateSource(values, savedIdentity);

  const bindings = [
    { key: 'F3' as const, onTrigger: onBack },
    { key: 'F5' as const, onTrigger: () => { void runTest(); }, disabled: testStatus === 'pending' || !canTest },
  ];
  const entries = useFunctionKeys(bindings);

  return (
    <div className="wizard">
      <WizardHeader kicker={copy.kicker} title={copy.title} lead={copy.lead} />
      {existingSource ? <p className="wizard__field-hint" role="status">{copy.existingSourceHint}</p> : null}
      <WizardField id="wizard-source-host" label={copy.hostLabel} value={values.host} onChange={setField('host')} onBlur={() => markTouched('host')} error={fieldError('host')} />
      <WizardField id="wizard-source-account" label={copy.accountLabel} value={values.account} onChange={setField('account')} onBlur={() => markTouched('account')} error={fieldError('account')} />
      <WizardField id="wizard-source-password" label={copy.passwordLabel} value={values.password} onChange={setField('password')} onBlur={() => markTouched('password')} error={fieldError('password')} type="password" />

      <details className="wizard__advanced" open={advancedOpen} onToggle={(event) => setAdvancedOpen((event.target as HTMLDetailsElement).open)}>
        <summary>{copy.advanced}</summary>
        <WizardField id="wizard-source-port" label={copy.portLabel} value={port} onChange={setPort} />
        <WizardField id="wizard-source-sslport" label={copy.sslPortLabel} value={sslPort} onChange={setSslPort} />
      </details>

      <ActionButton label={copy.test} onAction={() => { void runTest(); }} disabled={!canTest || testStatus === 'pending'} />

      {testError ? <p className="wizard__field-error" role="alert">{testError}</p> : null}

      {testResult ? <SourceTestResultView result={testResult} /> : null}

      <WizardPrimaryRow>
        <ActionButton label={copy.primary} onAction={onContinue} disabled={!canContinue} primary />
      </WizardPrimaryRow>
      <FunctionKeyBar entries={entries} onTrigger={(key) => bindings.find((binding) => binding.key === key)?.onTrigger()} />
    </div>
  );
}

function SourceTestResultView({ result }: { readonly result: SourceTestResult }) {
  if (result.kind === 'unavailable') {
    return (
      <p className="wizard__field-hint" role="status" aria-live="polite">
        {copy.unavailable}
      </p>
    );
  }
  return (
    <ul className="wizard__check-list" aria-live="polite">
      <CheckLine label="Réseau" check={result.network} />
      <CheckLine label="Certificat" check={result.tls} />
      <CheckLine label="Authentification" check={result.authentication} />
      <li>IBM i : {result.ibmiVersion ?? 'Non détectée'}</li>
      <li>
        Fuseau horaire détecté : {result.detectedTimeZone ?? 'Non détecté'}
        {result.timezoneAmbiguous ? ' (à confirmer)' : ''}
      </li>
    </ul>
  );
}

function WizardField({
  id,
  label,
  value,
  onChange,
  onBlur,
  error,
  type = 'text',
}: {
  readonly id: string;
  readonly label: string;
  readonly value: string;
  readonly onChange: (value: string) => void;
  readonly onBlur?: () => void;
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
        onBlur={onBlur}
      />
      {error ? <span id={errorId} className="wizard__field-error" role="alert">{error}</span> : null}
    </p>
  );
}
