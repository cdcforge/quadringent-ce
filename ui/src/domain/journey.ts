/**
 * Parcours de création d'une liaison.
 *
 * Il est écrit du point de vue de la personne qui l'exécute, pas du modèle
 * qu'il alimente. Chaque étape dit ce qu'elle demande et pourquoi, et ne
 * demande que ce qu'un exploitant peut fournir sans ouvrir de documentation.
 *
 * Deux honnêtetés structurent ce module :
 *
 * - Le service valide une configuration, il ne compose pas avec l'AS400. Le
 *   parcours ne promet donc jamais « connexion testée » : il dit « informations
 *   vérifiées », et annonce que la connexion réelle sera éprouvée à la mise en
 *   service. Promettre un test qui n'a pas lieu serait la pire des fausses
 *   données — celle qu'on ne découvre qu'en production.
 * - Le mot de passe n'entre jamais ici. Le parcours demande où il est déposé,
 *   jamais sa valeur.
 */

/** Les cinq moments du parcours, dans l'ordre où on les traverse. */
export const journeySteps = ['preparer', 'source', 'donnees', 'destination', 'creer'] as const;

export type JourneyStep = (typeof journeySteps)[number];

/**
 * Étape du contrat serveur à laquelle une étape du parcours est soumise.
 *
 * Le service valide de façon cumulative : demander la validation de
 * `destination` vaut validation de tout ce qui précède. Le parcours affiché et
 * le contrat validé restent ainsi distincts — on peut réécrire l'un sans
 * toucher l'autre.
 */
const SERVER_STEP: Readonly<Record<JourneyStep, string | null>> = {
  preparer: null,
  source: 'permissions',
  // Le service valide la sélection de tables en même temps que le suivi des
  // modifications, avant la destination : demander « où » avant « quoi »
  // faisait remonter, sur l'écran Snowflake, un reproche sur les tables que
  // l'opérateur n'avait pas encore eu l'occasion de choisir.
  donnees: 'journal',
  destination: 'destination',
  creer: 'verdict',
};

export function serverStepFor(step: JourneyStep): string | null {
  return SERVER_STEP[step];
}

export interface JourneyCopy {
  /** Ce qu'on fait, à l'infinitif de l'action attendue. */
  readonly title: string;
  /** Pourquoi on le fait — une phrase, jamais deux. */
  readonly why: string;
  /** Le libellé du bouton qui avance. */
  readonly next: string;
}

export const JOURNEY_COPY: Readonly<Record<JourneyStep, JourneyCopy>> = {
  preparer: {
    title: 'Avant de commencer',
    why: 'Trois informations suffisent. Rassemblez-les, la suite prend deux minutes.',
    next: 'Commencer',
  },
  source: {
    title: 'Votre AS400',
    why: 'Quadringent a besoin de savoir quelle machine lire, et avec quel compte.',
    next: 'Continuer',
  },
  destination: {
    title: 'Où déposer les données',
    why: 'Vos tables seront recréées dans Snowflake, à l’endroit que vous indiquez ici.',
    next: 'Continuer',
  },
  donnees: {
    title: 'Quelles données copier',
    why: 'Choisissez les tables à répliquer. Vous pourrez en ajouter plus tard.',
    next: 'Continuer',
  },
  creer: {
    title: 'Vérifiez et créez',
    why: 'Rien n’est écrit tant que vous n’avez pas validé cette page.',
    next: 'Créer la liaison',
  },
};

/** Ce que l'opérateur doit avoir sous la main avant de commencer. */
export interface Prerequisite {
  readonly label: string;
  readonly detail: string;
}

export const PREREQUISITES: readonly Prerequisite[] = [
  {
    label: 'L’adresse de votre AS400',
    detail: 'Une adresse IP ou un nom de machine, fourni par votre équipe système.',
  },
  {
    label: 'Un compte de lecture',
    detail: 'Un compte qui a le droit de lire les tables à copier. Un compte dédié est conseillé.',
  },
  {
    label: 'Le mot de passe déjà déposé',
    detail:
      'Quadringent ne demande jamais de mot de passe. Votre équipe l’a déposé dans un coffre ; il vous faut seulement son nom.',
  },
];

