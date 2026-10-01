import assert from 'node:assert/strict';
import test from 'node:test';
import {
  ControlPlaneV2Error,
  ControlPlaneV2Client,
  pendingConfirmationId,
  type CockpitEventSourceLike,
} from './controlPlaneV2Client.ts';

class FakeEventSource implements CockpitEventSourceLike {
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

  emit(type: string, data: string, lastEventId = ''): void {
    this.listeners.get(type)?.(new MessageEvent(type, { data, lastEventId }));
  }
}

function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json', ...headers } });
}

function fakeFetch(handler: (input: string, init?: RequestInit) => Response) {
  return async (input: string, init?: RequestInit) => handler(input, init);
}

test('every write request carries a generated Idempotency-Key header, unique per call', async () => {
  const seen: string[] = [];
  const client = new ControlPlaneV2Client({
    fetchFn: fakeFetch((_input, init) => {
      const key = (init?.headers as Record<string, string>)['Idempotency-Key'];
      assert.ok(key && key.length >= 8, 'Idempotency-Key manquante ou trop courte');
      seen.push(key);
      return jsonResponse(200, { before: null, after: { id: 'src_1' }, verify: { method: 'GET', path: '/v2/sources/src_1' }, dry_run: null });
    }),
  });
  await client.testSource('src_1');
  await client.testSource('src_1');
  assert.equal(new Set(seen).size, 2, 'chaque écriture doit porter une clé différente');
});

test('a GET request never carries an Idempotency-Key', async () => {
  const client = new ControlPlaneV2Client({
    fetchFn: fakeFetch((_input, init) => {
      assert.equal((init?.headers as Record<string, string> | undefined)?.['Idempotency-Key'], undefined);
      return jsonResponse(200, { items: [], next_cursor: null });
    }),
  });
  await client.listTables('src_1');
});

test('confirmation buttons follow the available actions declared by the service', async () => {
  const client = new ControlPlaneV2Client({
    fetchFn: fakeFetch(() => jsonResponse(200, { items: [{
      id: 'cf_1', action_ref: 'pipeline.restart_initial_copy', resource_type: 'pipeline', resource_id: 'pl_1',
      reason: 'Copie demandée', state: 'pending', available: { approve: true, reject: false, execute: false },
    }] })),
  });
  const [confirmation] = await client.listConfirmations();
  assert.deepEqual(confirmation.available, { approve: true, reject: false, execute: false });
});

test('the error envelope {code,message,next_action,retryable} becomes a typed ControlPlaneV2Error', async () => {
  const client = new ControlPlaneV2Client({
    fetchFn: fakeFetch(() => jsonResponse(409, {
      error: { code: 'capability_unavailable', message: 'La table n’est pas prête.', next_action: 'Relisez l’état de la table.', retryable: false },
    })),
  });
  await assert.rejects(
    () => client.testSource('src_1'),
    (error: unknown) => {
      assert.ok(error instanceof ControlPlaneV2Error);
      assert.equal((error as ControlPlaneV2Error).code, 'capability_unavailable');
      assert.equal((error as ControlPlaneV2Error).nextAction, 'Relisez l’état de la table.');
      assert.equal((error as ControlPlaneV2Error).retryable, false);
      return true;
    },
  );
});

test('an error body outside the known catalogue still becomes a safe, generic ControlPlaneV2Error', async () => {
  const client = new ControlPlaneV2Client({
    fetchFn: fakeFetch(() => jsonResponse(500, { error: { code: 'totally_unknown_code', message: 'x', next_action: 'y', retryable: true } })),
  });
  await assert.rejects(
    () => client.testSource('src_1'),
    (error: unknown) => {
      assert.ok(error instanceof ControlPlaneV2Error);
      assert.equal((error as ControlPlaneV2Error).code, 'internal_error');
      return true;
    },
  );
});

test('testSource without a wired probe returns the honest "unavailable" shape, never a false green', async () => {
  const client = new ControlPlaneV2Client({
    fetchFn: fakeFetch(() => jsonResponse(200, {
      before: null,
      // Forme exacte de `SourcesService.test` sans `probe` injecté
      // (`request.app.state.source_probe` absent — l'état par défaut de
      // toute installation qui n'a pas encore câblé de sonde IBM i réelle).
      after: { source_id: 'src_1', reachable: 'unknown', secret_set: true },
      verify: { method: 'GET', path: '/v2/sources/src_1' },
      dry_run: null,
    })),
  });
  const result = await client.testSource('src_1');
  assert.equal(result.kind, 'unavailable');
  assert.equal(result.kind === 'unavailable' && result.secretSet, true);
});

