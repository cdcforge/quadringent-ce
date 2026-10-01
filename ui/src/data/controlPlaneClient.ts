import { ControlPlaneParseError, parseOverview, parsePipeline, type Overview, type Pipeline } from '../domain/controlPlane.ts';
import { installedSiteIdentity } from '../domain/siteIdentity.ts';
import { parseOnboardingDefaults, type OnboardingDefaults } from '../domain/onboarding.ts';

export type ActionId = 'refresh' | 'prepare' | 'start' | 'pause' | 'resume';

export interface PipelineActionRequest {
  readonly fleet_id: string;
  readonly environment: string;
  readonly confirmation: string | null;
}

export type ActionOverallState = 'succeeded' | 'failed' | 'conflict' | 'unavailable';
export type ActionIntentState = 'recorded' | 'rejected';
export type ActionExecutionState = 'completed' | 'failed' | 'not_started';
export type ActionObservedEffectState = 'succeeded' | 'failed' | 'unknown';

export interface ActionStage {
  readonly state: ActionIntentState | ActionExecutionState | ActionObservedEffectState;
  readonly code: string;
  readonly message: string;
}

interface PipelineActionReceiptBase {
  readonly id: string;
  readonly action: ActionId;
  readonly environment: string;
  readonly createdAt: string;
  readonly state: ActionOverallState;
  readonly stages: {
    readonly intent: ActionStage & { readonly state: ActionIntentState };
    readonly execution: ActionStage & { readonly state: ActionExecutionState };
    readonly observedEffect: ActionStage & { readonly state: ActionObservedEffectState };
  };
}

export interface PipelineActionReceipt extends PipelineActionReceiptBase {
  readonly fleetId: string;
}

export class ControlPlaneActionError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string) {
    super(`Control plane indisponible (${status})`);
    this.name = 'ControlPlaneActionError';
    this.status = status;
    this.code = code;
  }
}

const ACTION_IDS = new Set<ActionId>(['refresh', 'prepare', 'start', 'pause', 'resume']);
const ACTION_OVERALL_STATES = new Set<ActionOverallState>(['succeeded', 'failed', 'conflict', 'unavailable']);
const ACTION_INTENT_STATES = new Set<ActionIntentState>(['recorded', 'rejected']);
const ACTION_EXECUTION_STATES = new Set<ActionExecutionState>(['completed', 'failed', 'not_started']);
const ACTION_OBSERVED_EFFECT_STATES = new Set<ActionObservedEffectState>(['succeeded', 'failed', 'unknown']);
const SAFE_ACTION_CODES = new Set([
  'invalid_request',
  'forbidden',
  'not_found',
  'wrong_environment',
  'wrong_confirmation',
  'capability_unavailable',
  'executor_unavailable',
  'action_in_progress',
  'action_failed',
  'internal_error',
]);
const SAFE_STAGE_CODE = /^[a-z][a-z0-9_]{0,63}$/;
const SAFE_STAGE_MESSAGE = /^[\w][\w .,'’-]{0,119}$/u;
const SAFE_RECEIPT_ID = /^[A-Za-z0-9_-]{8,128}$/;
const RECEIPT_UTC = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|\+00:00)$/;

export interface EventSourceLike {
  addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void;
  close(): void;
  onerror: ((event: Event) => void) | null;
  onopen?: ((event: Event) => void) | null;
}

export interface ControlPlaneClientOptions {
  readonly basePath?: string;
  readonly fetchFn?: (input: string, init?: RequestInit) => Promise<Response>;
  readonly eventSourceFactory?: (url: string) => EventSourceLike;
  readonly reconnectDelayMs?: number;
  readonly maxReconnectDelayMs?: number;
  readonly networkEvents?: Pick<EventTarget, 'addEventListener' | 'removeEventListener'>;
}

export interface PipelineListRequest {
  readonly signal?: AbortSignal;
  readonly etag?: string | null;
}

export type PipelineListResult =
  | {
      readonly kind: 'updated';
      readonly pipelines: readonly Pipeline[];
      readonly revision: number;
      readonly etag: string | null;
    }
  | { readonly kind: 'not-modified' };

export interface OverviewRequest {
  readonly signal?: AbortSignal;
  readonly etag?: string | null;
}

export type OverviewResult =
  | { readonly kind: 'updated'; readonly overview: Overview; readonly etag: string | null }
  | { readonly kind: 'not-modified' };

export interface PipelineRequest {
  readonly id: string;
  readonly signal?: AbortSignal;
  readonly etag?: string | null;
}

