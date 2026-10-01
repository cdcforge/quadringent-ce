import { useEffect, useRef } from 'react';
import { AppShell } from './components/AppShell.tsx';
import { restoreWorkspaceAfterNavigation } from './components/AppShell.model.ts';
import { Overview as OverviewScreen } from './screens/Overview.tsx';
import { Pipelines as PipelinesScreen } from './screens/Pipelines.tsx';
import { PipelineDetail as PipelineDetailScreen, pipelineStageAnchorId } from './screens/PipelineDetail.tsx';
import { Incidents as IncidentsScreen } from './screens/Incidents.tsx';
import { Usage as UsageScreen } from './screens/Usage.tsx';
import { Setup as SetupScreen } from './screens/Setup.tsx';
import { WizardOnboarding } from './screens/wizard/WizardOnboarding.tsx';
import { CockpitApp } from './screens/cockpit/CockpitApp.tsx';
import { LoginRoute } from './screens/LoginRoute.tsx';
import { useFirstRunWizardRedirect } from './data/useFirstRunRedirect.ts';
import type { ControlPlaneState } from './data/useControlPlane.ts';
import type { Pipeline } from './domain/controlPlane.ts';
import { fleetActionRequest } from './domain/fleetView.ts';
import { siteIdentity } from './domain/siteIdentity.ts';
import { surfaceHref, type Route, useRoute } from './router.ts';
import { useControlPlane } from './data/useControlPlane.ts';
import { scopeLabel } from './domain/scope.ts';
import { ControlPlaneClient, type ActionId, type PipelineActionReceipt, type PipelineActionRequest } from './data/controlPlaneClient.ts';

const fleetClient = new ControlPlaneClient();

