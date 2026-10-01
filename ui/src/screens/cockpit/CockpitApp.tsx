import type { Route } from '../../router.ts';
import { CockpitHome } from './CockpitHome.tsx';
import { CockpitConnectionScreen } from './CockpitConnection.tsx';
import { CockpitTableScreen } from './CockpitTable.tsx';
import { CockpitConfirmationsScreen } from './CockpitConfirmations.tsx';
import '../../styles/liaison.css';
import '../../styles/cockpit.css';

/**
 * CockpitApp — coquille autonome du cockpit v2 (accueil, connexion, table),
 * interceptée dans `App.tsx` avant `AppShell` : le backend `/v2` et son
 * modèle `declared_state` sont distincts du fleet v1 (`ControlPlaneState`),
 * donc jamais mêlés à cet état-là (même principe que l'assistant
 * `WizardOnboarding`). Les écrans connexion/table sont ajoutés
 * progressivement ; cette coquille route déjà vers eux une fois construits.
 */
export function CockpitApp({ route }: { readonly route: Extract<Route, { name: 'cockpit' | 'cockpit-connection' | 'cockpit-table' | 'cockpit-confirmations' }> }) {
  if (route.name === 'cockpit') return <CockpitHome />;
  if (route.name === 'cockpit-confirmations') return <CockpitConfirmationsScreen />;
  if (route.name === 'cockpit-connection') return <CockpitConnectionScreen connectionId={route.id} />;
  return <CockpitTableScreen pipelineId={route.id} tab={route.tab ?? 'metrics'} />;
}
