import { formatDuration, sequences } from './format.ts';
import { parseSiteIdentity, type SiteIdentity } from './siteIdentity.ts';

export const onboardingSteps = [
  'source',
  'permissions',
  'journal',
  'destination',
  'pilot',
  'verdict',
  'activate',
] as const;

export type OnboardingStep = (typeof onboardingSteps)[number];
export type OnboardingStatus = 'ok' | 'error' | 'review' | 'blocked' | 'ready';
export type OnboardingRisk = 'low' | 'medium' | 'high';

export interface OnboardingDraft {
  readonly ibmiHost: string;
  readonly ibmiUser: string;
  readonly secretRefName: string;
  readonly secretRefKey: string;
  readonly schema: string;
  readonly tables: readonly string[];
  readonly journalLibrary: string;
  readonly journalName: string;
  readonly snowflakeDatabase: string;
  readonly snowflakeSchema: string;
  readonly snowflakeStage: string;
}

export interface OnboardingVerdict {
  readonly step: OnboardingStep;
  readonly status: OnboardingStatus;
  readonly errors: readonly string[];
  readonly blocked: readonly string[];
  readonly unproven: readonly string[];
  readonly proven: readonly string[];
  readonly declared: readonly string[];
  readonly nextAction: string;
  readonly risk: OnboardingRisk;
}

export interface OnboardingStepCopy {
  readonly kicker: string;
  readonly title: string;
  readonly lead: string;
  readonly primary: string;
  readonly secondary: string;
}

export type OnboardingIndexState = 'done' | 'current' | 'todo';

export type OnboardingGroupId = 'as400' | 'snowflake' | 'data';

export interface OnboardingGroup {
  readonly id: OnboardingGroupId;
  readonly label: string;
  readonly description: string;
  readonly steps: readonly OnboardingStep[];
}

export interface OnboardingIndexItem {
  readonly id: OnboardingGroupId;
  readonly index: number;
  readonly label: string;
  readonly description: string;
  readonly state: OnboardingIndexState;
  readonly entryStep: OnboardingStep;
}

export const onboardingGroupDefinitions: readonly OnboardingGroup[] = [
  {
    id: 'as400',
    label: 'Votre AS400',
    description: 'La source de vos données',
    steps: ['source', 'permissions', 'journal'],
  },
  {
    id: 'snowflake',
    label: 'Snowflake',
    description: 'L’endroit où elles seront déposées',
    steps: ['destination'],
  },
  {
    id: 'data',
    label: 'Vos données',
    description: 'Le périmètre que vous préparez',
    steps: ['pilot', 'verdict', 'activate'],
  },
];

export function onboardingIndex(current: OnboardingStep): readonly OnboardingIndexItem[] {
  const currentIndex = onboardingSteps.indexOf(current);
  return onboardingGroupDefinitions.map((group, index) => {
    const firstStepIndex = onboardingSteps.indexOf(group.steps[0]!);
    const lastStepIndex = onboardingSteps.indexOf(group.steps[group.steps.length - 1]!);
    return {
      id: group.id,
      index: index + 1,
      label: group.label,
      description: group.description,
      state: currentIndex < firstStepIndex ? 'todo' : currentIndex > lastStepIndex ? 'done' : 'current',
      entryStep: group.steps[0]!,
    };
  });
}

export function onboardingGroupForStep(step: OnboardingStep): OnboardingGroup {
  return onboardingGroupDefinitions.find((group) => group.steps.includes(step)) ?? onboardingGroupDefinitions[0]!;
}

const statuses = new Set<OnboardingStatus>(['ok', 'error', 'review', 'blocked', 'ready']);
const risks = new Set<OnboardingRisk>(['low', 'medium', 'high']);
const steps = new Set<OnboardingStep>(onboardingSteps);

/**
 * Brouillon pré-rempli depuis l'identité publiée par le service — aucune
 * valeur d'installation n'est codée dans l'interface. « DEMOLIB » est la
 * convention IBM i du journal rattaché à la bibliothèque des données, pas
 * une valeur de site.
 */
export function onboardingDraftFor(site: SiteIdentity): OnboardingDraft {
  return {
    ibmiHost: site.ibmiHost,
    ibmiUser: site.ibmiUser,
    secretRefName: site.secretRefName,
    secretRefKey: site.secretRefKey,
    schema: site.sourceSchema,
    // Le périmètre préparé par le produit est le manifeste complet : la
    // sélection est pré-remplie et reste modifiable, le verdict dit ensuite
    // si le choix est cohérent avec ce qui est préparé.
    tables: [...site.manifest],
    journalLibrary: 'DEMOLIB',
    journalName: site.journalName,
    snowflakeDatabase: site.destinationDatabase,
    snowflakeSchema: site.destinationSchema,
    snowflakeStage: site.snowflakeStage,
  };
}

