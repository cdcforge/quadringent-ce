import { useEffect, useState } from 'react';
import type { PipelineStatus, StageId } from './domain/controlPlane.ts';

export type PipelineTab = 'overview' | 'tables' | 'live';
export type WizardStep = 'activate' | 'source' | 'snowflake' | 'tables';
export type CockpitTableTab = 'metrics' | 'rows' | 'logs' | 'costs' | 'proofs';
export type Route =
  | { name: 'overview' }
  | { name: 'pipelines' }
  | { name: 'pipeline'; id: string; tab?: PipelineTab; inspect?: StageId }
  | { name: 'source'; id: string; tab?: PipelineTab; inspect?: StageId }
  | { name: 'incidents' }
  | { name: 'usage' }
  | { name: 'setup' }
  | { name: 'login'; returnTo?: string; email?: string }
  | { name: 'wizard'; step: WizardStep }
  | { name: 'index' }
  | { name: 'flux'; id: string }
  | { name: 'cockpit' }
  | { name: 'cockpit-confirmations' }
  | { name: 'cockpit-connection'; id: string }
  | { name: 'cockpit-table'; id: string; tab?: CockpitTableTab };
const pipelineTabs = new Set<PipelineTab>(['overview', 'tables', 'live']);
const inspectStages = new Set<StageId>(['source', 'capture', 'raw', 'load', 'destination']);
const wizardSteps = new Set<WizardStep>(['activate', 'source', 'snowflake', 'tables']);
const cockpitTableTabs = new Set<CockpitTableTab>(['metrics', 'rows', 'logs', 'costs', 'proofs']);