export type PipelineResult =
  | {
      readonly kind: 'updated';
      readonly pipeline: Pipeline;
      readonly revision: number;
      readonly etag: string | null;
    }
  | { readonly kind: 'not-modified' };

export interface OnboardingDefaultsRequest {
  readonly signal?: AbortSignal;
  readonly etag?: string | null;
}

export type OnboardingDefaultsResult =
  | {
      readonly kind: 'updated';
      readonly defaults: OnboardingDefaults;
      readonly etag: string | null;
    }
  | { readonly kind: 'not-modified' };

export class ControlPlaneClient {
  private readonly basePath: string;
  private readonly fetchFn: (input: string, init?: RequestInit) => Promise<Response>;
  private readonly eventSourceFactory: (url: string) => EventSourceLike;
  private readonly reconnectDelayMs: number;
  private readonly maxReconnectDelayMs: number;
  private readonly networkEvents: Pick<EventTarget, 'addEventListener' | 'removeEventListener'> | undefined;

  constructor(options: ControlPlaneClientOptions = {}) {
    this.basePath = (options.basePath ?? '/v1').replace(/\/$/, '');
    this.fetchFn = options.fetchFn ?? ((input, init) => fetch(input, init));
    this.eventSourceFactory = options.eventSourceFactory ?? ((url) => new EventSource(url));
    this.reconnectDelayMs = Math.max(0, options.reconnectDelayMs ?? 1_000);
    this.maxReconnectDelayMs = Math.max(this.reconnectDelayMs, options.maxReconnectDelayMs ?? 15_000);
    this.networkEvents = options.networkEvents ?? (typeof window === 'undefined' ? undefined : window);
  }

  async listPipelines(signal?: AbortSignal): Promise<Pipeline[]> {
    const result = await this.fetchPipelineList({ signal });
    if (result.kind === 'not-modified') throw new Error('Réponse 304 sans cache de session');
    return [...result.pipelines];
  }

  async getOverview(signal?: AbortSignal): Promise<Overview> {
    const result = await this.fetchOverview({ signal });
    if (result.kind === 'not-modified') throw new Error('Réponse 304 sans cache de session');
    return result.overview;
  }

  async fetchOverview(options: OverviewRequest): Promise<OverviewResult> {
    const path = '/overview';
    const response = await this.request(path, options.signal, options.etag);
    if (response.status === 304) return { kind: 'not-modified' };
    return {
      kind: 'updated',
      overview: parseOverview(await json(response, path)),
      etag: response.headers.get('ETag'),
    };
  }

  async fetchPipelineList(options: PipelineListRequest): Promise<PipelineListResult> {
    const path = '/pipelines';
    const response = await this.request(path, options.signal, options.etag);
    if (response.status === 304) return { kind: 'not-modified' };
    const body = await json(response, path);
    const object = objectValue(body, path);
    const revision = responseRevision(object.revision, path);
    const pipelines = arrayValue(object.pipelines, `${path}.pipelines`).map((item, index) => parsePipeline(item, `${path}.pipelines[${index}]`));
    return {
      kind: 'updated',
      pipelines,
      revision,
      etag: response.headers.get('ETag'),
    };
  }

  async getPipeline(id: string, signal?: AbortSignal): Promise<Pipeline> {
    const result = await this.fetchPipeline({ id, signal });
    if (result.kind === 'not-modified') throw new Error('Réponse 304 sans cache de session');
    return result.pipeline;
  }

  async fetchPipeline(options: PipelineRequest): Promise<PipelineResult> {
    if (!options.id.trim()) throw new Error('Identifiant de pipeline invalide');
    const path = `/pipelines/${encodeURIComponent(options.id)}`;
    const response = await this.request(path, options.signal, options.etag);
    if (response.status === 304) return { kind: 'not-modified' };
    const object = objectValue(await json(response, path), path);
    const revision = responseRevision(object.revision, path);
    return {
      kind: 'updated',
      pipeline: parsePipeline(object.pipeline, `${path}.pipeline`),
      revision,
      etag: response.headers.get('ETag'),
    };
  }

  async getOnboardingDefaults(signal?: AbortSignal): Promise<OnboardingDefaults> {
    const result = await this.fetchOnboardingDefaults({ signal });
    if (result.kind === 'not-modified') throw new Error('Réponse 304 sans cache de session');
    return result.defaults;
  }

