import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ControlPlaneActionError,
  ControlPlaneClient,
  type ActionId,
  type OverviewResult,
  type PipelineActionReceipt,
  type PipelineActionRequest,
  type PipelineListResult,
  type PipelineResult,
} from './controlPlaneClient.ts';
import { ControlPlaneParseError } from '../domain/controlPlane.ts';
import { installTestSite, TEST_SITE, TEST_SITE_WIRE } from '../domain/siteFixture.ts';

installTestSite();

export function validPipeline(id = 'dev-cntr'): unknown {
  return {
    id,
    environment: 'dev',
    status: 'degraded',
    quality: { coverage: 'partial', freshness: 'fresh', evidence_kind: 'live' },
    summary: 'Capture saine, livraison Snowflake non prouvée',
    observed_at: '2026-08-28T09:59:00+00:00',
    stages: ['source', 'capture', 'raw', 'load', 'destination'].map((stageId) => ({
      id: stageId,
      status: stageId === 'load' || stageId === 'destination' ? 'unknown' : 'healthy',
      observed_at: '2026-08-28T09:59:00Z',
      headline: stageId,
      detail: stageId,
    })),
    lag_sequences: 3,
    lag_seconds: null,
    counters: { events_published: 12 },
    incident: null,
  };
}

export class FakeEventSource {
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

  emit(type: string, data: string): void {
    this.listeners.get(type)?.(new MessageEvent(type, { data }));
  }
}

export function response(value: unknown, revision = 7, etag = `"projection-${revision}"`): Response {
  return new Response(JSON.stringify({ revision, pipelines: [value] }), {
    status: 200,
    headers: { ETag: etag },
  });
}

function validOverview(): unknown {
  return {
    revision: 7,
    generated_at: '2026-08-28T10:00:00+00:00',
    scope: { kind: 'single', environments: ['dev'] },
    sources: [{ id: 'dev-cntr', environment: 'dev', evidence_kind: 'live', status: 'available', error: null }],
    pipelines: [validPipeline()],
  };
}

test('fetchPipelineList returns pipelines, revision and ETag as one atomic result', async () => {
  const headers: Array<string | null> = [];
  const client = new ControlPlaneClient({
    fetchFn: async (_input, init) => {
      headers.push(new Headers(init?.headers).get('If-None-Match'));
      return response(validPipeline(), 41, '"projection-41"');
    },
  });

  const result = await client.fetchPipelineList({ etag: '"projection-10"' });

  assert.deepEqual(headers, ['"projection-10"']);
  assert.equal(result.kind, 'updated');
  if (result.kind === 'updated') {
    assert.equal(result.pipelines[0]?.id, 'dev-cntr');
    assert.equal(result.revision, 41);
    assert.equal(result.etag, '"projection-41"');
  }
});

test('fetchPipelineList preserves a 304 as an explicit not-modified result', async () => {
  const client = new ControlPlaneClient({ fetchFn: async () => new Response(null, { status: 304 }) });

  assert.deepEqual(
    await client.fetchPipelineList({ etag: '"projection-41"' }),
    { kind: 'not-modified' },
  );
});

test('fetchOverview returns the real overview, its revision and ETag atomically', async () => {
  const requests: Array<{ readonly input: string; readonly etag: string | null }> = [];
  const client = new ControlPlaneClient({
    fetchFn: async (input, init) => {
      requests.push({ input, etag: new Headers(init?.headers).get('If-None-Match') });
      return new Response(JSON.stringify(validOverview()), { status: 200, headers: { ETag: '"overview-7"' } });
    },
  });

  const result = await client.fetchOverview({ etag: '"overview-6"' });

  assert.deepEqual(requests, [{ input: '/v1/overview', etag: '"overview-6"' }]);
  assert.equal(result.kind, 'updated');
  if (result.kind === 'updated') {
    assert.equal(result.overview.revision, 7);
    assert.equal(result.overview.generatedAt, '2026-08-28T10:00:00+00:00');
    assert.equal(result.etag, '"overview-7"');
  }
});

test('getOverview returns the parsed overview without a session cache', async () => {
  const client = new ControlPlaneClient({ fetchFn: async () => new Response(JSON.stringify(validOverview())) });
  const overview = await client.getOverview();
  assert.equal(overview.generatedAt, '2026-08-28T10:00:00+00:00');
});