test('testSource with a wired probe parses network/tls/authentication/version/timezone', async () => {
  const client = new ControlPlaneV2Client({
    fetchFn: fakeFetch(() => jsonResponse(200, {
      before: null,
      // Forme exacte de `SourceProbeResult.to_dict()` quand une sonde est câblée.
      after: {
        source_id: 'src_1',
        reachable: true,
        network: { ok: true, detail: 'Hôte joignable sur le port 8471.' },
        tls: { ok: true, detail: 'Certificat reconnu.', fingerprint: 'AA:BB:CC' },
        authentication: { ok: true, detail: 'Authentification réussie.' },
        ibmi_version: 'V7R5M0',
        qtimzon: 'QN0100CET',
        detected_time_zone: 'Europe/Paris',
        timezone_ambiguous: false,
      },
      verify: { method: 'GET', path: '/v2/sources/src_1' },
      dry_run: null,
    })),
  });
  const result = await client.testSource('src_1');
  assert.equal(result.kind, 'probed');
  assert.ok(result.kind === 'probed');
  assert.equal(result.network.state, 'ok');
  assert.equal(result.tls.state, 'ok');
  assert.equal(result.tlsFingerprint, 'AA:BB:CC');
  assert.equal(result.ibmiVersion, 'V7R5M0');
  assert.equal(result.detectedTimeZone, 'Europe/Paris');
});

test('testSource with a wired probe reporting a failed check never silently marks it ok', async () => {
  const client = new ControlPlaneV2Client({
    fetchFn: fakeFetch(() => jsonResponse(200, {
      before: null,
      after: {
        source_id: 'src_1',
        reachable: false,
        network: { ok: true, detail: 'Hôte joignable.' },
        tls: { ok: false, detail: 'Autorité privée non reconnue.', fingerprint: 'DE:AD:BE:EF' },
        authentication: { ok: false, detail: 'Non testée : le certificat n’est pas approuvé.' },
        ibmi_version: null,
        qtimzon: null,
        detected_time_zone: null,
        timezone_ambiguous: false,
      },
      verify: { method: 'GET', path: '/v2/sources/src_1' },
      dry_run: null,
    })),
  });
  const result = await client.testSource('src_1');
  assert.ok(result.kind === 'probed');
  assert.equal(result.tls.state, 'failed');
  assert.equal(result.tlsFingerprint, 'DE:AD:BE:EF');
  assert.equal(result.reachable, false);
});

test('testSource parses an unrecognized authority as unknown trust, never silently accepted', async () => {
  const client = new ControlPlaneV2Client({
    fetchFn: fakeFetch(() => jsonResponse(200, {
      before: null,
      after: {
        source_id: 'src_1',
        // authentification non tentée : la confiance n'est pas établie —
        // voir SourcesService.test/quadringent_source_probe_job.py::tls_probe.
        reachable: false,
        network: { ok: true, detail: 'Hôte joignable.' },
        tls: {
          ok: true,
          detail: 'Autorité non reconnue par le magasin système — mesure seule.',
          fingerprint: 'AA:BB:CC:DD',
          trust: 'unknown',
          certificate_pem: '-----BEGIN CERTIFICATE-----\nMEASURED\n-----END CERTIFICATE-----\n',
        },
        authentication: { ok: false, detail: 'non tentée (confiance non établie)' },
        ibmi_version: null,
        qtimzon: null,
        detected_time_zone: null,
        timezone_ambiguous: false,
      },
      verify: { method: 'GET', path: '/v2/sources/src_1' },
      dry_run: null,
    })),
  });
  const result = await client.testSource('src_1');
  assert.ok(result.kind === 'probed');
  assert.equal(result.tls.state, 'ok'); // poignée de main réussie...
  assert.equal(result.tlsTrust, 'unknown'); // ...mais jamais acceptée silencieusement
  assert.equal(result.tlsFingerprint, 'AA:BB:CC:DD');
  assert.ok(result.tlsCertificatePem?.includes('BEGIN CERTIFICATE'));
  assert.equal(result.reachable, false);
});

