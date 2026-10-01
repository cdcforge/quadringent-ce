import { useEffect, useState } from 'react';

/**
 * Bouton de déconnexion discret dans la barre existante (tâche
 * « auth-login »). N'importe pas `ControlPlaneV2Client` : `AppShell` reste
 * un composant v1, un appel direct à `/v2/auth/me`/`/v2/auth/logout` suffit
 * ici et évite un couplage au client v2 complet. Réutilise le style du lien
 * « Installation » (`.product-setup`, `product-shell.css`) — pas de CSS ni
 * de dépendance ajoutée.
 *
 * Invisible tant qu'aucune session `/v2` n'est active (`GET /v2/auth/me` en
 * échec) — jamais affiché en mode développement/loopback sans
 * authentification exigée, où il n'y a rien à déconnecter.
 */
export function LogoutButton() {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetch('/v2/auth/me', { headers: { Accept: 'application/json' } })
      .then((response) => {
        if (!cancelled) setVisible(response.ok);
      })
      .catch(() => {
        if (!cancelled) setVisible(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!visible) return null;

  const logout = () => {
    void fetch('/v2/auth/logout', { method: 'POST' }).finally(() => {
      window.location.hash = '#/login';
    });
  };

  return (
    <button type="button" className="product-setup" onClick={logout}>
      Déconnexion
    </button>
  );
}