export function parseRoute(hash: string): Route {
  const { path, query } = splitHash(hash);
  if (!path || path === 'overview') return { name: 'overview' };
  if (path === 'index') return { name: 'index' };
  if (path === 'pipelines') return { name: 'pipelines' };
  if (path === 'incidents') return { name: 'incidents' };
  if (path === 'usage') return { name: 'usage' };
  if (path === 'setup') return { name: 'setup' };
  if (path === 'login') {
    // URLSearchParams décode déjà une fois — jamais de second decodeURIComponent
    // ici (le hash de retour peut lui-même contenir des segments encodés).
    const returnTo = query.get('returnTo');
    const email = query.get('email');
    return {
      name: 'login',
      ...(returnTo ? { returnTo } : {}),
      ...(email ? { email } : {}),
    };
  }
  if (path === 'cockpit') return { name: 'cockpit' };
  if (path === 'cockpit/confirmations') return { name: 'cockpit-confirmations' };
  const cockpitConnectionMatch = /^cockpit\/connection\/([^/?#]+)$/.exec(path);
  if (cockpitConnectionMatch) {
    const id = safeDecode(cockpitConnectionMatch[1]);
    return id ? { name: 'cockpit-connection', id } : { name: 'cockpit' };
  }
  const cockpitTableMatch = /^cockpit\/table\/([^/?#]+)(?:\/([^/?#]+))?$/.exec(path);
  if (cockpitTableMatch) {
    const id = safeDecode(cockpitTableMatch[1]);
    if (!id) return { name: 'cockpit' };
    const tab = cockpitTableMatch[2] ? safeDecode(cockpitTableMatch[2]) : undefined;
    if (cockpitTableMatch[2] && !tab) return { name: 'cockpit' };
    return { name: 'cockpit-table', id, ...(tab && cockpitTableTabs.has(tab as CockpitTableTab) ? { tab: tab as CockpitTableTab } : {}) };
  }
  const wizardMatch = /^wizard\/([^/?#]+)$/.exec(path);
  if (wizardMatch) {
    const step = safeDecode(wizardMatch[1]);
    return step && wizardSteps.has(step as WizardStep) ? { name: 'wizard', step: step as WizardStep } : { name: 'wizard', step: 'activate' };
  }
  const legacyFlux = /^flux\/([^/?#]+)$/.exec(path);
  if (legacyFlux) {
    const id = safeDecode(legacyFlux[1]);
    return id ? { name: 'flux', id } : { name: 'overview' };
  }
  const match = /^(pipeline|pipelines|source)\/([^/?#]+)(?:\/([^/?#]+))?$/.exec(path);
  if (!match) return { name: 'overview' };
  const id = safeDecode(match[2]);
  const tab = match[3] ? safeDecode(match[3]) : undefined;
  if (!id || (match[3] && !tab)) return { name: 'overview' };
  if (tab && !pipelineTabs.has(tab as PipelineTab)) return { name: 'overview' };
  const inspect = parseInspectStage(query.get('inspect'));
  return {
    name: match[1] === 'source' ? 'source' : 'pipeline',
    id,
    ...(tab ? { tab: tab as PipelineTab } : {}),
    ...(inspect ? { inspect } : {}),
  };
}
export function href(route: Route): string {
  switch (route.name) {
    case 'overview':
    case 'index':
      return '#/';
    case 'pipelines':
      return '#/pipelines';
    case 'incidents':
      return '#/incidents';
    case 'usage':
      return '#/usage';
    case 'setup':
      return '#/setup';
    case 'login': {
      const params = new URLSearchParams();
      if (route.returnTo) params.set('returnTo', route.returnTo);
      if (route.email) params.set('email', route.email);
      const query = params.toString();
      return query ? `#/login?${query}` : '#/login';
    }
    case 'wizard':
      return `#/wizard/${route.step}`;
    case 'cockpit':
      return '#/cockpit';
    case 'cockpit-confirmations':
      return '#/cockpit/confirmations';
    case 'cockpit-connection':
      return `#/cockpit/connection/${encodeURIComponent(route.id)}`;
    case 'cockpit-table':
      return `#/cockpit/table/${encodeURIComponent(route.id)}${route.tab ? `/${route.tab}` : ''}`;
    case 'pipeline':
    case 'source': {
      const segment = route.name === 'source' ? 'source' : 'pipeline';
      const path = `#/${segment}/${encodeURIComponent(route.id)}${route.tab ? `/${route.tab}` : ''}`;
      return route.inspect ? `${path}?inspect=${encodeURIComponent(route.inspect)}` : path;
    }
    case 'flux':
      return `#/flux/${encodeURIComponent(route.id)}`;
  }
}

export function inspectHref(pipelineId: string, stage: StageId): string {
  return href({ name: 'pipeline', id: pipelineId, tab: 'overview', inspect: stage });
}

export function surfaceHref(route: Route): string {
  return href(route);
}

export function parseInspectStage(value: string | null | undefined): StageId | undefined {
  if (!value) return undefined;
  return inspectStages.has(value as StageId) ? value as StageId : undefined;
}

function splitHash(hash: string): { readonly path: string; readonly query: URLSearchParams } {
  const raw = hash.replace(/^#\/?/, '');
  const queryIndex = raw.indexOf('?');
  const pathPart = queryIndex === -1 ? raw : raw.slice(0, queryIndex);
  const queryPart = queryIndex === -1 ? '' : raw.slice(queryIndex + 1);
  return {
    path: pathPart.replace(/^\/+|\/+$/g, ''),
    query: new URLSearchParams(queryPart),
  };
}
export interface StatusCopy { readonly label: string; readonly description: string; }
export function statusCopy(status: PipelineStatus): StatusCopy { switch (status) { case 'healthy': return { label: 'Garantie vérifiée', description: 'Toutes les étapes obligatoires sont observées et récentes.' }; case 'recovering': return { label: 'Reprise en cours', description: 'Un incident est résolu et le retard diminue.' }; case 'degraded': return { label: 'Couverture partielle', description: 'Le flux avance, mais une observation obligatoire manque.' }; case 'incident': return { label: 'Incident actif', description: 'Une étape obligatoire est interrompue ou en défaut.' }; case 'unknown': return { label: 'État non établi', description: 'Les observations sont absentes, trop anciennes ou contradictoires.' }; case 'planned_stop': return { label: 'Arrêt planifié', description: 'L’arrêt demandé est toujours observé.' }; case 'awaiting_resume': return { label: 'Prête à reprendre', description: 'La cause de l’arrêt est résolue ; la capture attend le relancement.' }; } }
export function useRoute(): Route { const [route, setRoute] = useState(() => parseRoute(window.location.hash)); useEffect(() => { const update = () => setRoute(parseRoute(window.location.hash)); window.addEventListener('hashchange', update); return () => window.removeEventListener('hashchange', update); }, []); return route; }

function safeDecode(value: string): string | null {
  try {
    return decodeURIComponent(value);
  } catch {
    return null;
  }
}