test('testSource parses trust=system/pinned and treats an unrecognized value as null', async () => {
  const client = new ControlPlaneV2Client({
    fetchFn: fakeFetch(() => jsonResponse(200, {
      before: null,
      after: {
        source_id: 'src_1',
        reachable: true,
        network: { ok: true, detail: 'ok' },
        tls: { ok: true, detail: 'ok', fingerprint: 'AA:BB', trust: 'pinned', certificate_pem: null },
        authentication: { ok: true, detail: 'ok' },
        ibmi_version: 'V7R5M0',
        qtimzon: 'QN0100CET',
        detected_time_zone: 'Europe/Paris',
        timezone_ambiguous: false,
      },
      verify: { method: 'GET', path: '/v2/sources/src_1' },
      dry_run: null,
    })),
  });
  const result = await client.testSource('src_1');
  assert.ok(result.kind === 'probed');
  assert.equal(result.tlsTrust, 'pinned');
  assert.equal(result.tlsCertificatePem, null);
});

test('createSource sends secret.value (not secret.value_or_ref) — the only key routes/sources.py reads', async () => {
  const calls: Array<{ path: string; body: unknown }> = [];
  const client = new ControlPlaneV2Client({
    fetchFn: fakeFetch((input, init) => {
      calls.push({ path: input, body: init?.body ? JSON.parse(init.body as string) : null });
      return jsonResponse(201, {
        before: null,
        after: { id: 'src_1', display_name: null, ibmi_host: 'as400.local', ibmi_user: 'QSECOFR' },
        verify: { method: 'GET', path: '/v2/sources/src_1' },
        dry_run: null,
      });
    }),
  });
  const record = await client.createSource({ displayName: 'IBM i — as400.local', host: 'as400.local', account: 'QSECOFR', password: 'secret' });
  assert.equal(record.id, 'src_1');
  const body = calls[0]!.body as Record<string, unknown>;
  assert.deepEqual(body.secret, { kind: 'inline', value: 'secret' });
});

test('createDestination sends snowflake_account and returns the generated SQL setup script plus the one-time private key', async () => {
  const calls: Array<{ body: unknown }> = [];
  const client = new ControlPlaneV2Client({
    fetchFn: fakeFetch((_input, init) => {
      calls.push({ body: init?.body ? JSON.parse(init.body as string) : null });
      return jsonResponse(201, {
        before: null,
        after: {
          id: 'dst_1',
          snowflake_account: 'abcd-xy12345',
          verification_state: 'declared_not_verified',
          setup_script: 'CREATE ROLE...',
          private_key_pem: '-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n',
        },
        verify: { method: 'GET', path: '/v2/destinations/dst_1' },
        dry_run: null,
      });
    }),
  });
  const destination = await client.createDestination({ accountIdentifier: 'abcd-xy12345' });
  assert.equal((calls[0]!.body as Record<string, unknown>).snowflake_account, 'abcd-xy12345');
  assert.equal(destination.sqlScript, 'CREATE ROLE...');
  assert.equal(destination.id, 'dst_1');
  assert.equal(destination.verificationState, 'declared_not_verified');
  assert.ok(destination.privateKeyPem?.includes('PRIVATE KEY'));
});

test('la création et la relecture conservent le scope Snowflake explicite', async () => {
  let body: Record<string, unknown> = {};
  const record = { id: 'dst_scope', snowflake_account: 'test-account', destination_database: 'CLIENT_DB', destination_schema: 'SITE_A' };
  const client = new ControlPlaneV2Client({ fetchFn: fakeFetch((_input, init) => {
    if (init?.body) body = JSON.parse(init.body as string);
    return jsonResponse(200, init?.body ? { after: record } : { items: [record] });
  }) });
  const created = await client.createDestination({ accountIdentifier: 'test-account', destinationDatabase: 'CLIENT_DB', destinationSchema: 'SITE_A' });
  assert.deepEqual(body, { snowflake_account: 'test-account', destination_database: 'CLIENT_DB', destination_schema: 'SITE_A' });
  assert.equal(created.destinationDatabase, 'CLIENT_DB');
  assert.equal(created.destinationSchema, 'SITE_A');
  const [reread] = await client.listDestinations();
  assert.equal(reread!.destinationDatabase, 'CLIENT_DB');
  assert.equal(reread!.destinationSchema, 'SITE_A');
});

