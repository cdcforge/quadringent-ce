import type { DiscoveredTable, KeyStrategy, TableReadiness } from '../domain/wizardTables.ts';

/**
 * Client typé pour `/v2` (docs/plans/2026-09-23-control-plane-v2-contract.md
 * §2). Conventions suivies :
 *  - toute écriture (POST/PATCH/DELETE) porte un `Idempotency-Key` généré
 *    (une valeur différente par appel — rejouer une même intention est la
 *    responsabilité de l'appelant, pas de ce client) ;
 *  - une erreur revient sous l'enveloppe `{error:{code,message,next_action,
 *    retryable}}` (§2, catalogue en §2.6) — jamais un `Error` générique.
 */

export type CheckState = 'ok' | 'attention' | 'failed' | 'unknown';

export interface CheckResult {
  readonly state: CheckState;
  readonly message: string;
}

/**
 * Résultat de ``POST /v2/sources/{id}/test`` — le serveur répond sous deux
 * formes possibles (``docs/api-v2.md`` « Identité »/``services/sources.py::
 * SourcesService.test``) selon qu'une sonde IBM i réelle est câblée
 * (``request.app.state.source_probe``, chantier en parallèle) :
 *  - ``kind: 'unavailable'`` — aucune sonde câblée sur cette installation :
 *    ``{"source_id", "reachable": "unknown", "secret_set": true}``. Seul le
 *    déchiffrement du secret a été vérifié ; aucun résultat réseau n'existe,
 *    jamais présenté comme si tout allait bien.
 *  - ``kind: 'probed'`` — une sonde répond : ``{"source_id", "reachable":
 *    <bool>, "network": {"ok","detail"}, "tls": {"ok","detail","fingerprint",
 *    "trust","certificate_pem"}, "authentication": {"ok","detail"},
 *    "ibmi_version", "qtimzon", "detected_time_zone", "timezone_ambiguous"}``
 *    (``v2/services/source_probe.py::SourceProbeResult.to_dict``).
 *
 * ``tls.trust`` (chaîne de confiance TLS, chantier 2026-09-24) :
 *  - ``"system"`` — autorité publique reconnue par le magasin système/JVM,
 *    aucune action requise ;
 *  - ``"pinned"`` — empreinte épinglée confirmée par cette sonde ;
 *  - ``"unknown"`` — poignée de main réussie mais autorité non reconnue :
 *    mesure seule (jamais utilisée pour authentifier), ``tlsFingerprint``/
 *    ``tlsCertificatePem`` portent de quoi épingler explicitement (voir
 *    ``WizardSource.tsx``) ;
 *  - ``null`` — jamais mesurée (réseau/TLS injoignable).
 */
export type SourceTestResult =
  | { readonly kind: 'unavailable'; readonly secretSet: boolean }
  | {
      readonly kind: 'probed';
      readonly reachable: boolean;
      readonly network: CheckResult;
      readonly tls: CheckResult;
      readonly tlsFingerprint: string | null;
      readonly tlsTrust: 'system' | 'pinned' | 'unknown' | null;
      readonly tlsCertificatePem: string | null;
      readonly authentication: CheckResult;
      readonly ibmiVersion: string | null;
      readonly detectedTimeZone: string | null;
      readonly timezoneAmbiguous: boolean;
    };

export interface CreateSourceInput {
  /** Obligatoire côté serveur (`validation.validated_display_name`) — jamais `null`. */
  readonly displayName: string;
  readonly host: string;
  readonly account: string;
  readonly password: string;
}

export interface SourceRecord {
  readonly id: string;
  readonly displayName: string | null;
  readonly host: string;
  /** ``ibmi_user`` — absent des fixtures historiques ; optionnel pour rester
   *  compatible avec les deux formes (`ui/src/domain/cockpit.test.ts`). */
  readonly ibmiUser?: string;
}

/** État persisté par le service après création ou vérification native. */
export type DestinationVerificationState = 'declared_not_verified' | 'verified' | 'failed';

export interface DestinationRecord {
  readonly id: string;
  readonly accountIdentifier: string;
  readonly destinationDatabase: string | null;
  readonly destinationSchema: string | null;
  readonly verificationState: DestinationVerificationState;
  readonly sqlScript: string;
  /** Clé privée PEM — présente uniquement dans la réponse de création
   *  (``POST /v2/destinations``), jamais relisible ensuite. `null` sur
   *  toute autre lecture (``GET``). */
  readonly privateKeyPem: string | null;
}

export interface CreateDestinationInput {
  readonly accountIdentifier: string;
  readonly destinationDatabase?: string;
  readonly destinationSchema?: string | null;
}

export interface DestinationAccessVerification {
  readonly destination: DestinationRecord;
  readonly verified: boolean;
  readonly detail: string | null;
}

export interface StartTablePipelineOptions {
  readonly dryRun?: boolean;
}

export interface PatchTableKeyInput {
  readonly keyStrategy: Exclude<KeyStrategy, 'primary'>;
  /** Requis pour ``unique_index`` ; ignoré (envoyé vide) pour ``rrn``. */
  readonly keyColumns?: readonly string[];
  /** Requis (``true``) pour ``key_strategy: 'rrn'`` — voir ``services/tables.py::choose_key``. */
  readonly acknowledgeRrn?: boolean;
}

export interface AdminUser {
  readonly id: string;
  readonly email: string;
  readonly role: string;
  readonly activated: boolean;
}

/** Identité courante (``POST /v2/auth/login``, ``GET /v2/auth/me``) — tâche « auth-login ». */
export interface AuthUser {
  readonly email: string;
  readonly role: string;
}