export default function App() {
  const route = useRoute();
  const { state, refresh, runAction } = useControlPlane();
  useFirstRunWizardRedirect(route);
  const routeKey = surfaceHref(route);
  const previousRouteKey = useRef<string | null>(null);
  const handledInspect = useRef<string | null>(null);
  // Tâche « auth-login » : capturé une fois, au premier rendu — jamais
  // relu depuis `window.location.hash` ensuite (voir l'effet ci-dessous qui
  // retire le jeton de la barre d'adresse une fois utilisé).
  const activationTokenRef = useRef<string | null>(
    typeof window !== 'undefined'
      ? new URLSearchParams(window.location.hash.split('?')[1] ?? '').get('token')
      : null,
  );
  const inspect = route.name === 'pipeline' || route.name === 'source' ? route.inspect : undefined;
  const pipelines = visiblePipelines(state);
  const detailRoute =
    route.name === 'pipeline'
      ? route
      : route.name === 'source'
        ? { name: 'pipeline', id: route.id, tab: route.tab, inspect: route.inspect } as const
        : route.name === 'flux'
          ? { name: 'pipeline', id: route.id } as const
          : null;
  const pipeline = detailRoute ? pipelines.find((item) => item.id === detailRoute.id) ?? null : null;

  useEffect(() => {
    // Tâche « auth-login » : ne laisse jamais le jeton d'activation visible
    // dans la barre d'adresse une fois capturé (WizardActivate.tsx le lit
    // depuis `activationTokenRef`, jamais depuis le hash) — `replaceState`
    // ne déclenche pas `hashchange`, donc `route` (dérivé de `useRoute()`)
    // n'est jamais recalculé par cet effet. Se déclenche une seule fois, au
    // montage, sur le rendu qui a effectivement capturé un jeton.
    if (typeof window === 'undefined' || !activationTokenRef.current) return;
    if (route.name !== 'wizard' || route.step !== 'activate') return;
    window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}#/wizard/activate`);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    // Le cockpit v2 (connexion/table) gère son propre `document.title` une
    // fois le nom humain résolu (voir CockpitConnection.tsx/CockpitTable.tsx) —
    // cet effet ne doit pas l'écraser avec l'id technique à chaque relecture
    // du fleet v1, sans rapport avec cet état-là.
    if (route.name === 'cockpit-connection' || route.name === 'cockpit-table' || route.name === 'cockpit-confirmations') return;
    document.title = pageTitle(route, state);
  }, [route, state]);

  useEffect(() => {
    const routeChanged = previousRouteKey.current !== routeKey;
    const firstRun = previousRouteKey.current === null;
    previousRouteKey.current = routeKey;
    // Un deep-link d'étape — y compris au chargement initial — cible l'étape
    // demandée ; une navigation ordinaire rend la main au workspace. Tant que
    // l'ancre n'est pas rendue (lecture en cours), l'étape reste à traiter.
    if (inspect) {
      if (handledInspect.current === routeKey) return;
      const target = document.getElementById(pipelineStageAnchorId(inspect));
      if (!target) {
        if (routeChanged) window.scrollTo({ top: 0, left: 0, behavior: 'auto' });
        return;
      }
      handledInspect.current = routeKey;
      target.scrollIntoView({ block: 'start', behavior: 'auto' });
      target.focus({ preventScroll: true });
      return;
    }
    handledInspect.current = null;
    if (!routeChanged || firstRun) return;
    restoreWorkspaceAfterNavigation(
      () => window.scrollTo({ top: 0, left: 0, behavior: 'auto' }),
      () => document.getElementById('workspace')?.focus({ preventScroll: true }),
    );
  }, [routeKey, inspect, pipeline]);

  const overview = state.status === 'loading' || state.status === 'failed' ? null : state.overview;
  const runOverviewAction = async (pipelineId: string, action: ActionId): Promise<PipelineActionReceipt> => {
    const target = pipelines.find((item) => item.id === pipelineId);
    const payload = fleetOverviewActionPayload(target, action);
    if (payload) {
      const receipt = await fleetClient.runPipelineAction(pipelineId, action, payload);
      if (receipt.state === 'succeeded') refresh();
      return receipt;
    }
    return runAction(pipelineId, action);
  };

  if (route.name === 'login') {
    // Tâche « auth-login » : écran autonome, sans la coquille du cockpit ni
    // celle de l'assistant — atteint soit directement, soit par
    // redirection sur 401 (`data/authRedirect.ts`, `returnTo` retrouvé
    // après connexion), soit après une activation dont la connexion
    // automatique a échoué (`WizardActivate.tsx`, `email` préremplie).
    return <LoginRoute returnTo={route.returnTo} prefilledEmail={route.email} />;
  }

  if (route.name === 'wizard') {
    // Premier lancement : assistant autonome, sans la coquille de navigation
    // du cockpit (pas de fleet encore installée à afficher dans le topbar).
    return <WizardOnboarding step={route.step} activationToken={activationTokenRef.current} />;
  }

  if (route.name === 'cockpit' || route.name === 'cockpit-connection' || route.name === 'cockpit-table' || route.name === 'cockpit-confirmations') {
    // Cockpit v2 : backend `/v2` distinct (déclaré/pipelines) du fleet v1 —
    // coquille autonome dédiée (`CockpitApp`), même principe que l'assistant
    // ci-dessus. `AppShell`/`ControlPlaneState` restent v1 et ne sont jamais
    // mêlés à cet état.
    return <CockpitApp route={route} />;
  }

  return (
    <AppShell route={route} state={state}>
      {renderRoute(route, state, overview, pipelines, pipeline, detailRoute, refresh, runOverviewAction)}
    </AppShell>
  );
}