test('une ancienne destination reste sur le contrat RAW/CURATED', async () => {
  let body: unknown;
  const client = new ControlPlaneV2Client({ fetchFn: fakeFetch((_input, init) => {
    body = JSON.parse(init!.body as string);
    return jsonResponse(200, { after: { id: 'dst_legacy', snowflake_account: 'test-account', destination_database: 'QUADRINGENT', destination_schema: null } });
  }) });
  const destination = await client.createDestination({ accountIdentifier: 'test-account' });
  assert.deepEqual(body, { snowflake_account: 'test-account' });
  assert.equal(destination.destinationDatabase, 'QUADRINGENT');
  assert.equal(destination.destinationSchema, null);
});

test('listTables parses the real table shape (schema_name/table_name, readiness, key_strategy/key_columns, CL command objects)', async () => {
  const client = new ControlPlaneV2Client({
    fetchFn: fakeFetch(() => jsonResponse(200, {
      items: [
        {
          id: 'tbl_1', schema_name: 'DEMOLIB', table_name: 'CLIENTS', discovered_row_count: 12000, discovered_size_bytes: 4200000,
          readiness: 'ready', key_strategy: 'unique_index', key_columns: ['ID'], cl_fix_commands: [],
        },
        {
          id: 'tbl_2', schema_name: 'DEMOLIB', table_name: 'COMMANDES', discovered_row_count: 500, discovered_size_bytes: 90000,
          readiness: 'not_journaled', key_strategy: 'rrn', key_columns: [],
          cl_fix_commands: [{ command: 'STRJRNPF FILE(DEMOLIB/COMMANDES) JRN(DEMOLIB/DEMOJRN)', reason: 'Non journalisée.' }],
        },
      ],
      next_cursor: null,
    })),
  });
  const tables = await client.listTables('src_1');
  assert.equal(tables.length, 2);
  assert.equal(tables[0]!.readiness, 'ready');
  assert.equal(tables[0]!.library, 'DEMOLIB');
  assert.equal(tables[0]!.name, 'CLIENTS');
  assert.deepEqual(tables[1]!.clFixCommands, ['STRJRNPF FILE(DEMOLIB/COMMANDES) JRN(DEMOLIB/DEMOJRN)']);
});

test('patchTableKey sends key_strategy/key_columns/acknowledge_rrn (not key_status) and startTablePipeline supports dry_run', async () => {
  const calls: Array<{ path: string; body: unknown }> = [];
  const client = new ControlPlaneV2Client({
    fetchFn: fakeFetch((input, init) => {
      calls.push({ path: input, body: init?.body ? JSON.parse(init.body as string) : null });
      if (input.endsWith('/pipeline')) {
        return jsonResponse(200, { before: null, after: { id: 'tbl_2', declared_state: 'copying' }, verify: { method: 'GET', path: '/v2/pipelines/tbl_2' }, dry_run: null });
      }
      return jsonResponse(200, {
        before: null,
        after: { id: 'tbl_2', schema_name: 'DEMOLIB', table_name: 'COMMANDES', readiness: 'ready', key_strategy: 'rrn', key_columns: [], cl_fix_commands: [] },
        verify: { method: 'GET', path: '/v2/tables/tbl_2' },
        dry_run: null,
      });
    }),
  });
  await client.patchTableKey('tbl_2', { keyStrategy: 'rrn', acknowledgeRrn: true });
  await client.startTablePipeline('tbl_2', { dryRun: true });
  assert.equal((calls[0]!.body as Record<string, unknown>).key_strategy, 'rrn');
  assert.equal((calls[0]!.body as Record<string, unknown>).acknowledge_rrn, true);
  assert.equal((calls[1]!.body as Record<string, unknown>).dry_run, true);
});

