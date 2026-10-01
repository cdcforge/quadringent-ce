import assert from 'node:assert/strict';
import test from 'node:test';

import type { Overview, Pipeline } from '../domain/controlPlane.ts';
import { ControlPlaneClient, type OverviewRequest, type OverviewResult } from './controlPlaneClient.ts';
import { ControlPlaneController, type ControlPlaneState, type ControlPlaneTransport } from './controlPlaneController.ts';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function pipeline(id: string): Pipeline {
  return { id } as Pipeline;
}

function updated(revision: number, id = `rev-${revision}`, etag = `"projection-${revision}"`): OverviewResult {
  return { kind: 'updated', overview: { revision, generatedAt: '2026-08-28T10:00:00+00:00', scope: { kind: 'single', environments: ['dev'] }, pipelines: [pipeline(id)], sources: [] }, etag };
}

async function settle(): Promise<void> {
  await new Promise<void>((resolve) => setTimeout(resolve, 0));
}

class FakeEventSource {
  closed = false;
  onerror: ((event: Event) => void) | null = null;
  onopen: ((event: Event) => void) | null = null;
  private readonly listeners = new Map<string, (event: MessageEvent<string>) => void>();

  addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void {
    this.listeners.set(type, listener);
  }

  close(): void {
    this.closed = true;
  }

  emit(type: string, revision: number): void {
    this.listeners.get(type)?.(new MessageEvent(type, { data: JSON.stringify({ revision }) }));
  }

  open(): void {
    this.onopen?.(new Event('open'));
  }

  error(): void {
    this.onerror?.(new Event('error'));
  }
}

function restPipeline(id: string): unknown {
  return {
    id,
    environment: 'dev',
    status: 'degraded',
    quality: { coverage: 'partial', freshness: 'fresh', evidence_kind: 'live' },
    summary: id,
    observed_at: '2026-08-28T09:59:00Z',
    stages: ['source', 'capture', 'raw', 'load', 'destination'].map((stageId) => ({
      id: stageId,
      status: stageId === 'load' || stageId === 'destination' ? 'unknown' : 'healthy',
      observed_at: '2026-08-28T09:59:00Z',
      headline: stageId,
      detail: stageId,
    })),
    lag_sequences: 0,
    lag_seconds: null,
    counters: {},
    incident: null,
  };
}

function restResponse(revision: number, id: string): Response {
  return new Response(JSON.stringify({ revision, generated_at: '2026-08-28T10:00:00+00:00', scope: { kind: 'single', environments: ['dev'] }, sources: [], pipelines: [restPipeline(id)] }), {
    status: 200,
    headers: { ETag: `"projection-${revision}"` },
  });
}

test('a stale rev10 response cannot replace rev41 or its ETag before a 304', async () => {
  const requests: Array<{
    readonly signal: AbortSignal | undefined;
    readonly etag: string | null;
    readonly response: ReturnType<typeof deferred<Response>>;
  }> = [];
  const stream = new FakeEventSource();
  const client = new ControlPlaneClient({
    eventSourceFactory: () => stream,
    fetchFn: async (_input, init) => {
      const pending = deferred<Response>();
      requests.push({
        signal: init?.signal as AbortSignal | undefined,
        etag: new Headers(init?.headers).get('If-None-Match'),
        response: pending,
      });
      return pending.promise;
    },
  });
  const controller = new ControlPlaneController(client, () => {});

  controller.start();
  controller.refresh();
  requests[1]!.response.resolve(restResponse(41, 'new'));
  await settle();
  requests[0]!.response.resolve(restResponse(10, 'old'));
  await settle();
  controller.refresh();

  assert.equal(requests[2]!.etag, '"projection-41"');
  requests[2]!.response.resolve(new Response(null, { status: 304 }));
  await settle();
  stream.emit('stream.cursor', 41);
  assert.equal(requests.length, 3);
  assert.equal(controller.state.status, 'ready');
  if (controller.state.status === 'ready') assert.equal(controller.state.pipelines[0]?.id, 'new');
});