const _atomicOverviewResult: OverviewResult | null = null;
const _runPipelineActionContract: (
  pipelineId: string,
  action: ActionId,
  payload: PipelineActionRequest,
  signal?: AbortSignal,
) => Promise<PipelineActionReceipt> = new ControlPlaneClient().runPipelineAction.bind(new ControlPlaneClient());

test('the shared client never reuses an ETag unless its caller supplies it', async () => {
  const headers: Array<string | null> = [];
  const client = new ControlPlaneClient({
    fetchFn: async (_input, init) => {
      headers.push(new Headers(init?.headers).get('If-None-Match'));
      return response(validPipeline());
    },
  });

  await client.fetchPipelineList({});
  await client.fetchPipelineList({});

  assert.deepEqual(headers, [null, null]);
});

test('legacy listPipelines accepts an AbortSignal and returns Pipeline[]', async () => {
  const abortController = new AbortController();
  let receivedSignal: AbortSignal | null | undefined;
  const client = new ControlPlaneClient({
    fetchFn: async (_input, init) => {
      receivedSignal = init?.signal;
      return response(validPipeline('legacy'));
    },
  });

  const pipelines: Awaited<ReturnType<typeof client.listPipelines>> = await client.listPipelines(abortController.signal);

  assert.equal(receivedSignal, abortController.signal);
  assert.deepEqual(pipelines.map(({ id }) => id), ['legacy']);
});

test('legacy listPipelines rejects an unexpected 304 without a caller cache', async () => {
  const client = new ControlPlaneClient({ fetchFn: async () => new Response(null, { status: 304 }) });

  await assert.rejects(
    client.listPipelines(),
    /Réponse 304 sans cache de session/,
  );
});

test('fetchPipeline sends encoded id, signal and ETag then returns one atomic result', async () => {
  const abortController = new AbortController();
  let requestUrl = '';
  let receivedSignal: AbortSignal | null | undefined;
  let receivedEtag: string | null = null;
  const client = new ControlPlaneClient({
    fetchFn: async (input, init) => {
      requestUrl = input;
      receivedSignal = init?.signal;
      receivedEtag = new Headers(init?.headers).get('If-None-Match');
      return new Response(JSON.stringify({ revision: 52, pipeline: validPipeline('dev pays/42') }), {
        status: 200,
        headers: { ETag: '"projection-52"' },
      });
    },
  });

  const result = await client.fetchPipeline({
    id: 'dev pays/42',
    signal: abortController.signal,
    etag: '"projection-41"',
  });

  assert.equal(requestUrl, '/v1/pipelines/dev%20pays%2F42');
  assert.equal(receivedSignal, abortController.signal);
  assert.equal(receivedEtag, '"projection-41"');
  assert.equal(result.kind, 'updated');
  if (result.kind === 'updated') {
    assert.equal(result.pipeline.id, 'dev pays/42');
    assert.equal(result.revision, 52);
    assert.equal(result.etag, '"projection-52"');
  }
});

test('fetchPipeline represents 304 explicitly and legacy getPipeline rejects it', async () => {
  const client = new ControlPlaneClient({ fetchFn: async () => new Response(null, { status: 304 }) });

  assert.deepEqual(
    await client.fetchPipeline({ id: 'dev-cntr', etag: '"projection-52"' }),
    { kind: 'not-modified' },
  );
  await assert.rejects(
    client.getPipeline('dev-cntr'),
    /Réponse 304 sans cache de session/,
  );
});

test('legacy getPipeline keeps its id and AbortSignal contract', async () => {
  const abortController = new AbortController();
  let requestUrl = '';
  let receivedSignal: AbortSignal | null | undefined;
  const client = new ControlPlaneClient({
    fetchFn: async (input, init) => {
      requestUrl = input;
      receivedSignal = init?.signal;
      return new Response(JSON.stringify({ revision: 53, pipeline: validPipeline('legacy/detail') }), {
        status: 200,
      });
    },
  });

  const pipeline = await client.getPipeline('legacy/detail', abortController.signal);

  assert.equal(requestUrl, '/v1/pipelines/legacy%2Fdetail');
  assert.equal(receivedSignal, abortController.signal);
  assert.equal(pipeline.id, 'legacy/detail');
});