/* ------------------------------------------------------------------------
 * Cockpit v2 — pipelines, actions à tous les niveaux, métriques, journaux,
 * coûts, confirmations, audit, flux d'événements (docs/plans/
 * 2026-09-23-control-plane-v2-contract.md §2.4/§2.5, docs/api-v2.md).
 *
 * Statut des routes serveur au moment de l'écriture (voir docs/api-v2.md,
 * section « Endpoints implémentés ») :
 *  - EXISTE : GET /v2/pipelines/{id}, POST /v2/pipelines/{id}/actions/
 *    {pause,resume,remove,restart_initial_copy,replay}, GET/POST
 *    /v2/confirmations, GET /v2/audit, GET /v2/events (SSE).
 *  - CONTRAT SEUL (pas encore servi, code contre le contrat — chantier
 *    parallèle « control-plane-v2 fondation » confirmé hors périmètre) :
 *    GET /v2/pipelines (liste), GET /v2/pipelines/{id}/metrics,
 *    GET /v2/pipelines/{id}/logs, GET /v2/costs,
 *    POST /v2/sources/{id}/actions/{pause,resume},
 *    POST /v2/destinations/{id}/actions/{pause,resume},
 *    POST /v2/actions/{pause_all,resume_all}.
 *    Ces appels échoueront (404/501) tant que le serveur ne les sert pas ;
 *    l'écran doit traiter l'échec comme « action indisponible », jamais
 *    inventer un effet.
 * ------------------------------------------------------------------------ */

export type PipelineDeclaredState = 'not_started' | 'copying' | 'live' | 'paused' | 'attention' | 'stopped';

export interface PipelineRecordV2 {
  readonly id: string;
  readonly declaredState: PipelineDeclaredState;
}

/** Figures observées d'un pipeline (`GET /v2/pipelines`, contrat §2.4,
 *  `PipelineListRecord.to_dict` côté service) — distinctes de
 *  `declared_state` (base transactionnelle) : peuvent être absentes
 *  (`null` + raison dans `absentReasons`) sans jamais invalider l'état
 *  déclaré. `GET /v2/pipelines/{id}` (singulier) ne les porte pas — voir
 *  `PipelineRecordV2` ci-dessus, utilisé par `getPipeline`/les actions. */
export interface PipelineObservationV2 {
  readonly lagSeconds: number | null;
  readonly throughputRowsPerSecond: number | null;
  readonly rowsSource: number | null;
  readonly rowsDestination: number | null;
  readonly lastArrivalAt: string | null;
  readonly absentReasons: Readonly<Record<string, string>>;
}

export interface PipelineListRecordV2 extends PipelineRecordV2 {
  readonly tableId: string;
  readonly sourceId: string;
  readonly destinationId: string;
  readonly observation: PipelineObservationV2;
}

export type PipelineActionId = 'pause' | 'resume' | 'remove' | 'restart_initial_copy' | 'replay';
export type LevelActionId = 'pause' | 'resume';

export interface ActionEnvelope<T> {
  readonly before: T | null;
  readonly after: T | null;
  readonly verify: { readonly method: string; readonly path: string } | null;
  readonly dryRun: unknown | null;
}

export interface RunPipelineActionOptions {
  readonly dryRun?: boolean;
  readonly confirmationToken?: string;
  readonly fromSequence?: number;
  readonly toSequence?: number;
}

/** Extrait l'id de confirmation du message `pending_confirmation_required`
 *  (le service ne le porte pas encore comme champ structuré — voir
 *  `v2/routes/pipelines.py::run_pipeline_action`). Retourne `null` si le
 *  message ne contient pas la forme attendue : ne jamais deviner. */
export function pendingConfirmationId(error: ControlPlaneV2Error): string | null {
  if (error.code !== 'pending_confirmation_required') return null;
  const match = /id=([A-Za-z0-9_-]+)/.exec(error.message);
  return match ? match[1] : null;
}

export type ConfirmationState = 'pending' | 'approved' | 'rejected' | 'used' | 'expired';

export interface ConfirmationRecord {
  readonly id: string;
  readonly actionRef: string;
  readonly resourceType: string;
  readonly resourceId: string;
  readonly reason: string;
  readonly riskEstimate: string | null;
  readonly requestedByKind: string | null;
  readonly requestedById: string | null;
  readonly expiresAt: string | null;
  readonly state: ConfirmationState;
  readonly approvedByKind: string | null;
  readonly approvedById: string | null;
  readonly approvedAt: string | null;
  readonly createdAt: string | null;
  readonly available: { readonly approve: boolean; readonly reject: boolean; readonly execute: boolean };
}

export interface AuditRecord {
  readonly id: string;
  readonly at: string;
  readonly actorKind: string;
  readonly actorId: string | null;
  readonly actorDisplay: string | null;
  readonly mcpClient: string | null;
  readonly action: string;
  readonly resourceType: string;
  readonly resourceId: string | null;
  readonly requestId: string | null;
  readonly idempotencyKey: string | null;
  readonly dryRun: boolean;
  readonly confirmationId: string | null;
  readonly status: string;
  readonly before: unknown;
  readonly after: unknown;
}

export type CostStatus = 'measured' | 'estimated' | 'absent';

export interface CostSnapshot {
  readonly scope: 'connection' | 'table';
  readonly id: string;
  readonly window: string | null;
  readonly status: CostStatus;
  readonly amount: number | null;
  readonly currency: string | null;
  readonly basis: string | null;
  readonly collectedAt: string | null;
}

export type LogLevel = 'info' | 'warning' | 'error';

export interface LogEntry {
  readonly at: string;
  readonly level: LogLevel;
  readonly message: string;
  readonly incidentId: string | null;
}

export interface MetricPoint {
  readonly at: string;
  readonly lagSeconds: number | null;
  readonly throughputRowsPerSecond: number | null;
}

export interface MetricsSeries {
  readonly window: '1h' | '24h';
  readonly points: readonly MetricPoint[];
  readonly provenance?: string | null;
  readonly freshness?: string | null;
  readonly collectedAt?: string | null;
  readonly reason?: string | null;
}

export type CockpitEventType =
  | 'pipeline.state_changed'
  | 'action.pending_confirmation'
  | 'action.completed'
  | 'alert.fired'
  | 'alert.resolved';

export interface CockpitEvent {
  readonly id: number;
  readonly type: CockpitEventType | string;
  readonly payload: unknown;
}

export interface CockpitEventSourceLike {
  addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void;
  close(): void;
  onerror: ((event: Event) => void) | null;
  onopen?: ((event: Event) => void) | null;
}

const ERROR_CATALOGUE = new Set([
  'invalid_request',
  'idempotency_key_conflict',
  'wrong_confirmation',
  'pending_confirmation_required',
  'capability_unavailable',
  'action_in_progress',
  'insufficient_role',
  'wrong_environment',
  'not_found',
  'store_unavailable',
  'executor_unavailable',
  'internal_error',
]);

export class ControlPlaneV2Error extends Error {
  readonly status: number;
  readonly code: string;
  readonly nextAction: string;
  readonly retryable: boolean;

