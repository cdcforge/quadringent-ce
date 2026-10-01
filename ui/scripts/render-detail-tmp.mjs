import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';

const vite = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'silent' });
try {
  const { installSiteIdentity } = await vite.ssrLoadModule('/src/domain/siteIdentity.ts');
  const { parsePipeline, parseOverview } = await vite.ssrLoadModule('/src/domain/controlPlane.ts');
  const { PipelineDetail } = await vite.ssrLoadModule('/src/screens/PipelineDetail.tsx');

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
  const pipeline = overview.pipelines[0];
  const state = {
    status: 'ready', connection: 'live', overview,
    pipelines: overview.pipelines, lastSuccessAt: new Date(overview.generatedAt),
  };
  const tab = process.argv[2] ?? 'overview';
  const markup = renderToStaticMarkup(createElement(PipelineDetail, {
    state, pipeline, route: { name: 'pipeline', id: pipeline.id, tab }, onRefresh() {},
  }));
  const text = markup
    .replace(/<style[\s\S]*?<\/style>/g, '')
    .replace(/<[^>]+>/g, ' ')
    .replace(/\s+/g, ' ');
  console.log(text);
  console.log('\n---RAW MARKUP (flux-band + stages)---');
  const m = markup.match(/class="flux-band"[\s\S]*?flux-tables/);
  console.log(m ? m[0].slice(0, 9000) : 'no flux-band');
} finally {
  await vite.close();
}