test('SSE reconnect discards the previous process revision and ETag, including equal revisions', async () => {
  for (const oldRevision of [1, 41]) {
    let connection: (value: boolean) => void = () => {};
    const requests: OverviewRequest[] = [];
    const jobs = [deferred<OverviewResult>(), deferred<OverviewResult>()];
    const controller = new ControlPlaneController({
      fetchOverview(request) { requests.push(request); return jobs[requests.length - 1]!.promise; },
      subscribe(_revision, _reset, onConnection) { connection = onConnection!; return () => {}; },
    }, () => {});
    controller.start();
    jobs[0]!.resolve(updated(oldRevision, 'old-process'));
    await settle();
    connection(false);
    connection(true);
    assert.equal(requests[1]?.etag, null);
    jobs[1]!.resolve(updated(1, 'new-process'));
    await settle();
    assert.equal(controller.state.status, 'ready');
    if (controller.state.status === 'ready') assert.equal(controller.state.pipelines[0]?.id, 'new-process');
    controller.dispose();
  }
});

test('SSE reconnect aborts an old in-flight GET before accepting the new process snapshot', async () => {
  let connection: (value: boolean) => void = () => {};
  const requests: OverviewRequest[] = [];
  const jobs = [deferred<OverviewResult>(), deferred<OverviewResult>(), deferred<OverviewResult>()];
  const controller = new ControlPlaneController({
    fetchOverview(request) { requests.push(request); return jobs[requests.length - 1]!.promise; },
    subscribe(_revision, _reset, onConnection) { connection = onConnection!; return () => {}; },
  }, () => {});
  controller.start();
  jobs[0]!.resolve(updated(41, 'old-process'));
  await settle();
  controller.refresh();
  connection(false);
  connection(true);
  assert.equal(requests.length, 3);
  assert.equal(requests[1]?.signal?.aborted, true);
  assert.equal(requests[2]?.etag, null);
  jobs[2]!.resolve(updated(1, 'new-process'));
  await settle();
  jobs[1]!.resolve(updated(42, 'obsolete-response'));
  await settle();
  if (controller.state.status === 'ready') assert.equal(controller.state.pipelines[0]?.id, 'new-process');
  else assert.fail('new process snapshot was not accepted');
  controller.dispose();
});

test('a fetch implementation that ignores abort cannot publish or cache its stale response', async () => {
  const jobs = [
    deferred<OverviewResult>(),
    deferred<OverviewResult>(),
    deferred<OverviewResult>(),
  ];
  const requests: OverviewRequest[] = [];
  const states: ControlPlaneState[] = [];
  let calls = 0;
  const controller = new ControlPlaneController({
    fetchOverview(request) {
      requests.push(request);
      return jobs[calls++]!.promise;
    },
    subscribe() { return () => {}; },
  }, (state) => states.push(state));

  controller.start();
  controller.refresh();
  assert.equal(requests[0]!.signal?.aborted, true);
  jobs[1]!.resolve(updated(41, 'new'));
  await settle();
  const publishedAfterNew = states.length;
  jobs[0]!.resolve(updated(10, 'old'));
  await settle();

  assert.equal(states.length, publishedAfterNew);
  assert.equal(controller.state.status, 'ready');
  if (controller.state.status === 'ready') assert.equal(controller.state.pipelines[0]?.id, 'new');
  if (controller.state.status === 'ready') assert.equal(controller.state.overview.revision, 41);
  controller.refresh();
  assert.equal(requests[2]!.etag, '"projection-41"');
});

test('cursor 41 received during a GET that returns rev41 does not start a second GET', async () => {
  const job = deferred<OverviewResult>();
  let onRevision: (revision: number) => void = () => {};
  let calls = 0;
  const controller = new ControlPlaneController({
    fetchOverview() {
      calls += 1;
      return job.promise;
    },
    subscribe(revision) {
      onRevision = revision;
      return () => {};
    },
  }, () => {});

  controller.start();
  onRevision(41);
  job.resolve(updated(41));
  await settle();

  assert.equal(calls, 1);
});