  constructor(status: number, code: string, message: string, nextAction: string, retryable: boolean) {
    super(message);
    this.name = 'ControlPlaneV2Error';
    this.status = status;
    this.code = code;
    this.nextAction = nextAction;
    this.retryable = retryable;
  }
}

export interface ControlPlaneV2ClientOptions {
  readonly basePath?: string;
  readonly fetchFn?: (input: string, init?: RequestInit) => Promise<Response>;
  readonly idempotencyKeyFactory?: () => string;
  readonly eventSourceFactory?: (url: string) => CockpitEventSourceLike;
  readonly reconnectDelayMs?: number;
  readonly maxReconnectDelayMs?: number;
}

function defaultIdempotencyKey(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID();
  return `idem_${Date.now()}_${Math.random().toString(36).slice(2)}`;
}

export class ControlPlaneV2Client {
  private readonly basePath: string;
  private readonly fetchFn: (input: string, init?: RequestInit) => Promise<Response>;
  private readonly idempotencyKeyFactory: () => string;
  private readonly eventSourceFactory: (url: string) => CockpitEventSourceLike;
  private readonly reconnectDelayMs: number;
  private readonly maxReconnectDelayMs: number;

  constructor(options: ControlPlaneV2ClientOptions = {}) {
    this.basePath = (options.basePath ?? '/v2').replace(/\/$/, '');
    this.fetchFn = options.fetchFn ?? ((input, init) => fetch(input, init));
    this.idempotencyKeyFactory = options.idempotencyKeyFactory ?? defaultIdempotencyKey;
    this.eventSourceFactory = options.eventSourceFactory ?? ((url) => new EventSource(url));
    this.reconnectDelayMs = Math.max(0, options.reconnectDelayMs ?? 1_000);
    this.maxReconnectDelayMs = Math.max(this.reconnectDelayMs, options.maxReconnectDelayMs ?? 15_000);
  }

  async testSource(sourceId: string, signal?: AbortSignal): Promise<SourceTestResult> {
    const envelope = await this.write(`/sources/${encodeURIComponent(sourceId)}/test`, {}, signal);
    return parseSourceTestResult(envelope.after);
  }

  /** POST /v2/sources — ``secret.value`` (``routes/sources.py::create_source``
   *  ne lit que cette clé ; ``value_or_ref`` n'est jamais lu côté serveur). */
  async createSource(input: CreateSourceInput, signal?: AbortSignal): Promise<SourceRecord> {
    const body: Record<string, unknown> = {
      display_name: input.displayName,
      ibmi_host: input.host,
      ibmi_user: input.account,
      secret: { kind: 'inline', value: input.password },
    };
    const envelope = await this.write('/sources', body, signal);
    return parseSourceRecord(envelope.after);
  }

  /** GET /v2/sources — existe côté serveur (pagination non implémentée, `next_cursor` toujours `null`). */
  async listSources(signal?: AbortSignal): Promise<readonly SourceRecord[]> {
    const payload = await this.getJson('/sources', signal);
    const items = Array.isArray((payload as Record<string, unknown>).items) ? (payload as Record<string, unknown>).items as unknown[] : [];
    return items.map(parseSourceRecord);
  }

  /** GET /v2/destinations — existe côté serveur. */
  async listDestinations(signal?: AbortSignal): Promise<readonly DestinationRecord[]> {
    const payload = await this.getJson('/destinations', signal);
    const items = Array.isArray((payload as Record<string, unknown>).items) ? (payload as Record<string, unknown>).items as unknown[] : [];
    return items.map(parseDestinationRecord);
  }

  /** POST /v2/destinations : création déclarative, sans supposer l'accès vérifié. */
  async createDestination(input: CreateDestinationInput, signal?: AbortSignal): Promise<DestinationRecord> {
    const envelope = await this.write('/destinations', {
      snowflake_account: input.accountIdentifier,
      ...(input.destinationDatabase !== undefined ? { destination_database: input.destinationDatabase } : {}),
      ...(input.destinationSchema !== undefined ? { destination_schema: input.destinationSchema } : {}),
    }, signal);
    return parseDestinationRecord(envelope.after);
  }

  async getDestination(id: string, signal?: AbortSignal): Promise<DestinationRecord> {
    return parseDestinationRecord(await this.getJson(`/destinations/${encodeURIComponent(id)}`, signal));
  }

  /** La sonde et sa relecture doivent toutes deux confirmer l'accès. */
  async verifyDestination(id: string, signal?: AbortSignal): Promise<DestinationAccessVerification> {
    const envelope = await this.write(`/destinations/${encodeURIComponent(id)}/verify`, {}, signal);
    const probe = typeof envelope.after === 'object' && envelope.after !== null
      ? envelope.after as Record<string, unknown> : null;
    const destination = await this.getDestination(id, signal);
    if (destination.id !== id) {
      throw new ControlPlaneV2Error(502, 'internal_error', 'La relecture a renvoyé une autre destination.', 'Réessayez la vérification.', true);
    }
    const verified = probe?.destination_id === id && probe.verified === true
      && destination.verificationState === 'verified';
    let detail: string | null = null;
    if (!verified && probe) {
      for (const name of ['connection', 'role', 'warehouse', 'database', 'schema', 'load_privileges']) {
        const check = probe[name];
        if (typeof check === 'object' && check !== null) {
          const record = check as Record<string, unknown>;
          if (record.ok !== true && typeof record.detail === 'string' && record.detail.trim()) {
            detail = record.detail;
            break;
          }
        }
      }
      if (!detail && typeof probe.detail === 'string' && probe.detail.trim()) detail = probe.detail;
    }
    return { destination, verified, detail };
  }