export function nextStep(step: JourneyStep): JourneyStep | null {
  const index = journeySteps.indexOf(step);
  return index >= 0 && index < journeySteps.length - 1 ? journeySteps[index + 1]! : null;
}

export function previousStep(step: JourneyStep): JourneyStep | null {
  const index = journeySteps.indexOf(step);
  return index > 0 ? journeySteps[index - 1]! : null;
}

/** Position dans le parcours, pour l'indicateur d'avancement. */
export interface JourneyMark {
  readonly step: JourneyStep;
  readonly label: string;
  readonly state: 'done' | 'current' | 'todo';
}

const MARK_LABEL: Readonly<Record<JourneyStep, string>> = {
  preparer: 'Préparation',
  source: 'AS400',
  donnees: 'Données',
  destination: 'Snowflake',
  creer: 'Création',
};

export function journeyMarks(current: JourneyStep): readonly JourneyMark[] {
  const currentIndex = journeySteps.indexOf(current);
  return journeySteps.map((step, index) => ({
    step,
    label: MARK_LABEL[step],
    state: index < currentIndex ? 'done' : index === currentIndex ? 'current' : 'todo',
  }));
}

/* ------------------------------------------------------------------ */
/* Ce que le parcours répond après une validation                      */
/* ------------------------------------------------------------------ */

export interface JourneyCheck {
  /** Vrai quand rien ne s'oppose au passage à l'étape suivante. */
  readonly passed: boolean;
  /** Ce qui manque ou bloque, en langage courant. Vide si tout va bien. */
  readonly problems: readonly string[];
  /** Ce qui reste à éprouver plus tard — dit sans le maquiller en garantie. */
  readonly pending: readonly string[];
}

/**
 * Traduit le verdict du service en réponse lisible.
 *
 * Le service distingue erreurs, blocages et points non prouvés. Les deux
 * premiers empêchent d'avancer et se disent ensemble : pour l'opérateur, un
 * champ manquant et une règle refusée sont le même problème — quelque chose à
 * corriger ici. Le troisième ne bloque pas mais ne doit pas être tu.
 */
export function checkFrom(verdict: {
  readonly errors: readonly string[];
  readonly blocked: readonly string[];
  readonly unproven: readonly string[];
}): JourneyCheck {
  const problems = [...verdict.errors, ...verdict.blocked].map(problemCopy);
  return {
    passed: problems.length === 0,
    problems,
    pending: pendingCopy(verdict.unproven),
  };
}

/* ------------------------------------------------------------------ */
/* Traduction des verdicts du service                                  */
/* ------------------------------------------------------------------ */

/**
 * Ce que le service refuse, dit à la personne qui peut le corriger.
 *
 * Les messages du service nomment son propre modèle — « hôte IBM i »,
 * « référence du secret Kubernetes ». Ils désignent pourtant des champs que
 * l'écran vient d'afficher sous d'autres noms : laisser passer le message
 * brut oblige l'opérateur à deviner quel champ reprendre.
 *
 * Un message inconnu n'est jamais avalé : il est affiché tel quel. Taire un
 * refus parce qu'on ne sait pas le traduire serait pire que le traduire mal.
 */
const PROBLEM_COPY: Readonly<Record<string, string>> = {
  "L'hôte IBM i est obligatoire":
    'Indiquez l’adresse de votre AS400.',
  "L'utilisateur IBM i est obligatoire":
    'Indiquez le compte de lecture.',
  'La référence du secret Kubernetes est obligatoire':
    'Indiquez où le mot de passe est déposé : le nom du dépôt et celui de l’entrée.',
  'TLS est obligatoire ; le plaintext IBM i est interdit':
    'La connexion à votre AS400 doit être chiffrée. Elle ne peut pas être créée autrement.',
  'Le fichier CA TLS IBM i est obligatoire':
    'Le certificat qui authentifie votre AS400 n’est pas en place. À voir avec votre exploitation.',
  'Le journal IBM i est obligatoire':
    'Le suivi des modifications n’est pas activé sur cette machine. À voir avec votre équipe système.',
  'Sélectionnez les tables à copier':
    'Choisissez au moins une table à copier.',
  'La sélection contient une table en double':
    'Une table est sélectionnée deux fois.',
  'Sélection de tables illisible':
    'La sélection de tables n’a pas pu être lue. Reprenez le choix des données.',
  'Au moins une table déclarée est hors du périmètre préparé':
    'Une des tables choisies n’est pas disponible sur cette machine.',
  'Destination interdite par la politique du site':
    'Cette destination Snowflake n’est pas autorisée. À voir avec votre exploitation.',
  'Un secret ne doit jamais être envoyé au control plane':
    'Un mot de passe a été saisi dans le formulaire. Quadringent n’en accepte jamais : indiquez seulement où il est déposé.',
};