export function onboardingStepCopy(step: OnboardingStep): OnboardingStepCopy {
  switch (step) {
    case 'source':
      return {
        kicker: 'Votre AS400',
        title: 'Adresse et compte',
        lead: 'Le compte indiqué servira à préparer le flux.',
        primary: 'Continuer',
        secondary: 'Annuler',
      };
    case 'permissions':
      return {
        kicker: 'Votre AS400',
        title: 'Gestion des accès',
        lead: 'Ces accès sont préparés par votre administrateur : rien n’est à saisir sur cette étape.',
        primary: 'Continuer',
        secondary: 'Retour',
      };
    case 'journal':
      return {
        kicker: 'Votre AS400',
        title: 'Choisir vos données',
        lead: 'La bibliothèque et les tables choisies seront vérifiées par le service.',
        primary: 'Continuer',
        secondary: 'Retour',
      };
    case 'destination':
      return {
        kicker: 'Snowflake',
        title: 'Votre destination',
        lead: 'La destination de développement est définie pour ce flux : elle n’est pas modifiable ici.',
        primary: 'Continuer',
        secondary: 'Retour',
      };
    case 'pilot':
      return {
        kicker: 'Vos données',
        title: 'Relire votre installation',
        lead: 'Le récapitulatif ci-dessous sera transmis au service pour vérification.',
        primary: 'Continuer',
        secondary: 'Retour',
      };
    case 'verdict':
      return {
        kicker: 'Vos données',
        title: 'Vérification des réglages',
        lead: 'Le service signale chaque réglage bloquant avec son motif.',
        primary: 'Continuer',
        secondary: 'Retour',
      };
    case 'activate':
      return {
        kicker: 'Vos données',
        title: 'Activation indisponible',
        lead: 'Rien ne démarrera depuis cet écran.',
        primary: 'Continuer',
        secondary: 'Retour',
      };
  }
}

export function nextOnboardingStep(step: OnboardingStep): OnboardingStep | null {
  const index = onboardingSteps.indexOf(step);
  return index >= 0 && index < onboardingSteps.length - 1 ? onboardingSteps[index + 1]! : null;
}

export function previousOnboardingStep(step: OnboardingStep): OnboardingStep | null {
  const index = onboardingSteps.indexOf(step);
  return index > 0 ? onboardingSteps[index - 1]! : null;
}

export function buildOnboardingPayload(draft: OnboardingDraft, step: OnboardingStep, site: SiteIdentity): Record<string, unknown> {
  return {
    step,
    ibmi_host: draft.ibmiHost.trim(),
    ibmi_user: draft.ibmiUser.trim(),
    tls: true,
    allow_plaintext: false,
    tls_ca_file: site.tlsCaFile,
    secret_ref_name: draft.secretRefName.trim(),
    secret_ref_key: draft.secretRefKey.trim(),
    schema: draft.schema.trim(),
    tables: [...draft.tables],
    journal_library: draft.journalLibrary.trim(),
    journal_name: draft.journalName.trim(),
    snowflake_database: draft.snowflakeDatabase.trim(),
    snowflake_schema: draft.snowflakeSchema.trim(),
    snowflake_stage: draft.snowflakeStage.trim(),
  };
}

export function parseOnboardingVerdict(payload: unknown): OnboardingVerdict {
  if (typeof payload !== 'object' || payload === null || Array.isArray(payload)) {
    throw new Error('Verdict d’onboarding invalide');
  }
  const value = payload as Record<string, unknown>;
  const step = value.step;
  const status = value.status;
  const risk = value.risk;
  if (value.verification_scope !== 'configuration_only' || status === 'ready'
      || !Array.isArray(value.proven) || value.proven.length !== 0 || !Array.isArray(value.declared)) {
    throw new Error('Contrat de validation de configuration incompatible');
  }
  if (typeof step !== 'string' || !steps.has(step as OnboardingStep)) {
    throw new Error('Étape d’onboarding invalide');
  }
  if (typeof status !== 'string' || !statuses.has(status as OnboardingStatus)) {
    throw new Error('Statut d’onboarding invalide');
  }
  if (typeof risk !== 'string' || !risks.has(risk as OnboardingRisk)) {
    throw new Error('Risque d’onboarding invalide');
  }
  if (typeof value.next_action !== 'string' || !value.next_action.trim()) {
    throw new Error('Prochaine action absente');
  }
  return {
    step: step as OnboardingStep,
    status: status as OnboardingStatus,
    errors: stringList(value.errors),
    blocked: stringList(value.blocked),
    unproven: stringList(value.unproven),
    proven: stringList(value.proven),
    declared: stringList(value.declared),
    nextAction: value.next_action.trim(),
    risk: risk as OnboardingRisk,
  };
}