test('a 42/43/44 cursor burst starts one follow-up and cursor 44 then stays quiet', async () => {
  const jobs = [deferred<OverviewResult>(), deferred<OverviewResult>()];
  let onRevision: (revision: number) => void = () => {};
  let calls = 0;
  const controller = new ControlPlaneController({
    fetchOverview() {
      return jobs[calls++]!.promise;
    },
    subscribe(revision) {
      onRevision = revision;
      return () => {};
    },
  }, () => {});

  controller.start();
  onRevision(42);
  onRevision(43);
  onRevision(44);
  jobs[0]!.resolve(updated(42));
  await settle();
  assert.equal(calls, 2);
  jobs[1]!.resolve(updated(44));
  await settle();
  onRevision(44);

  assert.equal(calls, 2);
  assert.equal(controller.state.status, 'ready');
  if (controller.state.status === 'ready') assert.equal(controller.state.pipelines[0]?.id, 'rev-44');
});

test('one cursor burst cannot create a retry storm when its follow-up stays behind', async () => {
  const jobs = [deferred<OverviewResult>(), deferred<OverviewResult>()];
  let onRevision: (revision: number) => void = () => {};
  let calls = 0;
  const controller = new ControlPlaneController({
    fetchOverview() {
      return jobs[calls++]!.promise;
    },
    subscribe(revision) {
      onRevision = revision;
      return () => {};
    },
  }, () => {});

  controller.start();
  onRevision(44);
  jobs[0]!.resolve(updated(42));
  await settle();
  jobs[1]!.resolve(updated(42));
  await settle();

  assert.equal(calls, 2);
});

test('late resolve and every late SSE callback after dispose are inert', async () => {
  const jobs = [deferred<OverviewResult>(), deferred<OverviewResult>()];
  const requests: OverviewRequest[] = [];
  let revision: (value: number) => void = () => {};
  let reset: () => void = () => {};
  let connection: (connected: boolean) => void = () => {};
  let calls = 0;
  const states: ControlPlaneState[] = [];
  const controller = new ControlPlaneController({
    fetchOverview(request) {
      requests.push(request);
      calls += 1;
      return jobs[calls - 1]!.promise;
    },
    subscribe(onRevision, onReset, onConnection) {
      revision = onRevision;
      reset = onReset;
      connection = onConnection ?? (() => {});
      return () => {};
    },
  }, (state) => states.push(state));

  controller.start();
  controller.dispose();
  const publications = states.length;
  revision(99);
  reset();
  connection(true);
  connection(false);
  jobs[0]!.resolve(updated(99, 'late'));
  await settle();

  assert.equal(calls, 1);
  assert.equal(states.length, publications);
  assert.equal(controller.state.status, 'loading');
  controller.start();
  assert.equal(calls, 2);
  assert.equal(requests[1]!.etag, null);
});

test('every callback from an old SSE subscription stays inert after the same controller restarts', async () => {
  const jobs = [
    deferred<OverviewResult>(),
    deferred<OverviewResult>(),
    deferred<OverviewResult>(),
    deferred<OverviewResult>(),
    deferred<OverviewResult>(),
    deferred<OverviewResult>(),
  ];
  const requests: OverviewRequest[] = [];
  const subscriptions: Array<{
    revision: (value: number) => void;
    reset: () => void;
    connection: (connected: boolean) => void;
  }> = [];
  const states: ControlPlaneState[] = [];
  let calls = 0;
  const controller = new ControlPlaneController({
    fetchOverview(request) {
      requests.push(request);
      return jobs[calls++]!.promise;
    },
    subscribe(revision, reset, connection) {
      subscriptions.push({
        revision,
        reset,
        connection: connection ?? (() => {}),
      });
      return () => {};
    },
  }, (state) => states.push(state));

  controller.start();
  jobs[0]!.resolve(updated(41, 'first'));
  await settle();
  controller.dispose();
  controller.start();
  jobs[1]!.resolve(updated(52, 'second'));
  await settle();
  const stableState = controller.state;
  const publications = states.length;

  subscriptions[0]!.revision(99);
  subscriptions[0]!.reset();
  subscriptions[0]!.connection(true);
  subscriptions[0]!.connection(false);

  assert.equal(calls, 2);
  assert.equal(states.length, publications);
  assert.equal(controller.state, stableState);

  controller.refresh();
  assert.equal(requests[2]!.etag, '"projection-52"');
  jobs[2]!.resolve({ kind: 'not-modified' });
  await settle();
  assert.equal(calls, 3);
  assert.equal(controller.state.status, 'ready');
  if (controller.state.status === 'ready') assert.equal(controller.state.pipelines[0]?.id, 'second');
});