/**
 * Refus dont le texte porte des valeurs — un compte de tables, un nom de
 * schéma. Une table de correspondance exacte ne peut pas les couvrir : ils
 * passeraient tels quels, avec leur vocabulaire d'origine.
 */
const PROBLEM_PATTERNS: readonly (readonly [RegExp, (match: RegExpMatchArray) => string])[] = [
  [
    /^Destination Snowflake non provisionnée pour (\d+) des (\d+) tables sélectionnées$/,
    (match) =>
      `${match[1]} des ${match[2]} tables choisies n’ont pas encore d’emplacement prêt dans Snowflake. À faire préparer par votre exploitation.`,
  ],
  [
    /^Cette zone de dépôt n'est pas provisionnée en /,
    () => 'L’emplacement de destination n’est pas encore préparé pour cet environnement. À voir avec votre exploitation.',
  ],
  [
    /^La destination Snowflake doit rester (.+)$/,
    (match) => `Cette liaison doit écrire dans ${match[1]}. Corrigez la base et le schéma.`,
  ],
  [
    /^Le journal de ce site est (.+)$/,
    (match) => `Les données de cette machine sont lues depuis ${match[1]}.`,
  ],
];

export function problemCopy(message: string): string {
  const exact = PROBLEM_COPY[message];
  if (exact !== undefined) return exact;
  for (const [pattern, render] of PROBLEM_PATTERNS) {
    const match = message.match(pattern);
    if (match) return render(match);
  }
  return message;
}

/**
 * Ce que le service n'a pas vérifié, dit sans le maquiller en garantie.
 *
 * Le parcours valide une configuration ; il ne compose pas avec l'AS400 et
 * n'écrit pas dans Snowflake. Ces réserves ne bloquent pas — elles disent à
 * quel moment la vérité sera faite.
 */
const PENDING_COPY: Readonly<Record<string, string>> = {
  'Connectivité IBM i non observée':
    'La connexion à votre AS400 sera éprouvée à la mise en service.',
  'Destination Snowflake non vérifiée par ce formulaire':
    'L’accès à Snowflake sera éprouvé à la mise en service.',
  'Journalisation et droits IBM i non vérifiés':
    'Les droits de lecture seront éprouvés à la mise en service.',
  'Exécution et garde-fous du pilote non vérifiés par ce formulaire':
    'Le bon déroulement de la première copie sera constaté à la mise en service.',
  'Preuves serveur et lancement contrôlé non raccordés à ce formulaire':
    'La mise en service est faite par votre exploitation, pas par ce formulaire.',
};

/** Réserves à valeur variable, même traitement que les refus. */
const PENDING_PATTERNS: readonly (readonly [RegExp, (match: RegExpMatchArray) => string])[] = [
  [
    /^(\d+) table\(s\) sans clé métier/,
    (match) =>
      `${match[1]} des tables choisies n’ont pas d’identifiant propre : une réorganisation de fichier sur l’AS400 obligerait à les recopier entièrement.`,
  ],
];

/** Les réserves, dédoublonnées : plusieurs libellés disent la même attente. */
export function pendingCopy(messages: readonly string[]): readonly string[] {
  const seen = new Set<string>();
  for (const message of messages) {
    const exact = PENDING_COPY[message];
    if (exact !== undefined) {
      seen.add(exact);
      continue;
    }
    for (const [pattern, render] of PENDING_PATTERNS) {
      const match = message.match(pattern);
      if (match) {
        seen.add(render(match));
        break;
      }
    }
  }
  return [...seen];
}
