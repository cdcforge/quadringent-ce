/**
 * Mode démo de l'assistant v2 — sert des réponses fixes, sans backend, pour
 * le développement local et les tests d'écran. Suit le même principe que le
 * reste du mode démo du cockpit (`#/_fondation`, `soak-2026-08-27.ts`) :
 * aucune valeur mesurée n'est prétendue, tout est explicitement une donnée
 * d'exemple.
 *
 * Les formes de réponse reproduisent exactement le contrat serveur réel
 * (`docs/api-v2.md`, `src/quadringent_control_plane/v2/routes/*.py`) —
 * mêmes noms de champs que Postgres/SQLite renvoient (``schema_name``/
 * ``table_name``, ``key_strategy``/``key_columns``, ``readiness`` déjà
 * classée, ``snowflake_account``/``setup_script``, ``reachable: "unknown"``
 * sans sonde) : le client (`controlPlaneV2Client.ts`) ne doit jamais
 * distinguer démo et réel par sa forme de réponse.
 *
 * `createWizardDemoFetch` renvoie une fonction compatible avec l'option
 * `fetchFn` de `ControlPlaneV2Client` ; à utiliser pour instancier un client
 * de démonstration (voir `createDemoWizardClient` ci-dessous) quand aucun
 * control plane `/v2` n'est joignable.
 */

export interface WizardDemoState {
  readonly sourceId: string;
  readonly destinationId: string;
}

export const DEMO_SOURCE_ID = 'src_demo';
export const DEMO_DESTINATION_ID = 'dst_demo';
export const DEMO_ADMIN_EMAIL = 'admin@example.com';

interface DemoTable {
  id: string;
  schema_name: string;
  table_name: string;
  discovered_row_count: number;
  discovered_size_bytes: number;
  readiness: 'ready' | 'not_journaled' | 'images_incomplete' | 'no_key' | 'journal_mismatch';
  key_strategy: 'primary' | 'unique_index' | 'rrn';
  key_columns: string[];
  cl_fix_commands: Array<{ command: string; reason: string }>;
}

const DEMO_TABLES: readonly DemoTable[] = [
  {
    id: 'tbl_clients', schema_name: 'DEMOLIB', table_name: 'CLIENTS', discovered_row_count: 128430, discovered_size_bytes: 41_800_000,
    readiness: 'ready', key_strategy: 'unique_index', key_columns: ['ID_CLIENT'], cl_fix_commands: [],
  },
  {
    id: 'tbl_commandes', schema_name: 'DEMOLIB', table_name: 'COMMANDES', discovered_row_count: 2_410_905, discovered_size_bytes: 512_000_000,
    readiness: 'images_incomplete', key_strategy: 'unique_index', key_columns: ['ID_COMMANDE'],
    cl_fix_commands: [{ command: 'CHGJRNPF FILE(DEMOLIB/COMMANDES) JRN(DEMOLIB/DEMOJRN) IMAGES(*BOTH)', reason: 'Images avant/après incomplètes.' }],
  },
  {
    id: 'tbl_lignes', schema_name: 'DEMOLIB', table_name: 'LIGNES_CDE', discovered_row_count: 9_812_004, discovered_size_bytes: 1_980_000_000,
    readiness: 'not_journaled', key_strategy: 'rrn', key_columns: [],
    cl_fix_commands: [{ command: 'STRJRNPF FILE(DEMOLIB/LIGNES_CDE) JRN(DEMOLIB/DEMOJRN) IMAGES(*BOTH)', reason: 'Table non journalisée.' }],
  },
  {
    id: 'tbl_archives', schema_name: 'DEMOLIB', table_name: 'ARCHIVES_90J', discovered_row_count: 640_221, discovered_size_bytes: 88_000_000,
    readiness: 'no_key', key_strategy: 'rrn', key_columns: [], cl_fix_commands: [],
  },
] as const as DemoTable[];