test('projection events are forwarded and unsubscribe closes EventSource', () => {
  const stream = new FakeEventSource();
  const revisions: number[] = [];
  let resets = 0;
  const client = new ControlPlaneClient({ eventSourceFactory: () => stream });
  const unsubscribe = client.subscribe(
    (revision) => revisions.push(revision),
    () => { resets += 1; },
  );

  stream.emit('projection.updated', '{"revision":8}');
  stream.emit('projection.reset', '{"revision":9}');
  unsubscribe();

  assert.deepEqual(revisions, [8]);
  assert.equal(resets, 1);
  assert.equal(stream.closed, true);
});

test('an SSE error closes the stalled transport and recreates the subscription', async () => {
  const streams: FakeEventSource[] = [];
  const connections: boolean[] = [];
  const revisions: number[] = [];
  const client = new ControlPlaneClient({
    reconnectDelayMs: 0,
    eventSourceFactory: () => {
      const stream = new FakeEventSource();
      streams.push(stream);
      return stream;
    },
  });
  const unsubscribe = client.subscribe((revision) => revisions.push(revision), () => {}, (connected) => connections.push(connected));

  streams[0]!.onopen?.(new Event('open'));
  streams[0]!.onerror?.(new Event('error'));
  assert.equal(streams[0]!.closed, true);
  streams[0]!.emit('projection.updated', '{"revision":8}');
  assert.deepEqual(revisions, []);
  await new Promise<void>((resolve) => setTimeout(resolve, 0));

  assert.equal(streams.length, 2);
  streams[1]!.onopen?.(new Event('open'));
  assert.deepEqual(connections, [true, false, true]);

  unsubscribe();
  assert.equal(streams[1]!.closed, true);
});

test('unsubscribe cancels a pending SSE reconnect', async () => {
  const streams: FakeEventSource[] = [];
  const client = new ControlPlaneClient({
    reconnectDelayMs: 10,
    eventSourceFactory: () => {
      const stream = new FakeEventSource();
      streams.push(stream);
      return stream;
    },
  });
  const unsubscribe = client.subscribe(() => {}, () => {});

  streams[0]!.onerror?.(new Event('error'));
  unsubscribe();
  await new Promise<void>((resolve) => setTimeout(resolve, 20));

  assert.equal(streams.length, 1);
});

test('browser network return replaces a silent SSE without claiming connectivity before open', () => {
  const networkEvents = new EventTarget();
  const streams: FakeEventSource[] = [];
  const connections: boolean[] = [];
  const revisions: number[] = [];
  const client = new ControlPlaneClient({ networkEvents, eventSourceFactory: () => {
    const stream = new FakeEventSource(); streams.push(stream); return stream;
  } });
  const stop = client.subscribe((revision) => revisions.push(revision), () => {}, (live) => connections.push(live));
  try {
    streams[0]!.onopen?.(new Event('open'));
    networkEvents.dispatchEvent(new Event('offline'));
    assert.equal(streams[0]!.closed, true);
    assert.deepEqual(connections, [true, false]);
    streams[0]!.emit('stream.cursor', '{"revision":99}');
    assert.deepEqual(revisions, []);
    networkEvents.dispatchEvent(new Event('online'));
    assert.equal(streams.length, 2);
    assert.deepEqual(connections, [true, false], 'Online is not server connectivity evidence');
    networkEvents.dispatchEvent(new Event('online'));
    assert.equal(streams.length, 2, 'Repeated online must not duplicate subscriptions');
    streams[1]!.onopen?.(new Event('open'));
    assert.deepEqual(connections, [true, false, true]);
  } finally { stop(); }
  networkEvents.dispatchEvent(new Event('offline'));
  networkEvents.dispatchEvent(new Event('online'));
  assert.equal(streams.length, 2, 'Disposed subscription must not restart');
  assert.deepEqual(connections, [true, false, true]);
});

