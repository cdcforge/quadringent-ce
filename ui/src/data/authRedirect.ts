/**
 * Redirection automatique vers l'écran de connexion sur une réponse `401`
 * de l'API `/v2` (tâche « auth-login », contrat §6.2/§9.2 — mode
 * « authentification exigée » côté serveur, `v2/auth.py::resolve_identity`).
 *
 * Fonctions pures, testables sans DOM : `navigate`/`getCurrentHash` sont
 * injectés (en production, `window.location.hash` — voir `cockpitClient.ts`
 * et `wizardClient.ts`).
 */

/** Chemins qui ne doivent jamais déclencher de redirection sur 401 : soit
 *  parce qu'un 401 y est une réponse normale et attendue (`/auth/me` sans
 *  session, `/auth/login` avec un mauvais mot de passe — l'écran de
 *  connexion doit alors afficher l'erreur, pas se recharger lui-même), soit
 *  parce qu'un logout n'a jamais besoin d'authentification. */
export function isAuthRedirectExempt(path: string): boolean {
  return /\/auth\/(login|logout|me)$/.test(path);
}

/** Le hash de destination pour l'écran de connexion, portant le hash
 *  courant en `returnTo` (sauf si on y est déjà — jamais de boucle). */
export function loginRedirectHash(currentHash: string): string {
  if (currentHash.startsWith('#/login')) return currentHash;
  const returnTo = currentHash && currentHash !== '#/' ? currentHash : '';
  return returnTo ? `#/login?returnTo=${encodeURIComponent(returnTo)}` : '#/login';
}

/** Enveloppe une fonction `fetch` : sur une réponse `401` d'une route non
 *  exemptée, appelle `navigate` avec le hash de connexion — la réponse
 *  d'origine est toujours renvoyée telle quelle (l'appelant continue de
 *  recevoir/gérer son `ControlPlaneV2Error` normalement ; la navigation est
 *  un effet de bord, pas un court-circuit). */
export function wrapFetchWithAuthRedirect(
  fetchFn: (input: string, init?: RequestInit) => Promise<Response>,
  navigate: (hash: string) => void,
  getCurrentHash: () => string,
): (input: string, init?: RequestInit) => Promise<Response> {
  return async (input, init) => {
    const response = await fetchFn(input, init);
    if (response.status === 401 && !isAuthRedirectExempt(input)) {
      navigate(loginRedirectHash(getCurrentHash()));
    }
    return response;
  };
}