test('a late rejection after dispose neither publishes nor starts another GET', async () => {
  const job = deferred<OverviewResult>();
  let calls = 0;
  const states: ControlPlaneState[] = [];
  const controller = new ControlPlaneController({
    fetchOverview() {
      calls += 1;
      return job.promise;
    },
    subscribe() { return () => {}; },
  }, (state) => states.push(state));

  controller.start();
  controller.dispose();
  const publications = states.length;
  job.reject(new Error('late failure'));
  await settle();

  assert.equal(calls, 1);
  assert.equal(states.length, publications);
});

test('a stalled overview is bounded, aborted, and cannot overwrite recovery with a late result', async () => {
  const jobs = [deferred<OverviewResult>(), deferred<OverviewResult>(), deferred<OverviewResult>()];
  const signals: AbortSignal[] = [];
  const controller = new ControlPlaneController({
    fetchOverview({ signal }) { signals.push(signal!); return jobs[signals.length - 1]!.promise; },
    subscribe() { return () => {}; },
  }, () => {}, () => new Date('2026-09-09T15:00:00Z'), 20);
  try {
    controller.start();
    jobs[0]!.resolve(updated(1));
    await settle();
    controller.refresh();
    await new Promise<void>((resolve) => setTimeout(resolve, 40));
    assert.equal(controller.state.status, 'degraded');
    assert.equal(signals[1]!.aborted, true);
    if (controller.state.status === 'degraded') {
      assert.equal(controller.state.overview.revision, 1);
      assert.match(controller.state.message, /délai/i);
    }
    controller.refresh();
    jobs[2]!.resolve(updated(2));
    await settle();
    jobs[1]!.resolve(updated(999));
    await settle();
    assert.equal(controller.state.status, 'ready');
    if (controller.state.status === 'ready') assert.equal(controller.state.overview.revision, 2);
  } finally { controller.dispose(); }
});

test('a stalled initial overview leaves loading without inventing cached evidence', async () => {
  const controller = new ControlPlaneController({
    fetchOverview() { return new Promise<OverviewResult>(() => {}); },
    subscribe() { return () => {}; },
  }, () => {}, () => new Date(), 10);
  try {
    controller.start();
    await new Promise<void>((resolve) => setTimeout(resolve, 30));
    assert.equal(controller.state.status, 'failed');
    assert.equal('overview' in controller.state, false);
  } finally { controller.dispose(); }
});

test('initial failure enters failed state', async () => {
  const controller = new ControlPlaneController({
    fetchOverview() { return Promise.reject(new Error('offline')); },
    subscribe() { return () => {}; },
  }, () => {});

  controller.start();
  await settle();

  assert.deepEqual(controller.state, { status: 'failed', connection: 'connecting', message: 'offline' });
});

test('a failed GET can recover on SSE open without a positive revision', async () => {
  const jobs = [deferred<OverviewResult>(), deferred<OverviewResult>()];
  let connection: (connected: boolean) => void = () => {};
  let calls = 0;
  const controller = new ControlPlaneController({
    fetchOverview() { return jobs[calls++]!.promise; },
    subscribe(_revision, _reset, onConnection) {
      connection = onConnection ?? (() => {});
      return () => {};
    },
  }, () => {});

  controller.start();
  jobs[0]!.reject(new Error('offline'));
  await settle();
  assert.equal(controller.state.status, 'failed');

  connection(true);
  assert.equal(calls, 2);
  jobs[1]!.resolve(updated(41, 'stable'));
  await settle();

  assert.equal(controller.state.status, 'ready');
  assert.equal(controller.state.connection, 'live');
});

test('an SSE open during the first inflight GET grants exactly one retry after the reject', async () => {
  const jobs = [deferred<OverviewResult>(), deferred<OverviewResult>()];
  let connection: (connected: boolean) => void = () => {};
  let calls = 0;
  const controller = new ControlPlaneController({
    fetchOverview() { return jobs[calls++]!.promise; },
    subscribe(_revision, _reset, onConnection) {
      connection = onConnection ?? (() => {});
      return () => {};
    },
  }, () => {});

  controller.start();
  connection(true);
  jobs[0]!.reject(new Error('offline'));
  await settle();

  assert.equal(calls, 2);
  jobs[1]!.resolve(updated(41, 'stable'));
  await settle();

  assert.equal(calls, 2);
  assert.equal(controller.state.status, 'ready');
  assert.equal(controller.state.connection, 'live');
});

