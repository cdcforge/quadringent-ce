import type { Overview, Pipeline } from '../domain/controlPlane.ts';
import type { OverviewRequest, OverviewResult } from './controlPlaneClient.ts';

export type ConnectionState = 'connecting' | 'live' | 'reconnecting' | 'offline';

export type ControlPlaneState =
  | { readonly status: 'loading'; readonly connection: ConnectionState }
  | {
      readonly status: 'ready' | 'refreshing';
      readonly connection: ConnectionState;
      readonly overview: Overview;
      readonly pipelines: readonly Pipeline[];
      readonly lastSuccessAt: Date;
    }
  | {
      readonly status: 'degraded';
      readonly connection: ConnectionState;
      readonly overview: Overview;
      readonly pipelines: readonly Pipeline[];
      readonly lastSuccessAt: Date;
      readonly message: string;
    }
  | { readonly status: 'failed'; readonly connection: ConnectionState; readonly message: string };

export interface ControlPlaneTransport {
  fetchOverview(options: OverviewRequest): Promise<OverviewResult>;
  subscribe(
    onRevision: (revision: number) => void,
    onReset: () => void,
    onConnection?: (connected: boolean) => void,
  ): () => void;
}

interface SuccessfulRead {
  readonly overview: Overview;
  readonly pipelines: readonly Pipeline[];
  readonly at: Date;
}

export class ControlPlaneController {
  private disposed = false;
  private inFlight = false;
  private generation = 0;
  private subscriptionGeneration = 0;
  private appliedRevision = 0;
  private highestCursorSeen = 0;
  private requestedCursorRevision = 0;
  private etag: string | null = null;
  private connection: ConnectionState = 'connecting';
  private sseConnected = false;
  private retryAfterFailure = false;
  private abortController: AbortController | null = null;
  private unsubscribe: (() => void) | null = null;
  private success: SuccessfulRead | null = null;
  private readonly transport: ControlPlaneTransport;
  private readonly publish: (state: ControlPlaneState) => void;
  private readonly clock: () => Date;
  private readonly readTimeoutMs: number;

  state: ControlPlaneState = { status: 'loading', connection: 'connecting' };

  constructor(
    transport: ControlPlaneTransport,
    publish: (state: ControlPlaneState) => void,
    clock: () => Date = () => new Date(),
    readTimeoutMs = 15_000,
  ) {
    if (!Number.isFinite(readTimeoutMs) || readTimeoutMs <= 0) throw new Error('Invalid overview timeout');
    this.transport = transport;
    this.publish = publish;
    this.clock = clock;
    this.readTimeoutMs = readTimeoutMs;
  }

  start(): void {
    if (this.unsubscribe) return;
    if (this.disposed) this.resetSession();
    this.disposed = false;
    const subscriptionGeneration = ++this.subscriptionGeneration;
    this.unsubscribe = this.transport.subscribe(
      (revision) => {
        if (this.isCurrentSubscription(subscriptionGeneration)) this.cursor(revision);
      },
      () => {
        if (this.isCurrentSubscription(subscriptionGeneration)) this.projectionReset();
      },
      (connected) => {
        if (this.isCurrentSubscription(subscriptionGeneration)) this.setConnectionState(connected);
      },
    );
    this.refresh();
  }

  dispose(): void {
    this.disposed = true;
    this.inFlight = false;
    this.generation += 1;
    this.subscriptionGeneration += 1;
    this.abortController?.abort();
    this.abortController = null;
    this.unsubscribe?.();
    this.unsubscribe = null;
  }

  cursor(revision: number): void {
    if (this.disposed || !Number.isSafeInteger(revision) || revision < 0) return;
    this.highestCursorSeen = Math.max(this.highestCursorSeen, revision);
    if (revision <= this.appliedRevision || this.inFlight) return;
    this.refresh();
  }

  refresh(): void {
    if (this.disposed) return;

    this.abortController?.abort();
    const abortController = new AbortController();
    this.abortController = abortController;
    this.inFlight = true;
    const generation = ++this.generation;
    this.requestedCursorRevision = Math.max(
      this.requestedCursorRevision,
      this.highestCursorSeen,
    );

    this.emit(this.success
      ? {
          status: 'refreshing',
          connection: this.connection,
          overview: this.success.overview,
          pipelines: this.success.pipelines,
          lastSuccessAt: this.success.at,
        }
      : { status: 'loading', connection: this.connection });

    this.readOverview(abortController)
      .then((result) => this.applyResult(result, generation))
      .catch((error: unknown) => this.applyFailure(error, generation))
      .finally(() => this.finishRequest(generation));
  }