  async listTables(sourceId: string, signal?: AbortSignal): Promise<readonly DiscoveredTable[]> {
    const path = `/sources/${encodeURIComponent(sourceId)}/tables`;
    const response = await this.fetchFn(`${this.basePath}${path}`, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      signal,
    });
    const payload = await this.readJsonOrThrow(response, path);
    const items = Array.isArray((payload as Record<string, unknown>).items) ? (payload as Record<string, unknown>).items as unknown[] : [];
    return items.map(parseDiscoveredTable);
  }

  async refreshTables(sourceId: string, signal?: AbortSignal): Promise<readonly DiscoveredTable[]> {
    const envelope = await this.write(`/sources/${encodeURIComponent(sourceId)}/tables/refresh`, {}, signal);
    const items = Array.isArray(envelope.after) ? envelope.after as unknown[] : [];
    return items.map(parseDiscoveredTable);
  }

  /** PATCH /v2/tables/{id} — ``key_strategy``/``key_columns``/``acknowledge_rrn``
   *  (``routes/tables.py::patch_table`` ; ``key_status`` n'existe pas côté
   *  serveur). ``key_columns`` est ignoré (liste vide) pour ``rrn``. */
  async patchTableKey(tableId: string, input: PatchTableKeyInput, signal?: AbortSignal): Promise<DiscoveredTable> {
    const body = {
      key_strategy: input.keyStrategy,
      key_columns: input.keyStrategy === 'unique_index' ? [...(input.keyColumns ?? [])] : null,
      acknowledge_rrn: input.acknowledgeRrn ?? false,
    };
    const envelope = await this.write(`/tables/${encodeURIComponent(tableId)}`, body, signal, 'PATCH');
    return parseDiscoveredTable(envelope.after);
  }

  async startTablePipeline(tableId: string, options: StartTablePipelineOptions = {}, signal?: AbortSignal): Promise<void> {
    await this.write(`/tables/${encodeURIComponent(tableId)}/pipeline`, { dry_run: options.dryRun ?? false }, signal);
  }

  /** Échange le jeton d'un lien d'activation à usage unique contre un compte admin activé. */
  async activateAdmin(token: string, password: string, signal?: AbortSignal): Promise<AdminUser> {
    const envelope = await this.write('/users/activate', { token, password }, signal);
    return parseAdminUser(envelope.after);
  }

  /** POST /v2/auth/login — pose le cookie de session ``HttpOnly`` (transmis
   *  automatiquement par le navigateur, même origine que ``basePath``).
   *  Erreur (``401 invalid_request``) sur identifiants incorrects. */
  async login(email: string, password: string, signal?: AbortSignal): Promise<AuthUser> {
    const response = await this.fetchFn(`${this.basePath}/auth/login`, {
      method: 'POST',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
      signal,
    });
    const payload = (await this.readJsonOrThrow(response, '/auth/login')) as Record<string, unknown>;
    return parseAuthUser(payload.user);
  }

  /** POST /v2/auth/logout — efface le cookie de session ; jamais d'échec côté appelant. */
  async logout(signal?: AbortSignal): Promise<void> {
    await this.fetchFn(`${this.basePath}/auth/logout`, { method: 'POST', signal });
  }

  /** GET /v2/auth/me — identité courante, ou ``null`` sans session (401),
   *  jamais une exception pour ce cas précis (c'est l'appel qui sert à
   *  savoir si l'UI est connectée). */
  async me(signal?: AbortSignal): Promise<AuthUser | null> {
    const response = await this.fetchFn(`${this.basePath}/auth/me`, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      signal,
    });
    if (response.status === 401) return null;
    const payload = await this.readJsonOrThrow(response, '/auth/me');
    return parseAuthUser(payload);
  }

  /** GET /v2/pipelines/{id} — existe côté serveur. */
  async getPipeline(pipelineId: string, signal?: AbortSignal): Promise<PipelineRecordV2> {
    return parsePipelineRecord(await this.getJson(`/pipelines/${encodeURIComponent(pipelineId)}`, signal));
  }

  /** GET /v2/pipelines — existe côté serveur (`routes/pipelines.py::list_pipelines`) ;
   *  chaque ligne porte l'état déclaré et les figures observées (retard, débit,
   *  lignes source/destination, dernière arrivée), voir `PipelineListRecordV2`. */
  async listPipelines(signal?: AbortSignal): Promise<readonly PipelineListRecordV2[]> {
    const payload = await this.getJson('/pipelines', signal);
    const items = Array.isArray((payload as Record<string, unknown>).items) ? (payload as Record<string, unknown>).items as unknown[] : [];
    return items.map(parsePipelineListRecord);
  }

  /** POST /v2/pipelines/{id}/actions/{pause,resume,remove,restart_initial_copy,replay}
   *  — existe côté serveur. `remove`/`restart_initial_copy`/`replay` sont
   *  des actions sensibles : sans `confirmationToken` valide, le serveur
   *  répond `409 pending_confirmation_required` (voir `pendingConfirmationId`). */
  async runPipelineAction(
    pipelineId: string,
    action: PipelineActionId,
    options: RunPipelineActionOptions = {},
    signal?: AbortSignal,
  ): Promise<ActionEnvelope<PipelineRecordV2>> {
    const query = action === 'replay' ? buildReplayQuery(options) : '';
    const body: Record<string, unknown> = { dry_run: options.dryRun ?? false };
    if (options.confirmationToken) body.confirmation_token = options.confirmationToken;
    const envelope = await this.writeFull(`/pipelines/${encodeURIComponent(pipelineId)}/actions/${action}${query}`, body, signal);
    return {
      before: envelope.before ? parsePipelineRecord(envelope.before) : null,
      after: envelope.after ? parsePipelineRecord(envelope.after) : null,
      verify: envelope.verify,
      dryRun: envelope.dryRun,
    };
  }

  /** POST /v2/sources/{id}/actions/{pause,resume} — CONTRAT SEUL (toutes les
   *  tables d'une source). Confirmé absent du serveur par le chantier
   *  « control-plane-v2 fondation » (périmètre limité aux tâches 1/2/3/5/6). */
  async runSourceAction(sourceId: string, action: LevelActionId, options: RunPipelineActionOptions = {}, signal?: AbortSignal): Promise<ActionEnvelope<unknown>> {
    return this.writeFull(`/sources/${encodeURIComponent(sourceId)}/actions/${action}`, { dry_run: options.dryRun ?? false, ...(options.confirmationToken ? { confirmation_token: options.confirmationToken } : {}) }, signal);
  }

  /** POST /v2/destinations/{id}/actions/{pause,resume} — CONTRAT SEUL, voir `runSourceAction`. */
  async runDestinationAction(destinationId: string, action: LevelActionId, options: RunPipelineActionOptions = {}, signal?: AbortSignal): Promise<ActionEnvelope<unknown>> {
    return this.writeFull(`/destinations/${encodeURIComponent(destinationId)}/actions/${action}`, { dry_run: options.dryRun ?? false, ...(options.confirmationToken ? { confirmation_token: options.confirmationToken } : {}) }, signal);
  }

  /** POST /v2/actions/{pause_all,resume_all} — CONTRAT SEUL, voir `runSourceAction`. */
  async runFleetAction(action: 'pause_all' | 'resume_all', options: RunPipelineActionOptions = {}, signal?: AbortSignal): Promise<ActionEnvelope<unknown>> {
    return this.writeFull(`/actions/${action}`, { dry_run: options.dryRun ?? false, ...(options.confirmationToken ? { confirmation_token: options.confirmationToken } : {}) }, signal);
  }

  /** GET /v2/pipelines/{id}/metrics?window=1h|24h — CONTRAT SEUL (voir en-tête). */
  async getMetrics(pipelineId: string, window: '1h' | '24h' = '1h', signal?: AbortSignal): Promise<MetricsSeries> {
    const payload = await this.getJson(`/pipelines/${encodeURIComponent(pipelineId)}/metrics?window=${window}`, signal);
    return parseMetricsSeries(payload, window);
  }

  /** GET /v2/pipelines/{id}/logs?since=&level=&correlate_incident= — CONTRAT SEUL. */
  async getLogs(
    pipelineId: string,
    options: { readonly since?: string; readonly level?: LogLevel; readonly correlateIncident?: boolean } = {},
    signal?: AbortSignal,
  ): Promise<readonly LogEntry[]> {
    const params = new URLSearchParams();
    if (options.since) params.set('since', options.since);
    if (options.level) params.set('level', options.level);
    if (options.correlateIncident) params.set('correlate_incident', 'true');
    const query = params.toString();
    const payload = await this.getJson(`/pipelines/${encodeURIComponent(pipelineId)}/logs${query ? `?${query}` : ''}`, signal);
    const items = Array.isArray((payload as Record<string, unknown>).items) ? (payload as Record<string, unknown>).items as unknown[] : [];
    return items.map(parseLogEntry);
  }

  /** GET /v2/costs?scope=connection|table&window= — CONTRAT SEUL. */
  async getCosts(scope: 'connection' | 'table', id: string, window?: string, signal?: AbortSignal): Promise<CostSnapshot> {
    const params = new URLSearchParams({ scope, id });
    if (window) params.set('window', window);
    const payload = await this.getJson(`/costs?${params.toString()}`, signal);
    return parseCostSnapshot(payload, scope, id);
  }

  /** GET /v2/confirmations?state= — existe côté serveur. */
  async listConfirmations(state: ConfirmationState | 'all' = 'pending', signal?: AbortSignal): Promise<readonly ConfirmationRecord[]> {
    const query = `?state=${encodeURIComponent(state)}`;
    const payload = await this.getJson(`/confirmations${query}`, signal);
    const items = Array.isArray((payload as Record<string, unknown>).items) ? (payload as Record<string, unknown>).items as unknown[] : [];
    return items.map(parseConfirmationRecord);
  }

  /** GET /v2/confirmations/{id} — existe côté serveur. */
  async getConfirmation(confirmationId: string, signal?: AbortSignal): Promise<ConfirmationRecord> {
    return parseConfirmationRecord(await this.getJson(`/confirmations/${encodeURIComponent(confirmationId)}`, signal));
  }

  /** POST /v2/confirmations/{id}/approve — existe côté serveur (identité
   *  authentifiée scope `operate`, ou `{token}` de lien signé non authentifié). */
  async approveConfirmation(confirmationId: string, token?: string, signal?: AbortSignal): Promise<ConfirmationRecord> {
    const envelope = await this.write(`/confirmations/${encodeURIComponent(confirmationId)}/approve`, token ? { token } : {}, signal);
    return parseConfirmationRecord(envelope.after);
  }

  /** POST /v2/confirmations/{id}/reject — existe côté serveur. */
  async rejectConfirmation(confirmationId: string, signal?: AbortSignal): Promise<ConfirmationRecord> {
    const envelope = await this.write(`/confirmations/${encodeURIComponent(confirmationId)}/reject`, {}, signal);
    return parseConfirmationRecord(envelope.after);
  }

  /** GET /v2/audit?... — existe côté serveur (scope admin). */
  async listAudit(
    filters: { readonly actorKind?: string; readonly action?: string; readonly resourceType?: string; readonly resourceId?: string; readonly limit?: number } = {},
    signal?: AbortSignal,
  ): Promise<readonly AuditRecord[]> {
    const params = new URLSearchParams();
    if (filters.actorKind) params.set('actor_kind', filters.actorKind);
    if (filters.action) params.set('action', filters.action);
    if (filters.resourceType) params.set('resource_type', filters.resourceType);
    if (filters.resourceId) params.set('resource_id', filters.resourceId);
    if (filters.limit) params.set('limit', String(filters.limit));
    const query = params.toString();
    const payload = await this.getJson(`/audit${query ? `?${query}` : ''}`, signal);
    const items = Array.isArray((payload as Record<string, unknown>).items) ? (payload as Record<string, unknown>).items as unknown[] : [];
    return items.map(parseAuditRecord);
  }

  /**
   * GET /v2/events (SSE) — existe côté serveur, contrat `Last-Event-ID`
   * repris de `/v1/events` (docs/api-v2.md « SSE étendu »). Reconnexion avec
   * backoff exponentiel borné, identique au client v1
   * (`controlPlaneClient.ts::subscribe`) ; le curseur du dernier événement
   * reçu est renvoyé via `Last-Event-ID` au reconnect pour ne rien manquer.
   */
  subscribeEvents(onEvent: (event: CockpitEvent) => void, onConnection?: (connected: boolean) => void): () => void {
    let source: CockpitEventSourceLike | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let retryDelayMs = this.reconnectDelayMs;
    let stopped = false;
    let lastEventId: string | null = null;

    const connect = () => {
      if (stopped) return;
      const url = `${this.basePath}/events${lastEventId ? `?last_event_id=${encodeURIComponent(lastEventId)}` : ''}`;
      const current = this.eventSourceFactory(url);
      source = current;
      const isCurrent = () => !stopped && source === current;

      current.onopen = () => {
        if (!isCurrent()) return;
        retryDelayMs = this.reconnectDelayMs;
        onConnection?.(true);
      };
      current.onerror = () => {
        if (!isCurrent()) return;
        current.close();
        source = null;
        onConnection?.(false);
        if (retryTimer !== null) return;
        const scheduledDelayMs = retryDelayMs;
        retryDelayMs = Math.min(this.maxReconnectDelayMs, Math.max(this.reconnectDelayMs, retryDelayMs > 0 ? retryDelayMs * 2 : 1));
        retryTimer = setTimeout(() => {
          retryTimer = null;
          connect();
        }, scheduledDelayMs);
      };
      for (const type of ['pipeline.state_changed', 'action.pending_confirmation', 'action.completed', 'alert.fired', 'alert.resolved']) {
        current.addEventListener(type, (event) => {
          if (!isCurrent()) return;
          const id = messageEventId(event);
          if (id !== null) lastEventId = id;
          onEvent({ id: id === null ? 0 : Number(id), type, payload: safeJsonParse(event.data) });
        });
      }
    };

    connect();
    return () => {
      stopped = true;
      if (retryTimer !== null) clearTimeout(retryTimer);
      retryTimer = null;
      source?.close();
      source = null;
    };
  }

  private async getJson(path: string, signal?: AbortSignal): Promise<unknown> {
    const response = await this.fetchFn(`${this.basePath}${path}`, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      signal,
    });
    return this.readJsonOrThrow(response, path);
  }

  /** Comme `write`, mais conserve `verify`/`dry_run` de l'enveloppe générique
   *  (avant/après/vérifie — docs/api-v2.md « Enveloppe d'action générique »). */
  private async writeFull(path: string, body: Record<string, unknown>, signal?: AbortSignal, method: 'POST' | 'PATCH' | 'DELETE' = 'POST'): Promise<ActionEnvelope<unknown>> {
    const response = await this.fetchFn(`${this.basePath}${path}`, {
      method,
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
        'Idempotency-Key': this.idempotencyKeyFactory(),
      },
      body: JSON.stringify(body),
      signal,
    });
    const payload = await this.readJsonOrThrow(response, path) as Record<string, unknown>;
    const verify = typeof payload.verify === 'object' && payload.verify !== null
      ? { method: String((payload.verify as Record<string, unknown>).method ?? 'GET'), path: String((payload.verify as Record<string, unknown>).path ?? '') }
      : null;
    return { before: payload.before ?? null, after: payload.after ?? null, verify, dryRun: payload.dry_run ?? null };
  }

  private async write(path: string, body: Record<string, unknown>, signal?: AbortSignal, method: 'POST' | 'PATCH' = 'POST'): Promise<{ readonly before: unknown; readonly after: unknown }> {
    const response = await this.fetchFn(`${this.basePath}${path}`, {
      method,
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
        'Idempotency-Key': this.idempotencyKeyFactory(),
      },
      body: JSON.stringify(body),
      signal,
    });
    const payload = await this.readJsonOrThrow(response, path);
    return payload as { readonly before: unknown; readonly after: unknown };
  }

  private async readJsonOrThrow(response: Response, path: string): Promise<unknown> {
    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      throw new ControlPlaneV2Error(response.status, 'internal_error', `Réponse illisible (${path})`, 'Réessayez plus tard.', true);
    }
    if (!response.ok) {
      throw parseErrorEnvelope(response.status, payload);
    }
    return payload;
  }
}

