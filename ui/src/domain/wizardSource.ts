/**
 * Assistant v2 — étape « Source IBM i » (validation en direct par champ,
 * docs/plans/2026-09-23-produit-fini-design.md §2, contrat v2 §2.1).
 *
 * Chaque champ est validé indépendamment, avec un message correctif en
 * français ; aucune valeur n'est jamais devinée pour l'utilisateur.
 */

export type SourceFieldName = 'host' | 'account' | 'password';

export interface FieldValidation {
  readonly valid: boolean;
  readonly message: string | null;
}

export interface SourceFormValues {
  readonly host: string;
  readonly account: string;
  readonly password: string;
}

/** Reprend une source chargée après le premier rendu sans écraser la saisie en cours. */
export function prefillExistingSource(
  values: SourceFormValues,
  source: { readonly host: string; readonly ibmiUser?: string },
): SourceFormValues {
  return {
    host: values.host || source.host,
    account: values.account || source.ibmiUser || '',
    password: values.password,
  };
}

const VALID = { valid: true, message: null } as const;

// Nom d'hôte ou IPv4 simple — pas de schéma, pas de chemin, pas de port ici
// (les ports avancés sont un champ séparé et replié).
const HOST_PATTERN = /^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$/;
// Profil utilisateur IBM i : jusqu'à 10 caractères, lettre initiale, lettres/chiffres/_/#/$/@.
const ACCOUNT_PATTERN = /^[A-Za-z#$@][A-Za-z0-9#$@_]{0,9}$/;
// Identifiant de compte Snowflake (sans URL) : lettres, chiffres, tirets/underscores, points (org-account).
const SNOWFLAKE_ACCOUNT_PATTERN = /^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$/;

export function validateSourceField(field: SourceFieldName, rawValue: string): FieldValidation {
  const value = rawValue.trim();
  switch (field) {
    case 'host':
      if (!value) return { valid: false, message: 'L’adresse est obligatoire.' };
      if (/^[a-z]+:\/\//i.test(value) || value.includes('/')) {
        return { valid: false, message: 'Saisissez uniquement l’adresse (pas de « http:// » ni de chemin).' };
      }
      if (!HOST_PATTERN.test(value)) {
        return { valid: false, message: 'Adresse invalide : nom d’hôte ou adresse IP attendu.' };
      }
      return VALID;
    case 'account':
      if (!value) return { valid: false, message: 'Le compte est obligatoire.' };
      if (!ACCOUNT_PATTERN.test(value)) {
        return { valid: false, message: 'Compte invalide : 10 caractères maximum, lettres/chiffres, doit commencer par une lettre.' };
      }
      return VALID;
    case 'password':
      if (!value) return { valid: false, message: 'Le mot de passe est obligatoire.' };
      return VALID;
  }
}

export function isSourceFormComplete(values: SourceFormValues): boolean {
  return (['host', 'account', 'password'] as const).every((field) => validateSourceField(field, values[field]).valid);
}

type SavedSourceIdentity = { readonly host: string; readonly account: string };

export function shouldCreateSource(values: SourceFormValues, saved: SavedSourceIdentity | null): boolean {
  return saved === null || saved.host !== values.host || saved.account !== values.account || values.password.length > 0;
}

/** Le secret enregistré suffit pour retester une identité inchangée. */
export function canTestSource(values: SourceFormValues, saved: SavedSourceIdentity | null): boolean {
  if (!validateSourceField('host', values.host).valid || !validateSourceField('account', values.account).valid) return false;
  if (shouldCreateSource(values, saved)) return validateSourceField('password', values.password).valid;
  return values.password.length === 0;
}

export function validateSnowflakeAccount(rawValue: string): FieldValidation {
  const value = rawValue.trim();
  if (!value) return { valid: false, message: 'L’identifiant de compte est obligatoire.' };
  if (/^[a-z]+:\/\//i.test(value) || value.includes('/') || value.includes('snowflakecomputing.com')) {
    return { valid: false, message: 'Saisissez uniquement l’identifiant de compte, pas une URL.' };
  }
  if (!SNOWFLAKE_ACCOUNT_PATTERN.test(value)) {
    return { valid: false, message: 'Identifiant de compte invalide.' };
  }
  return VALID;
}