test('offline cancels pending SSE retry until online', async () => {
  const networkEvents = new EventTarget();
  const streams: FakeEventSource[] = [];
  const client = new ControlPlaneClient({ networkEvents, reconnectDelayMs: 0, eventSourceFactory: () => {
    const stream = new FakeEventSource(); streams.push(stream); return stream;
  } });
  const stop = client.subscribe(() => {}, () => {});
  try {
    streams[0]!.onerror?.(new Event('error'));
    networkEvents.dispatchEvent(new Event('offline'));
    await new Promise<void>((resolve) => setTimeout(resolve, 10));
    assert.equal(streams.length, 1);
    networkEvents.dispatchEvent(new Event('online'));
    assert.equal(streams.length, 2);
  } finally { stop(); }
});

test('invalid SSE payloads never trigger a reload', () => {
  const stream = new FakeEventSource();
  let revisions = 0;
  const client = new ControlPlaneClient({ eventSourceFactory: () => stream });
  client.subscribe(() => { revisions += 1; }, () => { revisions += 1; });

  stream.emit('projection.updated', '{"revision":"new"}');

  assert.equal(revisions, 0);
});

test('evaluateOnboarding posts the spec to the shipped control plane path', async () => {
  const requests: Array<{ readonly input: string; readonly method: string | undefined; readonly body: string }> = [];
  const client = new ControlPlaneClient({
    fetchFn: async (input, init) => {
      requests.push({ input, method: init?.method, body: String(init?.body ?? '') });
      return new Response(JSON.stringify({
        step: 'source',
        status: 'ok',
        errors: [],
        blocked: [],
        unproven: [],
        proven: [],
        next_action: 'Vérifier les permissions et la référence du secret',
        risk: 'low',
      }));
    },
  });

  const verdict = await client.evaluateOnboarding({ step: 'source', ibmi_host: TEST_SITE.ibmiHost, tls: true });
  assert.deepEqual(requests[0]?.input, '/v1/onboarding/evaluate');
  assert.equal(requests[0]?.method, 'POST');
  assert.equal(JSON.parse(requests[0]!.body).ibmi_host, TEST_SITE.ibmiHost);
  assert.equal((verdict as { status: string }).status, 'ok');
});

test('evaluateOnboarding surfaces an unavailable API instead of a fake success', async () => {
  const client = new ControlPlaneClient({
    fetchFn: async () => new Response('', { status: 503 }),
  });
  await assert.rejects(() => client.evaluateOnboarding({ step: 'source' }), /indisponible/);
});

function fleetPayload(confirmation: string | null = null): PipelineActionRequest {
  return {
    fleet_id: TEST_SITE.fleetId,
    environment: TEST_SITE.runtimeEnvironment,
    confirmation,
  };
}

function validReceipt(
  action: ActionId = 'start',
  state: PipelineActionReceipt['state'] = 'succeeded',
  unavailableCode: 'capability_unavailable' | 'executor_unavailable' = 'executor_unavailable',
): unknown {
  const stages = {
    succeeded: {
      intent: { state: 'recorded', code: 'intent_recorded', message: 'Intent recorded' },
      execution: { state: 'completed', code: 'execution_completed', message: 'Execution completed' },
      observed_effect: { state: 'succeeded', code: 'effect_observed', message: 'Effect observed' },
    },
    failed: {
      intent: { state: 'recorded', code: 'intent_recorded', message: 'Intent recorded' },
      execution: { state: 'failed', code: 'execution_failed', message: 'Execution failed' },
      observed_effect: { state: 'failed', code: 'effect_failed', message: 'Effect failed' },
    },
    conflict: {
      intent: { state: 'rejected', code: 'action_in_progress', message: 'Action already running' },
      execution: { state: 'not_started', code: 'executor_not_started', message: 'Execution not started' },
      observed_effect: { state: 'unknown', code: 'effect_unknown', message: 'Effect unknown' },
    },
    unavailable: {
      intent: {
        state: 'rejected',
        code: unavailableCode,
        message: unavailableCode === 'executor_unavailable' ? 'Executor unavailable' : 'Capability unavailable',
      },
      execution: { state: 'not_started', code: 'executor_not_started', message: 'Execution not started' },
      observed_effect: { state: 'unknown', code: 'effect_unknown', message: 'Effect unknown' },
    },
  } as const;
  return {
    id: 'act_test_receipt_01',
    action,
    fleet_id: TEST_SITE.fleetId,
    environment: TEST_SITE.runtimeEnvironment,
    created_at: '2026-09-13T10:00:00+00:00',
    state,
    stages: stages[state],
  };
}