function parseErrorEnvelope(status: number, payload: unknown): ControlPlaneV2Error {
  const error = typeof payload === 'object' && payload !== null ? (payload as Record<string, unknown>).error : null;
  if (typeof error !== 'object' || error === null) {
    return new ControlPlaneV2Error(status, 'internal_error', 'Le service a renvoyé une erreur inattendue.', 'Réessayez plus tard.', true);
  }
  const record = error as Record<string, unknown>;
  const rawCode = typeof record.code === 'string' ? record.code : 'internal_error';
  const code = ERROR_CATALOGUE.has(rawCode) ? rawCode : 'internal_error';
  const message = typeof record.message === 'string' && record.message.trim() ? record.message : 'Erreur inconnue du service.';
  const nextAction = typeof record.next_action === 'string' && record.next_action.trim() ? record.next_action : 'Réessayez plus tard.';
  const retryable = typeof record.retryable === 'boolean' ? record.retryable : false;
  return new ControlPlaneV2Error(status, code, message, nextAction, retryable);
}

/** ``{"ok": bool, "detail": str}`` — forme des sous-résultats de
 *  ``SourceProbeResult.to_dict`` (``network``/``tls``/``authentication``),
 *  distincte de ``{"state","message"}`` utilisée ailleurs dans le
 *  catalogue v2. ``ok: true`` -> ``state: 'ok'``, sinon ``'failed'``. */