function renderRoute(
  route: Route,
  state: ControlPlaneState,
  overview: import('./domain/controlPlane.ts').Overview | null,
  pipelines: readonly Pipeline[],
  pipeline: Pipeline | null,
  detailRoute: Extract<Route, { name: 'pipeline' }> | null,
  refresh: () => void,
  runAction: (pipelineId: string, action: ActionId) => Promise<PipelineActionReceipt>,
) {
  switch (route.name) {
    case 'overview':
    case 'index':
      return <OverviewScreen state={state} overview={overview} onRefresh={refresh} onBusinessAction={runAction} />;
    case 'pipelines':
      return <PipelinesScreen state={state} overview={overview} pipelines={pipelines} onRefresh={refresh} />;
    case 'incidents':
      return <IncidentsScreen state={state} overview={overview} pipelines={pipelines} onRefresh={refresh} />;
    case 'usage':
      return <UsageScreen state={state} overview={overview} pipelines={pipelines} onRefresh={refresh} />;
    case 'setup':
      return <SetupScreen state={state} onRefresh={refresh} />;
    case 'login':
      // Interceptée plus haut dans App() : la connexion n'utilise pas AppShell.
      return null;
    case 'wizard':
      // Interceptée plus haut dans App() : l'assistant n'utilise pas AppShell.
      return null;
    case 'pipeline':
    case 'source':
    case 'flux':
      return <PipelineDetailScreen state={state} pipeline={pipeline} route={detailRoute ?? { name: 'pipeline', id: route.id }} onRefresh={refresh} onBusinessAction={runAction} />;
    case 'cockpit':
    case 'cockpit-confirmations':
    case 'cockpit-connection':
    case 'cockpit-table':
      // Interceptée plus haut dans App() : le cockpit v2 n'utilise pas AppShell.
      return null;
  }
}

function pageTitle(route: Route, state: ControlPlaneState): string {
  const suffix = state.status === 'failed'
    ? ' · service indisponible'
    : state.status === 'loading' || state.status === 'refreshing'
      ? ' · vérification en cours'
      : '';
  const overviewScope = state.status === 'loading' || state.status === 'failed' ? null : state.overview.scope;
  const scope = overviewScope != null && overviewScope.kind !== 'unavailable' && overviewScope.environments.length > 0
    ? ` · ${scopeLabel(overviewScope)}`
    : '';
  switch (route.name) {
    case 'overview':
    case 'index':
      return `Quadringent · Liaisons${scope}${suffix}`;
    case 'pipelines':
      return `Quadringent · Tables${scope}${suffix}`;
    case 'incidents':
      return `Quadringent · Journal${scope}${suffix}`;
    case 'usage':
      return `Quadringent · Consommation${scope}${suffix}`;
    case 'setup':
      return `Quadringent · Connexions${scope}${suffix}`;
    case 'login':
      return 'Quadringent · Connexion';
    case 'wizard':
      return 'Quadringent · Connexion d’une source';
    case 'cockpit':
      return 'Quadringent · Cockpit';
    case 'cockpit-confirmations':
      return 'Quadringent · Confirmations';
    case 'cockpit-connection':
      return `Quadringent · Cockpit · ${route.id}`;
    case 'cockpit-table':
      return `Quadringent · Cockpit · ${route.id}`;
    case 'pipeline':
      return `Quadringent · ${route.id}${suffix}`;
    case 'source':
      return `Quadringent · ${route.id}${suffix}`;
    case 'flux':
      return `Quadringent · ${route.id}${suffix}`;
  }
}

function visiblePipelines(state: ControlPlaneState): readonly Pipeline[] {
  if (state.status === 'loading' || state.status === 'failed') return [];
  return state.pipelines;
}

export function usesFleetOverviewAction(pipeline: Pipeline | undefined): boolean {
  return Boolean(pipeline?.fleet || (pipeline?.fleetPlan && pipeline?.fleetRuntime));
}

export function fleetOverviewActionPayload(
  pipeline: Pipeline | undefined,
  action: ActionId,
): PipelineActionRequest | null {
  if (!usesFleetOverviewAction(pipeline) || !pipeline) return null;
  const site = siteIdentity();
  const fleetId = pipeline.fleet?.fleetId ?? pipeline.fleetRuntime?.fleetId ?? site.fleetId;
  return fleetActionRequest({ ...site, fleetId }, action);
}