  async fetchOnboardingDefaults(options: OnboardingDefaultsRequest = {}): Promise<OnboardingDefaultsResult> {
    const path = '/onboarding/defaults';
    const response = await this.request(path, options.signal, options.etag);
    if (response.status === 304) return { kind: 'not-modified' };
    return {
      kind: 'updated',
      defaults: parseOnboardingDefaults(await json(response, path)),
      etag: response.headers.get('ETag'),
    };
  }

  async evaluateOnboarding(payload: Record<string, unknown>, signal?: AbortSignal): Promise<unknown> {
    const path = '/onboarding/evaluate';
    const response = await this.fetchFn(`${this.basePath}${path}`, {
      method: 'POST',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal,
    });
    if (!response.ok) throw new Error(`Control plane indisponible (${response.status})`);
    return json(response, path);
  }

  /**
   * Crée une liaison. Le service valide le corps avec le même juge que le
   * parcours (`onboarding/evaluate`) : un refus revient sous la forme d'un
   * verdict, pas d'une erreur technique — l'écran peut donc le réafficher
   * tel quel dans les mots de l'opérateur.
   */
  async createConnection(
    payload: Record<string, unknown>,
    signal?: AbortSignal,
  ): Promise<{ readonly ok: true; readonly connection: unknown } | { readonly ok: false; readonly verdict: unknown }> {
    const path = '/connections';
    const response = await this.fetchFn(`${this.basePath}${path}`, {
      method: 'POST',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal,
    });
    if (response.status === 201) {
      return { ok: true, connection: await json(response, path) };
    }
    if (response.status === 400) {
      return { ok: false, verdict: await json(response, path) };
    }
    throw new Error(`Création impossible (${response.status})`);
  }

  async runPipelineAction(
    pipelineId: string,
    action: ActionId,
    payload: PipelineActionRequest,
    signal?: AbortSignal,
  ): Promise<PipelineActionReceipt> {
    if (!pipelineId.trim()) throw new Error('Identifiant de pipeline invalide');
    if (!ACTION_IDS.has(action)) throw new ControlPlaneParseError('action');
    const path = `/pipelines/${encodeURIComponent(pipelineId)}/actions/${action}`;
    const response = await this.fetchFn(`${this.basePath}${path}`, {
      method: 'POST',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal,
    });
    if (response.status === 200) {
      const receipt = parsePipelineActionReceipt(await json(response, path), path, action);
      if (receipt.state !== 'succeeded') throw new ControlPlaneParseError(`${path}.state`);
      return receipt;
    }
    if (response.status === 409) {
      const body = await json(response, path);
      try {
        const receipt = parsePipelineActionReceipt(body, path, action);
        if (receipt.state === 'succeeded') throw new ControlPlaneParseError(`${path}.state`);
        return receipt;
      } catch (error) {
        if (!(error instanceof ControlPlaneParseError)) throw error;
        const code = safeActionErrorCode(body);
        if (code !== null) throw new ControlPlaneActionError(response.status, code);
        throw error;
      }
    }
    if (response.status === 500) {
      throw new Error(`Control plane indisponible (${response.status})`);
    }
    let parsed: unknown = null;
    try {
      parsed = await json(response, path);
    } catch {
      parsed = null;
    }
    const code = safeActionErrorCode(parsed);
    if (code !== null) throw new ControlPlaneActionError(response.status, code);
    throw new Error(`Control plane indisponible (${response.status})`);
  }

  subscribe(onRevision: (revision: number) => void, onReset: () => void, onConnection?: (connected: boolean) => void): () => void {
    let source: EventSourceLike | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let retryDelayMs = this.reconnectDelayMs;
    let stopped = false;
    let networkOffline = false;

    const connect = () => {
      if (stopped || networkOffline) return;
      const current = this.eventSourceFactory(`${this.basePath}/events`);
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
        retryDelayMs = Math.min(
          this.maxReconnectDelayMs,
          Math.max(this.reconnectDelayMs, retryDelayMs > 0 ? retryDelayMs * 2 : 1),
        );
        retryTimer = setTimeout(() => {
          retryTimer = null;
          connect();
        }, scheduledDelayMs);
      };
      current.addEventListener('stream.cursor', (event) => {
        if (!isCurrent()) return;
        const revision = eventRevision(event.data);
        if (revision !== null) onRevision(revision);
      });
      current.addEventListener('projection.updated', (event) => {
        if (!isCurrent()) return;
        const revision = eventRevision(event.data);
        if (revision !== null) onRevision(revision);
      });
      current.addEventListener('projection.reset', (event) => {
        if (!isCurrent() || eventRevision(event.data) === null) return;
        onReset();
      });
    };