/** Fabrique une fonction `fetchFn` de démonstration pour `ControlPlaneV2Client`. */
export function createWizardDemoFetch(): (input: string, init?: RequestInit) => Promise<Response> {
  const tables = DEMO_TABLES.map((table) => ({ ...table, cl_fix_commands: table.cl_fix_commands.map((cmd) => ({ ...cmd })) }));
  let sourceCreated = false;

  return async (input, init) => {
    const method = (init?.method ?? 'GET').toUpperCase();
    const path = input.replace(/^.*\/v2/, '');

    if (method === 'GET' && path === '/sources') {
      return json(200, { items: sourceCreated ? [demoSourceRecord()] : [], next_cursor: null });
    }

    if (method === 'POST' && path === '/sources') {
      sourceCreated = true;
      return json(200, envelope(null, demoSourceRecord(), `/sources/${DEMO_SOURCE_ID}`));
    }

    if (method === 'POST' && path === `/sources/${DEMO_SOURCE_ID}/test`) {
      // Reproduit la forme « sans sonde câblée » (`SourcesService.test`
      // sans `probe`) — la forme par défaut de toute installation qui n'a
      // pas encore branché de sonde IBM i réelle.
      return json(200, envelope(null, {
        source_id: DEMO_SOURCE_ID,
        reachable: 'unknown',
        secret_set: true,
      }, `/sources/${DEMO_SOURCE_ID}`));
    }

    if (method === 'POST' && path === '/destinations') {
      return json(200, envelope(null, {
        id: DEMO_DESTINATION_ID,
        snowflake_account: 'demo-xy12345',
        verification_state: 'declared_not_verified',
        setup_script: DEMO_SQL_SCRIPT,
        private_key_pem: DEMO_PRIVATE_KEY_PEM,
      }, `/destinations/${DEMO_DESTINATION_ID}`));
    }

    if (method === 'GET' && path === `/sources/${DEMO_SOURCE_ID}/tables`) {
      return json(200, { items: tables, next_cursor: null });
    }

    if (method === 'POST' && path === `/sources/${DEMO_SOURCE_ID}/tables/refresh`) {
      return json(200, envelope(null, tables, `/sources/${DEMO_SOURCE_ID}/tables`));
    }

    const keyMatch = /^\/tables\/([^/]+)$/.exec(path);
    if (method === 'PATCH' && keyMatch) {
      const body = init?.body ? JSON.parse(init.body as string) : {};
      const table = tables.find((item) => item.id === keyMatch[1]);
      if (table) {
        table.key_strategy = body.key_strategy;
        table.key_columns = Array.isArray(body.key_columns) ? body.key_columns : [];
        table.readiness = 'ready';
      }
      return json(200, envelope(null, table ?? {}, `/tables/${keyMatch[1]}`));
    }

    const startMatch = /^\/tables\/([^/]+)\/pipeline$/.exec(path);
    if (method === 'POST' && startMatch) {
      return json(200, envelope(null, { id: startMatch[1], declared_state: 'copying' }, `/pipelines/${startMatch[1]}`));
    }

    if (method === 'POST' && path === '/users/activate') {
      const body = init?.body ? JSON.parse(init.body as string) : {};
      if (!body.token) {
        return json(403, { error: { code: 'wrong_confirmation', message: 'Lien d’activation invalide ou expiré.', next_action: 'Demandez un nouveau lien à votre administrateur.', retryable: false } });
      }
      return json(200, envelope(null, { id: 'usr_demo_admin', email: DEMO_ADMIN_EMAIL, role: 'admin', activated: true }, '/users/usr_demo_admin'));
    }

    // Tâche « auth-login » : connexion automatique après activation
    // (WizardActivate.tsx) — la démo accepte tout mot de passe pour rester
    // utilisable hors backend réel.
    if (method === 'POST' && path === '/auth/login') {
      const body = init?.body ? JSON.parse(init.body as string) : {};
      return json(200, { user: { id: 'usr_demo_admin', email: typeof body.email === 'string' ? body.email : DEMO_ADMIN_EMAIL, role: 'admin' } });
    }

    return json(404, { error: { code: 'not_found', message: `Route de démonstration inconnue : ${method} ${path}`, next_action: 'Vérifiez l’appel du client.', retryable: false } });
  };
}

function demoSourceRecord() {
  return { id: DEMO_SOURCE_ID, display_name: null, ibmi_host: 'as400.demo.local', ibmi_user: 'QDTDEMO' };
}

const DEMO_SQL_SCRIPT = `-- Script de démonstration — non destiné à être exécuté tel quel.
CREATE ROLE IF NOT EXISTS QUADRINGENT_LOADER;
CREATE USER IF NOT EXISTS QUADRINGENT_SVC
  RSA_PUBLIC_KEY = '<clé publique générée>'
  DEFAULT_ROLE = QUADRINGENT_LOADER;
GRANT ROLE QUADRINGENT_LOADER TO USER QUADRINGENT_SVC;
CREATE WAREHOUSE IF NOT EXISTS QUADRINGENT_WH WAREHOUSE_SIZE = 'XSMALL';
CREATE DATABASE IF NOT EXISTS QUADRINGENT_DB;
GRANT USAGE ON WAREHOUSE QUADRINGENT_WH TO ROLE QUADRINGENT_LOADER;
GRANT ALL ON DATABASE QUADRINGENT_DB TO ROLE QUADRINGENT_LOADER;
`;

const DEMO_PRIVATE_KEY_PEM = `-----BEGIN PRIVATE KEY-----
DEMOKEYDONOTUSE==
-----END PRIVATE KEY-----
`;

function envelope(before: unknown, after: unknown, path: string) {
  return { before, after, verify: { method: 'GET', path: `/v2${path}` }, dry_run: null };
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}