test('runPipelineAction posts the encoded URL, method, body and signal', async () => {
  const abortController = new AbortController();
  const requests: Array<{
    readonly input: string;
    readonly method: string | undefined;
    readonly body: string;
    readonly signal: AbortSignal | null | undefined;
  }> = [];
  const client = new ControlPlaneClient({
    fetchFn: async (input, init) => {
      requests.push({
        input,
        method: init?.method,
        body: String(init?.body ?? ''),
        signal: init?.signal,
      });
      return new Response(JSON.stringify(validReceipt('start')), { status: 200 });
    },
  });
  const payload = fleetPayload(`START ${TEST_SITE.siteId.toUpperCase()} ${TEST_SITE.environment}`);

  const receipt = await client.runPipelineAction('dev pays/42', 'start', payload, abortController.signal);

  assert.equal(requests[0]?.input, '/v1/pipelines/dev%20pays%2F42/actions/start');
  assert.equal(requests[0]?.method, 'POST');
  assert.deepEqual(JSON.parse(requests[0]!.body), payload);
  assert.equal(requests[0]?.signal, abortController.signal);
  assert.equal(receipt.action, 'start');
  assert.equal(receipt.fleetId, TEST_SITE.fleetId);
  assert.equal(receipt.environment, TEST_SITE.runtimeEnvironment);
  assert.equal(receipt.state, 'succeeded');
  assert.equal(receipt.stages.observedEffect.state, 'succeeded');
});

test('runPipelineAction parses a successful receipt strictly', async () => {
  const client = new ControlPlaneClient({
    fetchFn: async () => new Response(JSON.stringify(validReceipt('refresh')), { status: 200 }),
  });
  const receipt = await client.runPipelineAction('demo', 'refresh', fleetPayload());
  assert.equal(receipt.id, 'act_test_receipt_01');
  assert.equal(receipt.createdAt, '2026-09-13T10:00:00+00:00');
  assert.equal(receipt.stages.intent.state, 'recorded');
  assert.equal(receipt.stages.execution.state, 'completed');
});

test('runPipelineAction rejects a malformed receipt', async () => {
  const extraField = { ...validReceipt('refresh') as object, sql: 'SELECT 1' };
  const client = new ControlPlaneClient({
    fetchFn: async () => new Response(JSON.stringify(extraField), { status: 200 }),
  });
  await assert.rejects(
    () => client.runPipelineAction('demo', 'refresh', fleetPayload()),
    ControlPlaneParseError,
  );

  const mismatched = new ControlPlaneClient({
    fetchFn: async () => new Response(JSON.stringify(validReceipt('pause')), { status: 200 }),
  });
  await assert.rejects(
    () => mismatched.runPipelineAction('demo', 'refresh', fleetPayload()),
    ControlPlaneParseError,
  );

  const prodEnv = validReceipt('refresh') as { environment: string };
  prodEnv.environment = 'prod';
  const nonDev = new ControlPlaneClient({
    fetchFn: async () => new Response(JSON.stringify(prodEnv), { status: 200 }),
  });
  await assert.rejects(
    () => nonDev.runPipelineAction('demo', 'refresh', fleetPayload()),
    ControlPlaneParseError,
  );

  const successOnConflict = new ControlPlaneClient({
    fetchFn: async () => new Response(JSON.stringify(validReceipt('refresh', 'succeeded')), { status: 409 }),
  });
  await assert.rejects(
    () => successOnConflict.runPipelineAction('demo', 'refresh', fleetPayload()),
    ControlPlaneParseError,
  );

  const invalidCalendar = validReceipt('refresh') as { created_at: string };
  invalidCalendar.created_at = '2026-99-99T10:00:00+00:00';
  const invalidCreatedAt = new ControlPlaneClient({
    fetchFn: async () => new Response(JSON.stringify(invalidCalendar), { status: 200 }),
  });
  await assert.rejects(
    () => invalidCreatedAt.runPipelineAction('demo', 'refresh', fleetPayload()),
    ControlPlaneParseError,
  );
});