    const offline = () => {
      if (stopped || networkOffline) return;
      networkOffline = true;
      const previous = source;
      source = null;
      previous?.close();
      if (retryTimer !== null) clearTimeout(retryTimer);
      retryTimer = null;
      onConnection?.(false);
    };
    const online = () => {
      if (stopped || !networkOffline) return;
      networkOffline = false;
      retryDelayMs = this.reconnectDelayMs;
      // Browser connectivity is only a retry trigger. onopen confirms SSE;
      // the controller then reads REST before presenting a recovered API.
      connect();
    };
    this.networkEvents?.addEventListener('offline', offline);
    this.networkEvents?.addEventListener('online', online);
    connect();
    return () => {
      stopped = true;
      this.networkEvents?.removeEventListener('offline', offline);
      this.networkEvents?.removeEventListener('online', online);
      if (retryTimer !== null) clearTimeout(retryTimer);
      retryTimer = null;
      source?.close();
      source = null;
    };
  }

  private async request(path: string, signal?: AbortSignal, etag?: string | null): Promise<Response> {
    const headers = new Headers({ Accept: 'application/json' });
    if (etag) headers.set('If-None-Match', etag);
    const response = await this.fetchFn(`${this.basePath}${path}`, { method: 'GET', headers, signal });
    if (response.status === 304 || response.ok) return response;
    throw new Error(`Control plane indisponible (${response.status})`);
  }
}

async function json(response: Response, path: string): Promise<unknown> {
  try {
    return await response.json();
  } catch (error) {
    throw new Error(`Réponse JSON invalide (${path})`, { cause: error });
  }
}

function eventRevision(value: string): number | null {
  try {
    const object = objectValue(JSON.parse(value), 'sse');
    const revision = object.revision;
    return typeof revision === 'number' && Number.isSafeInteger(revision) && revision >= 0 ? revision : null;
  } catch (error) {
    if (error instanceof ControlPlaneParseError || error instanceof SyntaxError) return null;
    throw error;
  }
}

function objectValue(value: unknown, field: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new ControlPlaneParseError(field);
  return Object.fromEntries(Object.entries(value));
}

function arrayValue(value: unknown, field: string): readonly unknown[] {
  if (!Array.isArray(value)) throw new ControlPlaneParseError(field);
  return value;
}

function responseRevision(value: unknown, path: string): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) {
    throw new ControlPlaneParseError(`${path}.revision`);
  }
  return value;
}

const FLEET_RECEIPT_KEYS = ['id', 'action', 'fleet_id', 'environment', 'created_at', 'state', 'stages'] as const;

function parsePipelineActionReceipt(value: unknown, path: string, action: ActionId): PipelineActionReceipt {
  const object = objectValue(value, path);
  assertExactKeys(object, FLEET_RECEIPT_KEYS, path);
  const site = installedSiteIdentity();
  if (!site) throw new ControlPlaneParseError('site_identity');
  const base = parseReceiptBase(object, path, action, site.runtimeEnvironment);
  if (object.fleet_id !== site.fleetId) throw new ControlPlaneParseError(`${path}.fleet_id`);
  return { ...base, fleetId: site.fleetId };
}

function parseReceiptBase(
  object: Record<string, unknown>,
  path: string,
  action: ActionId,
  runtimeEnvironment: string,
): PipelineActionReceiptBase {
  const receiptAction = enumValue(object.action, ACTION_IDS, `${path}.action`);
  if (receiptAction !== action) throw new ControlPlaneParseError(`${path}.action`);
  if (object.environment !== runtimeEnvironment) throw new ControlPlaneParseError(`${path}.environment`);
  const id = textValue(object.id, `${path}.id`);
  if (!SAFE_RECEIPT_ID.test(id)) throw new ControlPlaneParseError(`${path}.id`);
  const createdAt = utcReceiptTimestamp(object.created_at, `${path}.created_at`);
  const state = enumValue(object.state, ACTION_OVERALL_STATES, `${path}.state`);
  const stagesObject = objectValue(object.stages, `${path}.stages`);
  assertExactKeys(stagesObject, ['intent', 'execution', 'observed_effect'], `${path}.stages`);
  const intent = parseActionStage(stagesObject.intent, `${path}.stages.intent`, ACTION_INTENT_STATES);
  const execution = parseActionStage(stagesObject.execution, `${path}.stages.execution`, ACTION_EXECUTION_STATES);
  const observedEffect = parseActionStage(
    stagesObject.observed_effect,
    `${path}.stages.observed_effect`,
    ACTION_OBSERVED_EFFECT_STATES,
  );
  assertReceiptStageConsistency(state, intent, execution, observedEffect, path);
  return {
    id,
    action: receiptAction,
    environment: runtimeEnvironment,
    createdAt,
    state,
    stages: {
      intent: { ...intent, state: intent.state },
      execution: { ...execution, state: execution.state },
      observedEffect: { ...observedEffect, state: observedEffect.state },
    },
  };
}

