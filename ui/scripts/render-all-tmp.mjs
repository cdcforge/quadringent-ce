import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';

const vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'silent' });
try {
  const { installSiteIdentity } = await vite.ssrLoadModule('/src/domain/siteIdentity.ts');
  const { parseOverview } = await vite.ssrLoadModule('/src/domain/controlPlane.ts');
  const screens = {
    incidents: (await vite.ssrLoadModule('/src/screens/Incidents.tsx')).Incidents,
    usage: (await vite.ssrLoadModule('/src/screens/Usage.tsx')).Usage,
    pipelines: (await vite.ssrLoadModule('/src/screens/Pipelines.tsx')).Pipelines,
  };
  const defaults = await (await fetch('http://localhost:8844/v1/onboarding/defaults')).json();
  const s = defaults.site;
  installSiteIdentity({
    siteId: s.site_id, fleetId: s.fleet_id, environment: s.environment,
    runtimeEnvironment: s.runtime_environment, destinationDatabase: s.destination_database,
    destinationSchema: s.destination_schema, destinationNamespace: s.destination_namespace,
    sourceSchema: s.source_schema, journalName: s.journal_name, manifest: s.tables,
    proofTable: s.proof_table, runtimePipelineId: s.runtime_pipeline_id,
    ibmiHost: s.ibmi_host, ibmiUser: s.ibmi_user, tlsCaFile: s.tls_ca_file,
    secretRefName: s.secret_ref_name, secretRefKey: s.secret_ref_key,
    snowflakeStage: s.snowflake_stage,
  });
  const raw = await (await fetch('http://localhost:8844/v1/overview')).json();
  const overview = parseOverview(raw);
  const state = {
    status: 'ready', connection: 'live', overview,
    pipelines: overview.pipelines, lastSuccessAt: new Date(overview.generatedAt),
  };
  const target = process.argv[2] ?? 'incidents';
  const Screen = screens[target];
  const markup = renderToStaticMarkup(createElement(Screen, {
    state, overview, pipelines: overview.pipelines, onRefresh() {},
  }));
  const text = markup
    .replace(/<style[\s\S]*?<\/style>/g, '')
    .replace(/<[^>]+>/g, ' ')
    .replace(/\s+/g, ' ');
  console.log(text);
} finally {
  await vite.close();
}