test('an SSE open during the first inflight GET does not loop after two rejects', async () => {
  const jobs = [
    deferred<OverviewResult>(),
    deferred<OverviewResult>(),
  ];
  let connection: (connected: boolean) => void = () => {};
  let calls = 0;
  const controller = new ControlPlaneController({
    fetchOverview() { return jobs[calls++]!.promise; },
    subscribe(_revision, _reset, onConnection) {
      connection = onConnection ?? (() => {});
      return () => {};
    },
  }, () => {});

  controller.start();
  connection(true);
  jobs[0]!.reject(new Error('offline'));
  await settle();
  jobs[1]!.reject(new Error('still offline'));
  await settle();

  assert.equal(calls, 2);
  assert.equal(controller.state.status, 'failed');
  assert.equal(controller.state.connection, 'live');
});

test('an SSE close before the first inflight reject consumes the retry right', async () => {
  const job = deferred<OverviewResult>();
  let connection: (connected: boolean) => void = () => {};
  let calls = 0;
  const controller = new ControlPlaneController({
    fetchOverview() {
      calls += 1;
      return job.promise;
    },
    subscribe(_revision, _reset, onConnection) {
      connection = onConnection ?? (() => {});
      return () => {};
    },
  }, () => {});

  controller.start();
  connection(true);
  connection(false);
  job.reject(new Error('offline'));
  await settle();

  assert.equal(calls, 1);
  assert.equal(controller.state.status, 'failed');
  assert.equal(controller.state.connection, 'offline');
});

test('a GET success without SSE never produces a connected shell state', async () => {
  const job = deferred<OverviewResult>();
  const controller = new ControlPlaneController({
    fetchOverview() { return job.promise; },
    subscribe() { return () => {}; },
  }, () => {});

  controller.start();
  job.resolve(updated(41, 'stable'));
  await settle();

  assert.equal(controller.state.status, 'ready');
  assert.equal(controller.state.connection, 'connecting');
});

test('an SSE error before the first GET is memorized', async () => {
  const job = deferred<OverviewResult>();
  let connection: (connected: boolean) => void = () => {};
  const controller = new ControlPlaneController({
    fetchOverview() { return job.promise; },
    subscribe(_revision, _reset, onConnection) {
      connection = onConnection ?? (() => {});
      return () => {};
    },
  }, () => {});

  controller.start();
  connection(false);
  assert.equal(controller.state.connection, 'offline');
  job.resolve(updated(41, 'stable'));
  await settle();

  assert.equal(controller.state.status, 'ready');
  assert.equal(controller.state.connection, 'offline');
});

test('an SSE error after live keeps cached data separate from connection state', async () => {
  const job = deferred<OverviewResult>();
  let connection: (connected: boolean) => void = () => {};
  const controller = new ControlPlaneController({
    fetchOverview() { return job.promise; },
    subscribe(_revision, _reset, onConnection) {
      connection = onConnection ?? (() => {});
      return () => {};
    },
  }, () => {});

  controller.start();
  connection(true);
  job.resolve(updated(41, 'stable'));
  await settle();
  connection(false);

  assert.equal(controller.state.status, 'ready');
  assert.equal(controller.state.connection, 'reconnecting');
});

test('refreshing, degraded and 304 recovery preserve data and lastSuccessAt', async () => {
  const firstAt = new Date('2026-08-28T10:00:00Z');
  const jobs = [
    deferred<OverviewResult>(),
    deferred<OverviewResult>(),
    deferred<OverviewResult>(),
  ];
  let calls = 0;
  const controller = new ControlPlaneController({
    fetchOverview() { return jobs[calls++]!.promise; },
    subscribe() { return () => {}; },
  }, () => {}, () => firstAt);

  controller.start();
  jobs[0]!.resolve(updated(41, 'stable'));
  await settle();
  controller.refresh();
  assert.equal(controller.state.status, 'refreshing');
  if (controller.state.status === 'refreshing') {
    assert.equal(controller.state.pipelines[0]?.id, 'stable');
    assert.equal(controller.state.lastSuccessAt, firstAt);
  }
  jobs[1]!.reject(new Error('temporary'));
  await settle();
  assert.equal(controller.state.status, 'degraded');
  if (controller.state.status === 'degraded') {
    assert.equal(controller.state.pipelines[0]?.id, 'stable');
    assert.equal(controller.state.lastSuccessAt, firstAt);
  }
  controller.refresh();
  jobs[2]!.resolve({ kind: 'not-modified' });
  await settle();

  assert.equal(controller.state.status, 'ready');
  if (controller.state.status === 'ready') {
    assert.equal(controller.state.pipelines[0]?.id, 'stable');
    assert.equal(controller.state.lastSuccessAt, firstAt);
  }
});