test('runPipelineAction rejects a legacy dataset-scoped receipt', async () => {
  const legacy = validReceipt('refresh') as Record<string, unknown>;
  delete legacy['fleet_id'];
  legacy['dataset'] = TEST_SITE.proofTable;
  const client = new ControlPlaneClient({
    fetchFn: async () => new Response(JSON.stringify(legacy), { status: 200 }),
  });
  await assert.rejects(
    () => client.runPipelineAction('demo', 'refresh', fleetPayload()),
    ControlPlaneParseError,
  );
});

test('runPipelineAction preserves a safe 409 receipt', async () => {
  const client = new ControlPlaneClient({
    fetchFn: async () => new Response(JSON.stringify(validReceipt('refresh', 'conflict')), { status: 409 }),
  });
  const receipt = await client.runPipelineAction('demo', 'refresh', fleetPayload());
  assert.equal(receipt.state, 'conflict');
  assert.equal(receipt.stages.intent.code, 'action_in_progress');
  assert.equal(receipt.environment, TEST_SITE.runtimeEnvironment);

  const unavailable = await new ControlPlaneClient({
    fetchFn: async () => new Response(
      JSON.stringify(validReceipt('refresh', 'unavailable', 'capability_unavailable')),
      { status: 409 },
    ),
  }).runPipelineAction('demo', 'refresh', fleetPayload());
  assert.equal(unavailable.state, 'unavailable');
  assert.equal(unavailable.stages.intent.code, 'capability_unavailable');

  const failed = await new ControlPlaneClient({
    fetchFn: async () => new Response(JSON.stringify(validReceipt('refresh', 'failed')), { status: 409 }),
  }).runPipelineAction('demo', 'refresh', fleetPayload());
  assert.equal(failed.state, 'failed');
  assert.equal(failed.stages.execution.state, 'failed');
});

test('runPipelineAction rejects 409 receipts whose stages encode success or the wrong reason', async () => {
  const payload = fleetPayload();
  const conflictWithSuccessStages = validReceipt('refresh', 'succeeded') as { state: string };
  conflictWithSuccessStages.state = 'conflict';
  await assert.rejects(
    () => new ControlPlaneClient({
      fetchFn: async () => new Response(JSON.stringify(conflictWithSuccessStages), { status: 409 }),
    }).runPipelineAction('demo', 'refresh', payload),
    ControlPlaneParseError,
  );

  const unavailableWithConflictReason = validReceipt('refresh', 'conflict') as { state: string };
  unavailableWithConflictReason.state = 'unavailable';
  await assert.rejects(
    () => new ControlPlaneClient({
      fetchFn: async () => new Response(JSON.stringify(unavailableWithConflictReason), { status: 409 }),
    }).runPipelineAction('demo', 'refresh', payload),
    ControlPlaneParseError,
  );

  const conflictWithUnavailableReason = validReceipt('refresh', 'unavailable') as { state: string };
  conflictWithUnavailableReason.state = 'conflict';
  await assert.rejects(
    () => new ControlPlaneClient({
      fetchFn: async () => new Response(JSON.stringify(conflictWithUnavailableReason), { status: 409 }),
    }).runPipelineAction('demo', 'refresh', payload),
    ControlPlaneParseError,
  );
});

test('runPipelineAction posts exactly the fleet contract body', async () => {
  const requests: string[] = [];
  const client = new ControlPlaneClient({
    fetchFn: async (_input, init) => {
      requests.push(String(init?.body ?? ''));
      return new Response(JSON.stringify(validReceipt('start')), { status: 200 });
    },
  });
  const payload = fleetPayload(`START ${TEST_SITE.siteId.toUpperCase()} ${TEST_SITE.environment}`);
  await client.runPipelineAction('alpha', 'start', payload);
  const body = JSON.parse(requests[0]!);
  assert.deepEqual(Object.keys(body).sort(), ['confirmation', 'environment', 'fleet_id']);
  assert.deepEqual(body, payload);
});