function utcReceiptTimestamp(value: unknown, field: string): string {
  const result = textValue(value, field);
  const match = RECEIPT_UTC.exec(result);
  if (!match) throw new ControlPlaneParseError(field);
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);
  const milliseconds = Number(`${match[7] ?? '0'}000`.slice(0, 3));
  const parsed = new Date(0);
  parsed.setUTCFullYear(year, month - 1, day);
  parsed.setUTCHours(hour, minute, second, milliseconds);
  if (Number.isNaN(parsed.getTime())) throw new ControlPlaneParseError(field);
  if (
    parsed.getUTCFullYear() !== year
    || parsed.getUTCMonth() !== month - 1
    || parsed.getUTCDate() !== day
    || parsed.getUTCHours() !== hour
    || parsed.getUTCMinutes() !== minute
    || parsed.getUTCSeconds() !== second
    || parsed.getUTCMilliseconds() !== milliseconds
  ) {
    throw new ControlPlaneParseError(field);
  }
  return result;
}

function assertReceiptStageConsistency(
  state: ActionOverallState,
  intent: { readonly state: ActionIntentState; readonly code: string },
  execution: { readonly state: ActionExecutionState },
  observedEffect: { readonly state: ActionObservedEffectState },
  path: string,
): void {
  const successStages =
    intent.state === 'recorded' && execution.state === 'completed' && observedEffect.state === 'succeeded';
  const failedStages =
    intent.state === 'recorded'
    && (
      (execution.state === 'failed' && (observedEffect.state === 'failed' || observedEffect.state === 'unknown'))
      || (execution.state === 'completed' && observedEffect.state === 'failed')
    );
  const rejectedIdle =
    intent.state === 'rejected' && execution.state === 'not_started' && observedEffect.state === 'unknown';
  const conflictStages = rejectedIdle && intent.code === 'action_in_progress';
  const unavailableStages =
    rejectedIdle && (intent.code === 'capability_unavailable' || intent.code === 'executor_unavailable');
  if (
    (state === 'succeeded') !== successStages
    || (state === 'failed') !== failedStages
    || (state === 'conflict') !== conflictStages
    || (state === 'unavailable') !== unavailableStages
  ) {
    throw new ControlPlaneParseError(`${path}.state`);
  }
}

function parseActionStage<T extends string>(
  value: unknown,
  field: string,
  allowed: Set<T>,
): { readonly state: T; readonly code: string; readonly message: string } {
  const object = objectValue(value, field);
  assertExactKeys(object, ['state', 'code', 'message'], field);
  const state = enumValue(object.state, allowed, `${field}.state`);
  const code = textValue(object.code, `${field}.code`);
  const message = textValue(object.message, `${field}.message`);
  if (!SAFE_STAGE_CODE.test(code)) throw new ControlPlaneParseError(`${field}.code`);
  if (!SAFE_STAGE_MESSAGE.test(message)) throw new ControlPlaneParseError(`${field}.message`);
  return { state, code, message };
}

function safeActionErrorCode(value: unknown): string | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null;
  const object = Object.fromEntries(Object.entries(value));
  if (Object.keys(object).length !== 1 || !('error' in object)) return null;
  const error = object.error;
  if (typeof error !== 'object' || error === null || Array.isArray(error)) return null;
  const errorObject = Object.fromEntries(Object.entries(error));
  if (Object.keys(errorObject).length !== 1 || typeof errorObject.code !== 'string') return null;
  return SAFE_ACTION_CODES.has(errorObject.code) ? errorObject.code : null;
}

function assertExactKeys(value: Record<string, unknown>, keys: readonly string[], field: string): void {
  const expected = new Set(keys);
  const actual = Object.keys(value);
  if (actual.length !== expected.size || actual.some((key) => !expected.has(key))) {
    throw new ControlPlaneParseError(field);
  }
}

function enumValue<T extends string>(value: unknown, allowed: Set<T>, field: string): T {
  if (typeof value !== 'string' || !allowed.has(value as T)) throw new ControlPlaneParseError(field);
  return value as T;
}

function textValue(value: unknown, field: string): string {
  if (typeof value !== 'string' || value.length === 0) throw new ControlPlaneParseError(field);
  return value;
}
