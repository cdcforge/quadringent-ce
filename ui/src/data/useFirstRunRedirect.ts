import { useEffect } from 'react';
import { getCockpitClient } from './useCockpit.ts';
import { href, type Route } from '../router.ts';

/** Pure : seule l'accueil (`overview`/`index`) avec zéro source déclare le
 *  premier lancement — testable sans réseau ni horloge. */
export function shouldRedirectToWizard(routeName: Route['name'], sourceCount: number): boolean {
  return (routeName === 'overview' || routeName === 'index') && sourceCount === 0;
}

/**
 * Premier lancement : si l'accueil est ouvert et qu'aucune source IBM i
 * n'est encore déclarée côté `/v2`, renvoie vers l'assistant plutôt que de
 * laisser l'exploitant face à un accueil vide sans indication de la marche
 * à suivre. Ne redirige que depuis l'accueil (`overview`/`index`) — jamais
 * depuis un autre écran, ni plus d'une fois par montage. Une erreur réseau
 * (service `/v2` indisponible) n'entraîne jamais de redirection : rester
 * sur l'accueil v1 existant est le choix le plus sûr tant qu'on ne sait pas.
 */
export function useFirstRunWizardRedirect(route: Route): void {
  useEffect(() => {
    if (route.name !== 'overview' && route.name !== 'index') return;
    let alive = true;
    getCockpitClient()
      .then((client) => client.listSources())
      .then((sources) => {
        if (alive && shouldRedirectToWizard(route.name, sources.length)) {
          window.location.hash = href({ name: 'wizard', step: 'source' });
        }
      })
      .catch(() => {
        // Service /v2 indisponible ou non déployé : rien à faire, l'accueil
        // v1 reste affiché tel quel.
      });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- ne dépend que du nom de route, pas de son identité complète.
  }, [route.name]);
}