function probeCheckResult(value: unknown): CheckResult {
  const record = typeof value === 'object' && value !== null ? value as Record<string, unknown> : {};
  const message = typeof record.detail === 'string' ? record.detail : '';
  return { state: record.ok === true ? 'ok' : 'failed', message };
}

function parseSourceTestResult(value: unknown): SourceTestResult {
  const record = typeof value === 'object' && value !== null ? value as Record<string, unknown> : {};
  if (typeof record.reachable === 'string') {
    // Aucune sonde câblée sur cette installation (`SourcesService.test`
    // sans `probe` injecté) — jamais présenté comme un test réussi.
    return { kind: 'unavailable', secretSet: record.secret_set === true };
  }
  const tlsRecord = typeof record.tls === 'object' && record.tls !== null ? record.tls as Record<string, unknown> : {};
  return {
    kind: 'probed',
    reachable: record.reachable === true,
    network: probeCheckResult(record.network),
    tls: probeCheckResult(record.tls),
    tlsFingerprint: typeof tlsRecord.fingerprint === 'string' ? tlsRecord.fingerprint : null,
    tlsTrust:
      tlsRecord.trust === 'system' || tlsRecord.trust === 'pinned' || tlsRecord.trust === 'unknown'
        ? tlsRecord.trust
        : null,
    tlsCertificatePem: typeof tlsRecord.certificate_pem === 'string' ? tlsRecord.certificate_pem : null,
    authentication: probeCheckResult(record.authentication),
    ibmiVersion: typeof record.ibmi_version === 'string' ? record.ibmi_version : null,
    detectedTimeZone: typeof record.detected_time_zone === 'string' ? record.detected_time_zone : null,
    timezoneAmbiguous: record.timezone_ambiguous === true,
  };
}