test('an active SSE error keeps the cached data and shifts only the connection state', async () => {
  const firstAt = new Date('2026-08-28T10:00:00Z');
  const jobs = [deferred<OverviewResult>(), deferred<OverviewResult>()];
  let connection: (connected: boolean) => void = () => {};
  let calls = 0;
  const controller = new ControlPlaneController({
    fetchOverview() { return jobs[calls++]!.promise; },
    subscribe(_revision, _reset, onConnection) {
      connection = onConnection ?? (() => {});
      return () => {};
    },
  }, () => {}, () => firstAt);

  controller.start();
  jobs[0]!.resolve(updated(41, 'stable'));
  await settle();
  connection(false);
  assert.equal(controller.state.status, 'ready');
  assert.equal(controller.state.connection, 'reconnecting');
  if (controller.state.status === 'ready') {
    assert.equal(controller.state.pipelines[0]?.id, 'stable');
    assert.equal(controller.state.lastSuccessAt, firstAt);
  }
  connection(true);
  jobs[1]!.resolve({ kind: 'not-modified' });
  await settle();

  assert.equal(controller.state.status, 'ready');
  if (controller.state.status === 'ready') {
    assert.equal(controller.state.pipelines[0]?.id, 'stable');
    assert.equal(controller.state.lastSuccessAt, firstAt);
  }
});

test('an active projection reset clears only its session ETag before reloading', async () => {
  const jobs = [deferred<OverviewResult>(), deferred<OverviewResult>()];
  const requests: OverviewRequest[] = [];
  let reset: () => void = () => {};
  let calls = 0;
  const controller = new ControlPlaneController({
    fetchOverview(request) {
      requests.push(request);
      return jobs[calls++]!.promise;
    },
    subscribe(_revision, onReset) {
      reset = onReset;
      return () => {};
    },
  }, () => {});

  controller.start();
  jobs[0]!.resolve(updated(41));
  await settle();
  reset();

  assert.equal(requests[1]!.etag, null);
  jobs[1]!.resolve(updated(42));
  await settle();
  assert.equal(controller.state.status, 'ready');
});

test('dispose/start with a shared client leaves the second session cache intact', async () => {
  const firstStream = new FakeEventSource();
  const secondStream = new FakeEventSource();
  const streams = [firstStream, secondStream];
  const headers: Array<string | null> = [];
  const jobs: Array<ReturnType<typeof deferred<Response>>> = [];
  const client = new ControlPlaneClient({
    eventSourceFactory: () => streams.shift()!,
    fetchFn: async (_input, init) => {
      const job = deferred<Response>();
      jobs.push(job);
      headers.push(new Headers(init?.headers).get('If-None-Match'));
      return job.promise;
    },
  });
  const first = new ControlPlaneController(client, () => {});
  const second = new ControlPlaneController(client, () => {});

  first.start();
  jobs[0]!.resolve(restResponse(10, 'first'));
  await settle();
  first.dispose();
  second.start();
  jobs[1]!.resolve(restResponse(41, 'second'));
  await settle();

  // The disposed session's callbacks remain callable in real EventSource implementations.
  firstStream.emit('projection.reset', 99);
  second.refresh();

  assert.deepEqual(headers, [null, null, '"projection-41"']);
  assert.equal(second.state.status, 'refreshing');
  if (second.state.status === 'refreshing') assert.equal(second.state.pipelines[0]?.id, 'second');
});

// Compile-time assertion: controller fakes and the production client share the same session contract.
const _transport: ControlPlaneTransport = new ControlPlaneClient();
void _transport;