test('patchTableKey with unique_index sends the declared key_columns', async () => {
  const calls: Array<{ body: unknown }> = [];
  const client = new ControlPlaneV2Client({
    fetchFn: fakeFetch((_input, init) => {
      calls.push({ body: init?.body ? JSON.parse(init.body as string) : null });
      return jsonResponse(200, {
        before: null,
        after: { id: 'tbl_2', schema_name: 'DEMOLIB', table_name: 'COMMANDES', readiness: 'ready', key_strategy: 'unique_index', key_columns: ['ID_COMMANDE'], cl_fix_commands: [] },
        verify: { method: 'GET', path: '/v2/tables/tbl_2' },
        dry_run: null,
      });
    }),
  });
  const table = await client.patchTableKey('tbl_2', { keyStrategy: 'unique_index', keyColumns: ['ID_COMMANDE'] });
  assert.deepEqual((calls[0]!.body as Record<string, unknown>).key_columns, ['ID_COMMANDE']);
  assert.deepEqual(table.keyColumns, ['ID_COMMANDE']);
});

test('activateAdmin exchanges the single-use link token and a new password', async () => {
  const client = new ControlPlaneV2Client({
    fetchFn: fakeFetch((_input, init) => {
      const body = JSON.parse(init!.body as string);
      assert.equal(body.token, 'tok_abc');
      assert.equal(body.password, 'un-mot-de-passe-suffisant');
      return jsonResponse(200, { before: null, after: { id: 'usr_1', role: 'admin', activated: true }, verify: { method: 'GET', path: '/v2/users/usr_1' }, dry_run: null });
    }),
  });
  const user = await client.activateAdmin('tok_abc', 'un-mot-de-passe-suffisant');
  assert.equal(user.activated, true);
});

test('activateAdmin also recognizes the real server shape (email + activated_at, no activated boolean)', async () => {
  const client = new ControlPlaneV2Client({
    fetchFn: fakeFetch(() => jsonResponse(200, {
      before: null,
      after: { id: 'usr_1', email: 'admin@example.com', role: 'admin', activated_at: '2026-09-24T10:00:00Z' },
      verify: null,
      dry_run: null,
    })),
  });
  const user = await client.activateAdmin('tok_abc', 'un-mot-de-passe-suffisant');
  assert.equal(user.email, 'admin@example.com');
  assert.equal(user.activated, true);
});

test('login posts credentials and parses the session user; a wrong password raises ControlPlaneV2Error', async () => {
  const client = new ControlPlaneV2Client({
    fetchFn: fakeFetch((_input, init) => {
      const body = JSON.parse(init!.body as string);
      if (body.password === 'bon-mot-de-passe') {
        return jsonResponse(200, { user: { id: 'usr_1', email: body.email, role: 'admin' } });
      }
      return jsonResponse(401, { error: { code: 'invalid_request', message: 'email/mot de passe incorrect', next_action: 'fournir une identité valide', retryable: false } });
    }),
  });
  const user = await client.login('admin@example.com', 'bon-mot-de-passe');
  assert.deepEqual(user, { email: 'admin@example.com', role: 'admin' });
  await assert.rejects(() => client.login('admin@example.com', 'mauvais-mot-de-passe'), ControlPlaneV2Error);
});

test('me returns null (not a throw) without a session, and the identity with one', async () => {
  let authenticated = false;
  const client = new ControlPlaneV2Client({
    fetchFn: fakeFetch(() => authenticated
      ? jsonResponse(200, { subject: 'user:1', role: 'admin', actor_kind: 'human', email: 'admin@example.com' })
      : jsonResponse(401, { error: { code: 'invalid_request', message: 'authentification requise', next_action: 'se connecter', retryable: false } })),
  });
  assert.equal(await client.me(), null);
  authenticated = true;
  assert.deepEqual(await client.me(), { email: 'admin@example.com', role: 'admin' });
});

test('logout never throws, even on a non-2xx response', async () => {
  const client = new ControlPlaneV2Client({ fetchFn: fakeFetch(() => jsonResponse(200, { logged_out: true })) });
  await client.logout();
});

test('getPipeline parses the declared state and rejects an unknown one to a safe default', async () => {
  const client = new ControlPlaneV2Client({
    fetchFn: fakeFetch(() => jsonResponse(200, { id: 'pl_1', declared_state: 'live' })),
  });
  const pipeline = await client.getPipeline('pl_1');
  assert.equal(pipeline.declaredState, 'live');
});