function parseSourceRecord(value: unknown): SourceRecord {
  const record = typeof value === 'object' && value !== null ? value as Record<string, unknown> : {};
  return {
    id: String(record.id ?? ''),
    displayName: typeof record.display_name === 'string' ? record.display_name : null,
    host: typeof record.ibmi_host === 'string' ? record.ibmi_host : '',
    ibmiUser: typeof record.ibmi_user === 'string' ? record.ibmi_user : undefined,
  };
}

const DESTINATION_VERIFICATION_STATES = new Set<DestinationVerificationState>(['declared_not_verified', 'verified', 'failed']);

function parseDestinationRecord(value: unknown): DestinationRecord {
  const record = typeof value === 'object' && value !== null ? value as Record<string, unknown> : {};
  const state = record.verification_state;
  return {
    id: String(record.id ?? ''),
    accountIdentifier: typeof record.snowflake_account === 'string' ? record.snowflake_account : '',
    destinationDatabase: typeof record.destination_database === 'string' ? record.destination_database : null,
    destinationSchema: typeof record.destination_schema === 'string' ? record.destination_schema : null,
    verificationState: DESTINATION_VERIFICATION_STATES.has(state as DestinationVerificationState)
      ? state as DestinationVerificationState
      : 'declared_not_verified',
    sqlScript: typeof record.setup_script === 'string' ? record.setup_script : '',
    privateKeyPem: typeof record.private_key_pem === 'string' ? record.private_key_pem : null,
  };
}

const TABLE_READINESS_STATES = new Set<TableReadiness>(['ready', 'not_journaled', 'images_incomplete', 'no_key', 'journal_mismatch']);
const KEY_STRATEGIES = new Set<KeyStrategy>(['primary', 'unique_index', 'rrn']);

/** ``tables.to_dict()`` (``services/tables.py``) : ``schema_name``/
 *  ``table_name`` (pas ``library``/``name``), ``discovered_row_count``/
 *  ``discovered_size_bytes`` (pas ``approx_*``), ``readiness`` déjà classée
 *  côté serveur (jamais recalculée ici), ``key_strategy``/``key_columns``
 *  (pas ``key_status``), ``cl_fix_commands`` une liste d'objets
 *  ``{"command","reason"}`` (pas des chaînes). */
function parseDiscoveredTable(value: unknown): DiscoveredTable {
  const record = typeof value === 'object' && value !== null ? value as Record<string, unknown> : {};
  const readiness = TABLE_READINESS_STATES.has(record.readiness as TableReadiness) ? record.readiness as TableReadiness : 'not_journaled';
  const keyStrategy = KEY_STRATEGIES.has(record.key_strategy as KeyStrategy) ? record.key_strategy as KeyStrategy : 'rrn';
  const keyColumns = Array.isArray(record.key_columns)
    ? record.key_columns.filter((item): item is string => typeof item === 'string')
    : [];
  const clFixCommands = Array.isArray(record.cl_fix_commands)
    ? record.cl_fix_commands.map((item) => {
        if (typeof item === 'string') return item;
        const entry = typeof item === 'object' && item !== null ? item as Record<string, unknown> : {};
        return typeof entry.command === 'string' ? entry.command : '';
      }).filter((command) => command.length > 0)
    : [];
  return {
    id: String(record.id ?? ''),
    library: typeof record.schema_name === 'string' ? record.schema_name : '',
    name: typeof record.table_name === 'string' ? record.table_name : '',
    approxRowCount: typeof record.discovered_row_count === 'number' ? record.discovered_row_count : 0,
    approxSizeBytes: typeof record.discovered_size_bytes === 'number' ? record.discovered_size_bytes : 0,
    readiness,
    keyStrategy,
    keyColumns,
    clFixCommands,
  };
}

function parseAdminUser(value: unknown): AdminUser {
  const record = typeof value === 'object' && value !== null ? value as Record<string, unknown> : {};
  return {
    id: String(record.id ?? ''),
    email: typeof record.email === 'string' ? record.email : '',
    role: typeof record.role === 'string' ? record.role : 'admin',
    // Le serveur (UserRecord.to_dict) porte `activated_at` (horodatage ou
    // `null`), jamais un booléen `activated` — seule la fixture de
    // démonstration en émettait un directement. Les deux formes sont
    // acceptées ici pour rester compatible avec les deux.
    activated: record.activated === true || (typeof record.activated_at === 'string' && record.activated_at.length > 0),
  };
}

function parseAuthUser(value: unknown): AuthUser {
  const record = typeof value === 'object' && value !== null ? value as Record<string, unknown> : {};
  return {
    email: typeof record.email === 'string' ? record.email : '',
    role: typeof record.role === 'string' ? record.role : '',
  };
}

const PIPELINE_DECLARED_STATES = new Set<PipelineDeclaredState>(['not_started', 'copying', 'live', 'paused', 'attention', 'stopped']);
const CONFIRMATION_STATES = new Set<ConfirmationState>(['pending', 'approved', 'rejected', 'used', 'expired']);
const COST_STATUSES = new Set<CostStatus>(['measured', 'estimated', 'absent']);
const LOG_LEVELS = new Set<LogLevel>(['info', 'warning', 'error']);

function parsePipelineRecord(value: unknown): PipelineRecordV2 {
  const record = typeof value === 'object' && value !== null ? value as Record<string, unknown> : {};
  const declaredState = record.declared_state;
  return {
    id: String(record.id ?? ''),
    declaredState: PIPELINE_DECLARED_STATES.has(declaredState as PipelineDeclaredState) ? declaredState as PipelineDeclaredState : 'not_started',
  };
}

function parsePipelineListRecord(value: unknown): PipelineListRecordV2 {
  const record = typeof value === 'object' && value !== null ? value as Record<string, unknown> : {};
  const absentReasonsRaw = typeof record.absent_reasons === 'object' && record.absent_reasons !== null ? record.absent_reasons as Record<string, unknown> : {};
  const absentReasons: Record<string, string> = {};
  for (const [key, reason] of Object.entries(absentReasonsRaw)) {
    if (typeof reason === 'string') absentReasons[key] = reason;
  }
  return {
    ...parsePipelineRecord(value),
    tableId: String(record.table_id ?? ''),
    sourceId: String(record.source_id ?? ''),
    destinationId: String(record.destination_id ?? ''),
    observation: {
      lagSeconds: typeof record.lag_seconds === 'number' ? record.lag_seconds : null,
      throughputRowsPerSecond: typeof record.throughput_rows_per_second === 'number' ? record.throughput_rows_per_second : null,
      rowsSource: typeof record.rows_source === 'number' ? record.rows_source : null,
      rowsDestination: typeof record.rows_destination === 'number' ? record.rows_destination : null,
      lastArrivalAt: typeof record.last_arrival_at === 'string' ? record.last_arrival_at : null,
      absentReasons,
    },
  };
}