export function onboardingUnavailableCopy(reason: 'loading' | 'offline'): { readonly title: string; readonly detail: string } {
  if (reason === 'loading') {
    return {
      title: 'Vérification en cours',
      detail: 'Le service contrôle la cohérence des réglages saisis.',
    };
  }
  return {
    title: 'Vérification indisponible',
    detail: 'Le service de configuration ne répond pas. Votre saisie est conservée : réessayez dans un instant.',
  };
}

function stringList(value: unknown): readonly string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === 'string' && item.trim().length > 0);
}

/** Réglages publiés par GET /v1/onboarding/defaults. Le service ne publie que
 *  des primitives techniques ; l'écran les accompagne d'un libellé métier et
 *  n'en invente aucune lorsqu'elles sont absentes. */
export interface OnboardingDefaults {
  readonly values: Readonly<Record<string, number | boolean | string>>;
  /** Identité publique du site déclaré — requise pour lire les documents de
   *  flotte et pré-remplir le formulaire. */
  readonly site: SiteIdentity;
}

export interface OnboardingDefaultRow {
  readonly key: string;
  readonly label: string;
  readonly value: string;
  readonly raw: string;
}

const ONBOARDING_DEFAULT_LABELS: Readonly<Record<string, string>> = {
  batch_entries: 'Taille des lots de lecture',
  reader_timeout_seconds: 'Délai de lecture maximal',
  poll_seconds: 'Cadence de lecture',
  max_consecutive_errors: 'Erreurs consécutives tolérées',
  pilot_max_seconds: 'Durée maximale du pilote',
  pilot_max_polls: 'Vérifications du pilote',
  replica_count_at_rest: 'Composants actifs au repos',
  tls: 'Chiffrement TLS',
  allow_plaintext: 'Connexion non chiffrée',
  tls_ca_file: 'Certificat de confiance',
};

export function parseOnboardingDefaults(payload: unknown): OnboardingDefaults {
  if (typeof payload !== 'object' || payload === null || Array.isArray(payload)) {
    throw new Error('Réglages publiés invalides');
  }
  const defaults = (payload as Record<string, unknown>).defaults;
  if (typeof defaults !== 'object' || defaults === null || Array.isArray(defaults)) {
    throw new Error('Réglages publiés invalides');
  }
  const values: Record<string, number | boolean | string> = {};
  for (const [key, value] of Object.entries(defaults)) {
    const keep =
      typeof value === 'boolean'
      || (typeof value === 'number' && Number.isFinite(value))
      || (typeof value === 'string' && value.trim().length > 0);
    if (keep) values[key] = value as number | boolean | string;
  }
  return { values, site: parseSiteIdentity((payload as Record<string, unknown>).site) };
}

export function onboardingDefaultsRows(defaults: OnboardingDefaults): readonly OnboardingDefaultRow[] {
  return Object.entries(defaults.values).map(([key, value]) => ({
    key,
    label: ONBOARDING_DEFAULT_LABELS[key] ?? key,
    value: onboardingDefaultValue(key, value),
    raw: typeof value === 'string' ? value : String(value),
  }));
}

function onboardingDefaultValue(key: string, value: number | boolean | string): string {
  if (typeof value === 'boolean') {
    if (key === 'allow_plaintext') return value ? 'Autorisée' : 'Interdite';
    if (key === 'tls') return value ? 'Obligatoire' : 'Non exigé';
    return value ? 'Oui' : 'Non';
  }
  if (typeof value === 'number') {
    if (key === 'pilot_max_seconds' || key === 'reader_timeout_seconds' || key === 'poll_seconds') {
      return formatDuration(value);
    }
    if (key === 'replica_count_at_rest' && value === 0) return 'Aucun';
    return sequences(value);
  }
  return value;
}