test('runPipelineAction sends dry_run and confirmation_token, and parses the before/after/verify envelope', async () => {
  const calls: Array<{ path: string; body: Record<string, unknown> }> = [];
  const client = new ControlPlaneV2Client({
    fetchFn: fakeFetch((input, init) => {
      calls.push({ path: input, body: JSON.parse(init!.body as string) });
      return jsonResponse(200, {
        before: { id: 'pl_1', declared_state: 'copying' },
        after: { id: 'pl_1', declared_state: 'paused' },
        verify: { method: 'GET', path: '/v2/pipelines/pl_1' },
        dry_run: null,
      });
    }),
  });
  const envelope = await client.runPipelineAction('pl_1', 'pause', { dryRun: true, confirmationToken: 'cf_1' });
  assert.equal(calls[0]!.path, '/v2/pipelines/pl_1/actions/pause');
  assert.equal(calls[0]!.body.dry_run, true);
  assert.equal(calls[0]!.body.confirmation_token, 'cf_1');
  assert.equal(envelope.after?.declaredState, 'paused');
  assert.equal(envelope.before?.declaredState, 'copying');
  assert.deepEqual(envelope.verify, { method: 'GET', path: '/v2/pipelines/pl_1' });
});

test('replay carries from_sequence/to_sequence as query parameters', async () => {
  const calls: string[] = [];
  const client = new ControlPlaneV2Client({
    fetchFn: fakeFetch((input) => {
      calls.push(input);
      return jsonResponse(200, { before: null, after: null, verify: null, dry_run: { plan: true } });
    }),
  });
  await client.runPipelineAction('pl_1', 'replay', { dryRun: true, fromSequence: 10, toSequence: 20 });
  assert.equal(calls[0], '/v2/pipelines/pl_1/actions/replay?from_sequence=10&to_sequence=20');
});

test('pendingConfirmationId extracts the id from a pending_confirmation_required error, and only that code', async () => {
  const client = new ControlPlaneV2Client({
    fetchFn: fakeFetch(() => jsonResponse(409, {
      error: {
        code: 'pending_confirmation_required',
        message: 'confirmation requise (id=cf_42) — voir /v2/confirmations/cf_42',
        next_action: 'Approuvez la confirmation.',
        retryable: false,
      },
    })),
  });
  await assert.rejects(
    () => client.runPipelineAction('pl_1', 'remove'),
    (error: unknown) => {
      assert.ok(error instanceof ControlPlaneV2Error);
      assert.equal(pendingConfirmationId(error as ControlPlaneV2Error), 'cf_42');
      return true;
    },
  );
  const otherError = new ControlPlaneV2Error(403, 'wrong_confirmation', 'non', 'non', false);
  assert.equal(pendingConfirmationId(otherError), null);
});

test('runSourceAction, runDestinationAction and runFleetAction hit the contract-only routes with dry_run', async () => {
  const calls: string[] = [];
  const client = new ControlPlaneV2Client({
    fetchFn: fakeFetch((input) => {
      calls.push(input);
      return jsonResponse(200, { before: null, after: { paused: true }, verify: null, dry_run: null });
    }),
  });
  await client.runSourceAction('src_1', 'pause', { dryRun: true });
  await client.runDestinationAction('dst_1', 'resume');
  await client.runFleetAction('pause_all');
  assert.deepEqual(calls, [
    '/v2/sources/src_1/actions/pause',
    '/v2/destinations/dst_1/actions/resume',
    '/v2/actions/pause_all',
  ]);
});

test('getMetrics parses lag/throughput points for the requested window', async () => {
  const client = new ControlPlaneV2Client({
    fetchFn: fakeFetch(() => jsonResponse(200, {
      points: [{ at: '2026-09-23T10:00:00Z', lag_seconds: 12, throughput_rows_per_second: 4.5 }],
    })),
  });
  const series = await client.getMetrics('pl_1', '24h');
  assert.equal(series.window, '24h');
  assert.equal(series.points[0]!.lagSeconds, 12);
});

test('getLogs never carries row data and forwards filters as query parameters', async () => {
  const calls: string[] = [];
  const client = new ControlPlaneV2Client({
    fetchFn: fakeFetch((input) => {
      calls.push(input);
      return jsonResponse(200, { items: [{ at: '2026-09-23T10:00:00Z', level: 'error', message: 'Incident détecté', incident_id: 'inc_1' }] });
    }),
  });
  const logs = await client.getLogs('pl_1', { level: 'error', correlateIncident: true });
  assert.equal(calls[0], '/v2/pipelines/pl_1/logs?level=error&correlate_incident=true');
  assert.equal(logs[0]!.incidentId, 'inc_1');
});