  private readOverview(abortController: AbortController): Promise<OverviewResult> {
    const { signal } = abortController;
    return new Promise<OverviewResult>((resolve, reject) => {
      const cancelled = () => {
        clearTimeout(timer);
        reject(new DOMException('Lecture annulée', 'AbortError'));
      };
      const timer = setTimeout(() => {
        // Reject as a failure before aborting transport: user cancellation and
        // deadline expiration must not be confused by applyFailure.
        reject(new Error('Le délai de lecture de l’API est dépassé. Réessayez.'));
        abortController.abort();
      }, this.readTimeoutMs);
      signal.addEventListener('abort', cancelled, { once: true });
      const cleanup = () => {
        clearTimeout(timer);
        signal.removeEventListener('abort', cancelled);
      };
      if (signal.aborted) { cancelled(); cleanup(); return; }
      try {
        // fetchOverview includes JSON decoding: slow response bodies are bounded too.
        this.transport.fetchOverview({ signal, etag: this.etag }).then(
          (result) => { cleanup(); resolve(result); },
          (error: unknown) => { cleanup(); reject(error); },
        );
      } catch (error) { cleanup(); reject(error); }
    });
  }

  private applyResult(result: OverviewResult, generation: number): void {
    if (!this.isCurrent(generation)) return;

    if (result.kind === 'not-modified') {
      if (!this.success) throw new Error('Réponse 304 sans cache de session');
      this.emit({
        status: 'ready',
        connection: this.connection,
        overview: this.success.overview,
        pipelines: this.success.pipelines,
        lastSuccessAt: this.success.at,
      });
      return;
    }

    if (this.success && result.overview.revision < this.appliedRevision) {
      this.emit({
        status: 'ready',
        connection: this.connection,
        overview: this.success.overview,
        pipelines: this.success.pipelines,
        lastSuccessAt: this.success.at,
      });
      return;
    }

    const success = { overview: result.overview, pipelines: result.overview.pipelines, at: this.clock() };
    this.success = success;
    this.appliedRevision = result.overview.revision;
    this.etag = result.etag;
    this.retryAfterFailure = false;
    this.emit({
      status: 'ready',
      connection: this.connection,
      overview: success.overview,
      pipelines: success.pipelines,
      lastSuccessAt: success.at,
    });
  }

  private applyFailure(error: unknown, generation: number): void {
    if (!this.isCurrent(generation) || isAbort(error)) return;
    const message = error instanceof Error
      ? error.message
      : 'Lecture du control plane impossible';
    this.emit(this.success
      ? {
          status: 'degraded',
          connection: this.connection,
          overview: this.success.overview,
          pipelines: this.success.pipelines,
          lastSuccessAt: this.success.at,
          message,
        }
      : { status: 'failed', connection: this.connection, message });
  }

  private finishRequest(generation: number): void {
    if (!this.isCurrent(generation)) return;
    this.inFlight = false;
    if (
      this.retryAfterFailure
      && this.sseConnected
    ) {
      this.retryAfterFailure = false;
      this.refresh();
      return;
    }
    if (
      this.highestCursorSeen > this.appliedRevision
      && this.requestedCursorRevision < this.highestCursorSeen
    ) {
      this.refresh();
    }
  }

  private projectionReset(): void {
    if (this.disposed) return;
    this.etag = null;
    this.appliedRevision = 0;
    this.highestCursorSeen = 0;
    this.requestedCursorRevision = 0;
    this.retryAfterFailure = false;
    this.refresh();
  }

  private setConnectionState(connected: boolean): void {
    if (this.disposed) return;
    this.sseConnected = connected;
    this.connection = connected ? 'live' : (this.success ? 'reconnecting' : 'offline');
    if (!connected) this.retryAfterFailure = false;
    if (connected && this.inFlight && !this.success) this.retryAfterFailure = true;
    this.emitConnectionCurrent();
    if (connected && this.success) {
      // A new SSE connection may belong to a restarted server. Its revision
      // can be equal to or lower than ours; discard that process-local cache.
      this.projectionReset();
      return;
    }
    if (connected && !this.inFlight && (this.success || this.state.status === 'failed')) this.refresh();
  }

  private resetSession(): void {
    this.inFlight = false;
    this.appliedRevision = 0;
    this.highestCursorSeen = 0;
    this.requestedCursorRevision = 0;
    this.etag = null;
    this.connection = 'connecting';
    this.sseConnected = false;
    this.retryAfterFailure = false;
    this.success = null;
    this.state = { status: 'loading', connection: 'connecting' };
  }

  private isCurrent(generation: number): boolean {
    return !this.disposed && generation === this.generation;
  }

  private isCurrentSubscription(generation: number): boolean {
    return !this.disposed && generation === this.subscriptionGeneration;
  }

  private emit(state: ControlPlaneState): void {
    if (this.disposed) return;
    this.state = state;
    this.publish(state);
  }

  private emitConnectionCurrent(): void {
    switch (this.state.status) {
      case 'loading':
        this.emit({ status: 'loading', connection: this.connection });
        return;
      case 'failed':
        this.emit({ status: 'failed', connection: this.connection, message: this.state.message });
        return;
      case 'ready':
      case 'refreshing':
        this.emit({
          status: this.state.status,
          connection: this.connection,
          overview: this.state.overview,
          pipelines: this.state.pipelines,
          lastSuccessAt: this.state.lastSuccessAt,
        });
        return;
      case 'degraded':
        this.emit({
          status: 'degraded',
          connection: this.connection,
          overview: this.state.overview,
          pipelines: this.state.pipelines,
          lastSuccessAt: this.state.lastSuccessAt,
          message: this.state.message,
        });
    }
  }
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}
