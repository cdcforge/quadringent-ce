import { ControlPlaneV2Client } from './controlPlaneV2Client.ts';
import { wrapFetchWithAuthRedirect } from './authRedirect.ts';

/**
 * Fabrique le client de l'assistant v2. Sans control plane `/v2` déployé,
 * l'assistant tourne en mode démo (fixtures locales, aucun réseau) — même
 * principe que la page de référence des fondations (`#/_fondation`) : un
 * bandeau `import.meta.env.DEV` isole ce qui n'est pas encore branché à un
 * service réel du contrat produit expédié. L'import de `fixtures/wizardDemo.ts`
 * est **dynamique et gardé par `import.meta.env.DEV`** (constante figée au
 * build) afin que Rollup élimine toute la branche — code et sourcemap — du
 * bundle de production ; `scripts/verify-build.mjs` fait échouer le build au
 * moindre résidu de fixture.
 *
 * `VITE_QUADRINGENT_V2_BASE_PATH` bascule vers un vrai control plane une
 * fois celui-ci déployé (ex. `/v2`).
 */
export async function createWizardClient(): Promise<ControlPlaneV2Client> {
  const basePath = import.meta.env.VITE_QUADRINGENT_V2_BASE_PATH as string | undefined;
  if (import.meta.env.DEV && !basePath) {
    const { createWizardDemoFetch } = await import('./wizardDemoBridge.ts');
    return new ControlPlaneV2Client({ fetchFn: createWizardDemoFetch() });
  }
  // Tâche « auth-login » : voir cockpitClient.ts (même mécanisme) — une
  // fois le compte admin activé, les étapes suivantes de l'assistant
  // (source/snowflake/tables) exigent une session ; sans elle (401), l'UI
  // renvoie vers l'écran de connexion plutôt que d'afficher une erreur
  // technique opaque.
  return new ControlPlaneV2Client({
    basePath: basePath ?? '/v2',
    fetchFn: wrapFetchWithAuthRedirect(fetch, (hash) => { window.location.hash = hash; }, () => window.location.hash),
  });
}