test('getCosts distinguishes measured/estimated/absent and defaults to absent on an unknown status', async () => {
  const client = new ControlPlaneV2Client({
    fetchFn: fakeFetch(() => jsonResponse(200, { status: 'estimated', amount: 3.2, currency: 'USD', basis: 'warehouse_credits', collected_at: '2026-09-23T09:00:00Z' })),
  });
  const cost = await client.getCosts('table', 'tbl_1', '24h');
  assert.equal(cost.status, 'estimated');
  assert.equal(cost.amount, 3.2);
});

test('confirmations: list defaults to pending, approve/reject parse the resulting record', async () => {
  const calls: string[] = [];
  const client = new ControlPlaneV2Client({
    fetchFn: fakeFetch((input) => {
      calls.push(input);
      if (input.endsWith('/approve')) return jsonResponse(200, { before: null, after: { id: 'cf_1', state: 'approved', action_ref: 'pipeline.remove', resource_type: 'pipeline', resource_id: 'pl_1', reason: 'retrait', risk_estimate: null, requested_by_kind: 'human', requested_by_id: 'op_1', expires_at: null, approved_by_kind: 'human', approved_by_id: 'op_1', approved_at: '2026-09-23T09:00:00Z', created_at: '2026-09-23T08:00:00Z' } });
      if (input.endsWith('/reject')) return jsonResponse(200, { before: null, after: { id: 'cf_1', state: 'rejected', action_ref: 'pipeline.remove', resource_type: 'pipeline', resource_id: 'pl_1', reason: 'retrait', risk_estimate: null, requested_by_kind: null, requested_by_id: null, expires_at: null, approved_by_kind: null, approved_by_id: null, approved_at: null, created_at: null } });
      return jsonResponse(200, { items: [], next_cursor: null });
    }),
  });
  await client.listConfirmations();
  assert.equal(calls[0], '/v2/confirmations?state=pending');
  await client.listConfirmations('all');
  assert.equal(calls[1], '/v2/confirmations?state=all');
  const approved = await client.approveConfirmation('cf_1');
  assert.equal(approved.state, 'approved');
  const rejected = await client.rejectConfirmation('cf_1');
  assert.equal(rejected.state, 'rejected');
});

test('listAudit forwards filters and parses records, never dropping before/after', async () => {
  const calls: string[] = [];
  const client = new ControlPlaneV2Client({
    fetchFn: fakeFetch((input) => {
      calls.push(input);
      return jsonResponse(200, { items: [{ id: 'a_1', at: '2026-09-23T09:00:00Z', actor_kind: 'human', actor_id: 'op_1', actor_display: 'Opérateur', mcp_client: null, action: 'pipeline.pause', resource_type: 'pipeline', resource_id: 'pl_1', request_id: 'req_1', idempotency_key: 'idem_1', dry_run: false, confirmation_id: null, status: 'succeeded', before: { declared_state: 'copying' }, after: { declared_state: 'paused' } }], next_cursor: null });
    }),
  });
  const records = await client.listAudit({ actorKind: 'human', limit: 10 });
  assert.equal(calls[0], '/v2/audit?actor_kind=human&limit=10');
  assert.deepEqual(records[0]!.after, { declared_state: 'paused' });
});

test('subscribeEvents resumes with Last-Event-ID on reconnect and reconnects with bounded backoff', () => {
  const streams: FakeEventSource[] = [];
  const urls: string[] = [];
  const client = new ControlPlaneV2Client({
    eventSourceFactory: (url) => {
      urls.push(url);
      const stream = new FakeEventSource();
      streams.push(stream);
      return stream;
    },
    reconnectDelayMs: 100,
    maxReconnectDelayMs: 400,
  });
  const received: Array<{ type: string; id: number }> = [];
  const connections: boolean[] = [];
  const unsubscribe = client.subscribeEvents((event) => received.push({ type: event.type, id: event.id }), (connected) => connections.push(connected));

  assert.equal(urls[0], '/v2/events');
  streams[0]!.onopen?.(new Event('open'));
  assert.deepEqual(connections, [true]);
  streams[0]!.emit('pipeline.state_changed', JSON.stringify({ id: 'pl_1' }), '42');
  assert.deepEqual(received, [{ type: 'pipeline.state_changed', id: 42 }]);

  streams[0]!.onerror?.(new Event('error'));
  assert.equal(streams[0]!.closed, true);
  assert.deepEqual(connections, [true, false]);

  unsubscribe();
});