function buildReplayQuery(options: RunPipelineActionOptions): string {
  const params = new URLSearchParams();
  if (options.fromSequence !== undefined) params.set('from_sequence', String(options.fromSequence));
  if (options.toSequence !== undefined) params.set('to_sequence', String(options.toSequence));
  const query = params.toString();
  return query ? `?${query}` : '';
}

function parseConfirmationRecord(value: unknown): ConfirmationRecord {
  const record = typeof value === 'object' && value !== null ? value as Record<string, unknown> : {};
  const state = record.state;
  const available = typeof record.available === 'object' && record.available !== null
    ? record.available as Record<string, unknown> : {};
  return {
    id: String(record.id ?? ''),
    actionRef: typeof record.action_ref === 'string' ? record.action_ref : '',
    resourceType: typeof record.resource_type === 'string' ? record.resource_type : '',
    resourceId: typeof record.resource_id === 'string' ? record.resource_id : '',
    reason: typeof record.reason === 'string' ? record.reason : '',
    riskEstimate: typeof record.risk_estimate === 'string' ? record.risk_estimate : null,
    requestedByKind: typeof record.requested_by_kind === 'string' ? record.requested_by_kind : null,
    requestedById: typeof record.requested_by_id === 'string' ? record.requested_by_id : null,
    expiresAt: typeof record.expires_at === 'string' ? record.expires_at : null,
    state: CONFIRMATION_STATES.has(state as ConfirmationState) ? state as ConfirmationState : 'pending',
    approvedByKind: typeof record.approved_by_kind === 'string' ? record.approved_by_kind : null,
    approvedById: typeof record.approved_by_id === 'string' ? record.approved_by_id : null,
    approvedAt: typeof record.approved_at === 'string' ? record.approved_at : null,
    createdAt: typeof record.created_at === 'string' ? record.created_at : null,
    available: { approve: available.approve === true, reject: available.reject === true, execute: available.execute === true },
  };
}

function parseAuditRecord(value: unknown): AuditRecord {
  const record = typeof value === 'object' && value !== null ? value as Record<string, unknown> : {};
  return {
    id: String(record.id ?? ''),
    at: typeof record.at === 'string' ? record.at : '',
    actorKind: typeof record.actor_kind === 'string' ? record.actor_kind : '',
    actorId: typeof record.actor_id === 'string' ? record.actor_id : null,
    actorDisplay: typeof record.actor_display === 'string' ? record.actor_display : null,
    mcpClient: typeof record.mcp_client === 'string' ? record.mcp_client : null,
    action: typeof record.action === 'string' ? record.action : '',
    resourceType: typeof record.resource_type === 'string' ? record.resource_type : '',
    resourceId: typeof record.resource_id === 'string' ? record.resource_id : null,
    requestId: typeof record.request_id === 'string' ? record.request_id : null,
    idempotencyKey: typeof record.idempotency_key === 'string' ? record.idempotency_key : null,
    dryRun: record.dry_run === true,
    confirmationId: typeof record.confirmation_id === 'string' ? record.confirmation_id : null,
    status: typeof record.status === 'string' ? record.status : '',
    before: record.before ?? null,
    after: record.after ?? null,
  };
}

function parseCostSnapshot(value: unknown, scope: 'connection' | 'table', id: string): CostSnapshot {
  const record = typeof value === 'object' && value !== null ? value as Record<string, unknown> : {};
  const status = record.status;
  return {
    scope,
    id,
    window: typeof record.window === 'string' ? record.window : null,
    status: COST_STATUSES.has(status as CostStatus) ? status as CostStatus : 'absent',
    amount: typeof record.amount === 'number' ? record.amount : null,
    currency: typeof record.currency === 'string' ? record.currency : null,
    basis: typeof record.basis === 'string' ? record.basis : null,
    collectedAt: typeof record.collected_at === 'string' ? record.collected_at : null,
  };
}

function parseLogEntry(value: unknown): LogEntry {
  const record = typeof value === 'object' && value !== null ? value as Record<string, unknown> : {};
  const level = record.level;
  return {
    at: typeof record.at === 'string' ? record.at : '',
    level: LOG_LEVELS.has(level as LogLevel) ? level as LogLevel : 'info',
    message: typeof record.message === 'string' ? record.message : '',
    incidentId: typeof record.incident_id === 'string' ? record.incident_id : null,
  };
}

function parseMetricsSeries(value: unknown, window: '1h' | '24h'): MetricsSeries {
  const record = typeof value === 'object' && value !== null ? value as Record<string, unknown> : {};
  const items = Array.isArray(record.points) ? record.points as unknown[] : [];
  return {
    window,
    provenance: typeof record.provenance === 'string' ? record.provenance : null,
    freshness: typeof record.freshness === 'string' ? record.freshness : null,
    collectedAt: typeof record.collected_at === 'string' ? record.collected_at : null,
    reason: typeof record.reason === 'string' ? record.reason : null,
    points: items.map((item) => {
      const point = typeof item === 'object' && item !== null ? item as Record<string, unknown> : {};
      return {
        at: typeof point.at === 'string' ? point.at : '',
        lagSeconds: typeof point.lag_seconds === 'number' ? point.lag_seconds : null,
        throughputRowsPerSecond: typeof point.throughput_rows_per_second === 'number' ? point.throughput_rows_per_second : null,
      };
    }),
  };
}

function messageEventId(event: MessageEvent<string>): string | null {
  const withId = event as MessageEvent<string> & { lastEventId?: string };
  return typeof withId.lastEventId === 'string' && withId.lastEventId ? withId.lastEventId : null;
}

function safeJsonParse(data: string): unknown {
  try {
    return JSON.parse(data);
  } catch {
    return null;
  }
}