test('runPipelineAction redacts an arbitrary 500 body', async () => {
  const client = new ControlPlaneClient({
    fetchFn: async () => new Response('Snowflake failed: AKIASECRET traceback', { status: 500 }),
  });
  await assert.rejects(
    () => client.runPipelineAction('demo', 'refresh', fleetPayload()),
    (error: unknown) => {
      assert.ok(error instanceof Error);
      assert.equal(error.message, 'Control plane indisponible (500)');
      assert.doesNotMatch(error.message, /Snowflake|AKIASECRET|traceback/);
      assert.equal(error instanceof ControlPlaneActionError, false);
      return true;
    },
  );
});

test('fetchOnboardingDefaults sends GET, forwards the ETag and returns parsed defaults', async () => {
  const requests: Array<{ readonly input: string; readonly method: string | undefined; readonly etag: string | null }> = [];
  const client = new ControlPlaneClient({
    fetchFn: async (input, init) => {
      requests.push({ input, method: init?.method, etag: new Headers(init?.headers).get('If-None-Match') });
      return new Response(JSON.stringify({ defaults: {
        batch_entries: 60_000,
        poll_seconds: 2,
        tls: true,
        allow_plaintext: false,
        tls_ca_file: TEST_SITE.tlsCaFile,
      }, site: TEST_SITE_WIRE }), { status: 200, headers: { ETag: '"defaults-2"' } });
    },
  });

  const result = await client.fetchOnboardingDefaults({ etag: '"defaults-1"' });

  assert.deepEqual(requests, [{ input: '/v1/onboarding/defaults', method: 'GET', etag: '"defaults-1"' }]);
  assert.equal(result.kind, 'updated');
  if (result.kind === 'updated') {
    assert.equal(result.defaults.values.batch_entries, 60_000);
    assert.equal(result.defaults.values.tls, true);
    assert.equal(result.etag, '"defaults-2"');
  }
});

test('fetchOnboardingDefaults keeps 304 explicit and getOnboardingDefaults refuses it without a cache', async () => {
  const client = new ControlPlaneClient({ fetchFn: async () => new Response(null, { status: 304 }) });

  assert.deepEqual(await client.fetchOnboardingDefaults({ etag: '"defaults-2"' }), { kind: 'not-modified' });
  await assert.rejects(() => client.getOnboardingDefaults(), /Réponse 304 sans cache de session/);
});

test('onboarding defaults reject malformed payloads instead of inventing settings', async () => {
  for (const body of [{}, { defaults: null }, { defaults: [] }, { defaults: 'x' }, null, []]) {
    const client = new ControlPlaneClient({
      fetchFn: async () => new Response(JSON.stringify(body), { status: 200 }),
    });
    await assert.rejects(() => client.getOnboardingDefaults(), /Réglages publiés invalides/);
  }
});

test('onboarding defaults keep primitives and drop unusable entries without failing the read', async () => {
  const client = new ControlPlaneClient({
    fetchFn: async () => new Response(JSON.stringify({ defaults: {
      batch_entries: 60_000,
      nested: { unexpected: true },
      list: [1, 2],
      blank: '   ',
      nan: Number.NaN,
    }, site: TEST_SITE_WIRE }), { status: 200 }),
  });

  const defaults = await client.getOnboardingDefaults();
  assert.deepEqual(defaults.values, { batch_entries: 60_000 });
});

// Compile-time contracts: callers keep the original public APIs while controllers use atomic reads.
const _legacyListContract: (signal?: AbortSignal) => Promise<import('../domain/controlPlane.ts').Pipeline[]> =
  new ControlPlaneClient().listPipelines.bind(new ControlPlaneClient());
const _legacyDetailContract: (id: string, signal?: AbortSignal) => Promise<import('../domain/controlPlane.ts').Pipeline> =
  new ControlPlaneClient().getPipeline.bind(new ControlPlaneClient());
const _atomicListResult: PipelineListResult | null = null;
const _atomicPipelineResult: PipelineResult | null = null;
const _onboardingDefaultsContract: (signal?: AbortSignal) => Promise<import('../domain/onboarding.ts').OnboardingDefaults> =
  new ControlPlaneClient().getOnboardingDefaults.bind(new ControlPlaneClient());
void [_legacyListContract, _legacyDetailContract, _atomicListResult, _atomicPipelineResult, _runPipelineActionContract, _onboardingDefaultsContract];