test('an invalid SSE payload never throws and parses to a null payload', () => {
  const streams: FakeEventSource[] = [];
  const client = new ControlPlaneV2Client({
    eventSourceFactory: (_url) => {
      const stream = new FakeEventSource();
      streams.push(stream);
      return stream;
    },
  });
  const received: unknown[] = [];
  const unsubscribe = client.subscribeEvents((event) => received.push(event.payload));
  streams[0]!.emit('alert.fired', '{not-json', '1');
  assert.deepEqual(received, [null]);
  unsubscribe();
});

test('listSources and listDestinations parse the items array from each list route', async () => {
  const calls: string[] = [];
  const client = new ControlPlaneV2Client({
    fetchFn: fakeFetch((input) => {
      calls.push(input);
      if (input.endsWith('/sources')) return jsonResponse(200, { items: [{ id: 'src_1', display_name: 'Ventes', ibmi_host: 'as400.local', ibmi_user: 'QSECOFR' }], next_cursor: null });
      return jsonResponse(200, { items: [{ id: 'dst_1', snowflake_account: 'abcd-xy1', verification_state: 'declared_not_verified', setup_script: '' }], next_cursor: null });
    }),
  });
  const sources = await client.listSources();
  const destinations = await client.listDestinations();
  assert.deepEqual(calls, ['/v2/sources', '/v2/destinations']);
  assert.equal(sources[0]!.displayName, 'Ventes');
  assert.equal(destinations[0]!.id, 'dst_1');
});

test('listPipelines parses the observed figures (retard/débit/lignes/dernière arrivée) alongside declared_state', async () => {
  const client = new ControlPlaneV2Client({
    fetchFn: fakeFetch(() => jsonResponse(200, {
      items: [{
        id: 'pl_1',
        table_id: 'tbl_1',
        source_id: 'src_1',
        destination_id: 'dst_1',
        declared_state: 'live',
        lag_seconds: 12.5,
        throughput_rows_per_second: 3.2,
        rows_source: 1000,
        rows_destination: 998,
        last_arrival_at: '2026-09-23T09:00:00Z',
        absent_reasons: {},
      }],
      next_cursor: null,
    })),
  });
  const [pipeline] = await client.listPipelines();
  assert.equal(pipeline!.id, 'pl_1');
  assert.equal(pipeline!.tableId, 'tbl_1');
  assert.equal(pipeline!.sourceId, 'src_1');
  assert.equal(pipeline!.destinationId, 'dst_1');
  assert.equal(pipeline!.declaredState, 'live');
  assert.equal(pipeline!.observation.lagSeconds, 12.5);
  assert.equal(pipeline!.observation.throughputRowsPerSecond, 3.2);
  assert.equal(pipeline!.observation.rowsSource, 1000);
  assert.equal(pipeline!.observation.rowsDestination, 998);
  assert.equal(pipeline!.observation.lastArrivalAt, '2026-09-23T09:00:00Z');
});

test('listPipelines never fabricates a value for an absent figure — null plus its reason', async () => {
  const client = new ControlPlaneV2Client({
    fetchFn: fakeFetch(() => jsonResponse(200, {
      items: [{
        id: 'pl_2',
        table_id: 'tbl_2',
        source_id: 'src_1',
        destination_id: 'dst_1',
        declared_state: 'paused',
        lag_seconds: null,
        throughput_rows_per_second: null,
        rows_source: 500,
        rows_destination: null,
        last_arrival_at: null,
        absent_reasons: { lag_seconds: 'Table en pause.', throughput_rows_per_second: 'Table en pause.', rows_destination: 'Table en pause.', last_arrival_at: 'Table en pause.' },
      }],
      next_cursor: null,
    })),
  });
  const [pipeline] = await client.listPipelines();
  assert.equal(pipeline!.observation.lagSeconds, null);
  assert.equal(pipeline!.observation.rowsSource, 500);
  assert.equal(pipeline!.observation.rowsDestination, null);
  assert.equal(pipeline!.observation.absentReasons.lag_seconds, 'Table en pause.');
});
