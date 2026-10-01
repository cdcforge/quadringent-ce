import { productVersion } from '../version.ts';
import { useEffect, type ReactNode } from 'react';
import type { ControlPlaneState } from '../data/controlPlaneController.ts';
import { href, type Route } from '../router.ts';
import { closeMobileDetailsOnEscape, connectionCopy, liveAnnouncement, skipWorkspaceInteraction } from './AppShell.model.ts';
import { scopeLabel } from '../domain/scope.ts';
import { EvidenceContext, evidencePresentation } from './EvidenceContext.tsx';
import { ForgeMark } from './ForgeMark.tsx';
import { LogoutButton } from './LogoutButton.tsx';

/* Les onglets portent le nom de ce qu'on y trouve, dans l'ordre où un
 * exploitant les consulte : l'état d'abord, l'historique ensuite, la
 * consommation en dernier. */
const destinations = [
  { name: 'overview', label: 'Liaisons', mobileLabel: 'Liaisons' },
  { name: 'pipelines', label: 'Tables', mobileLabel: 'Tables' },
  { name: 'incidents', label: 'Journal', mobileLabel: 'Journal' },
  { name: 'usage', label: 'Consommation', mobileLabel: 'Conso.' },
] as const;

export function AppShell({
  route,
  state,
  children,
}: {
  readonly route: Route;
  readonly state: ControlPlaneState;
  readonly children: ReactNode;
}) {
  const selected = selectedDestination(route);
  const hasSnapshot = state.status !== 'loading' && state.status !== 'failed';
  const overviewScope = hasSnapshot ? state.overview.scope : null;
  const scopeKnown = overviewScope != null && overviewScope.kind !== 'unavailable' && overviewScope.environments.length > 0;
  const scope = scopeKnown ? scopeLabel(overviewScope) : null;
  const evidence = evidencePresentation(state);
  const currentTransport = evidence.tone === 'live' && evidence.shortLabel === 'LIVE';
  const connection = connectionCopy(state.connection, state.status, currentTransport ? 'current' : 'technical');
  const routeHref = href(route);

  useEffect(() => {
    closeOpenMobileMenus();
  }, [routeHref]);

  return (
    <div className="product-shell">
      <button
        className="skip-link"
        type="button"
        onClick={(event) =>
          skipWorkspaceInteraction(event, () => {
            document.getElementById('workspace')?.focus({ preventScroll: false });
          })
        }
      >
        Aller au contenu principal
      </button>

      <header className="product-topbar">
        <div className="product-topbar__inner">
          <a className="product-mark" href={href({ name: 'overview' })} aria-label="Quadringent">
            <ForgeMark size={22} />
            <strong>Quadringent</strong><small aria-label={`Version ${productVersion}`}>{productVersion}</small>
          </a>

          <PrimaryNavigation selected={selected} />

          <div className="product-topbar__meta" aria-label="Contexte courant">
            <a
              className={route.name === 'setup' ? 'product-setup is-current' : 'product-setup'}
              aria-current={route.name === 'setup' ? 'page' : undefined}
              href={href({ name: 'setup' })}
            >
              Installation
            </a>
            {/* Un seul repère dans l'en-tête : l'environnement. L'âge d'un
                relevé appartient à la liaison qu'il décrit — le répéter ici
                produisait un « pas à jour » global qui contredisait des
                cartes fraîches. */}
            {scope ? (
              <span className="product-context__item product-context__item--scope">
                <span className="product-context__label">Environnement</span>
                <strong>{scope}</strong>
              </span>
            ) : null}
            {state.connection !== 'live' ? (
              <span
                className={`connection connection--${state.connection}`}
                title={connection.label}
              >
                <span aria-hidden="true" className="connection__shape" />
                <strong className="connection__label">{connection.label}</strong>
              </span>
            ) : null}
            <LogoutButton />
          </div>

          <details
            className="product-mobile-context"
            onKeyDown={closeMobileDetailsOnEscape}
            onToggle={(event) => closePeerMobileMenu(event.currentTarget, '.product-more')}
          >
            <summary aria-label="Ouvrir les détails">Détails</summary>
            <EvidenceContext state={state} />
          </details>
        </div>
      </header>

      <MobileNavigation selected={selected} />

      <p className="sr-only" role="status" aria-live="polite" aria-atomic="true">
        {liveAnnouncement(state)}
      </p>

      <main id="workspace" className="product-workspace" tabIndex={-1}>
        {children}
      </main>
    </div>
  );
}


function PrimaryNavigation({ selected }: { readonly selected: NavSelection }) {
  return (
    <nav className="product-nav product-nav--primary" aria-label="Destinations principales">
      <NavigationLinks selected={selected} />
    </nav>
  );
}

function NavigationLinks({ selected }: { readonly selected: NavSelection }) {
  return (
    <ul>
      {destinations.map((destination) => {
        const current = selected === destination.name;
        return (
          <li key={destination.name}>
            <a
              className={current ? 'is-current' : undefined}
              aria-current={current ? 'page' : undefined}
              href={href({ name: destination.name })}
            >
              {destination.label}
            </a>
          </li>
        );
      })}
    </ul>
  );
}

function MobileNavigation({ selected }: { readonly selected: NavSelection }) {
  const primary = destinations.slice(0, 3);
  return (
    <nav className="product-nav product-nav--mobile" aria-label="Navigation mobile">
      <ul>
        {primary.map((destination) => {
          const current = selected === destination.name;
          return (
            <li key={destination.name}>
              <a
                className={current ? 'is-current' : undefined}
                aria-current={current ? 'page' : undefined}
                href={href({ name: destination.name })}
              >
                {destination.mobileLabel}
              </a>
            </li>
          );
        })}
        <li>
          <details
            className="product-more"
            onKeyDown={closeMobileDetailsOnEscape}
            onToggle={(event) => {
              const panel = event.currentTarget.querySelector<HTMLElement>(':scope > .product-more__menu');
              if (panel) panel.hidden = !event.currentTarget.open;
              closePeerMobileMenu(event.currentTarget, '.product-mobile-context');
            }}
          >
            <summary className={selected === 'usage' || selected === 'setup' ? 'is-current' : undefined}>Plus</summary>
            <div className="product-more__menu" hidden>
              <a aria-current={selected === 'usage' ? 'page' : undefined} href={href({ name: 'usage' })}>Coûts</a>
              <a aria-current={selected === 'setup' ? 'page' : undefined} href={href({ name: 'setup' })}>Connexions</a>
            </div>
          </details>
        </li>
      </ul>
    </nav>
  );
}

type DestinationName = typeof destinations[number]['name'];
type NavSelection = DestinationName | 'setup';

function selectedDestination(route: Route): NavSelection {
  if (route.name === 'setup' || route.name === 'wizard') return 'setup';
  // Le détail d'une liaison s'atteint depuis Liaisons : marquer « Tables »
  // laissait croire qu'on avait changé de surface.
  if (route.name === 'pipeline' || route.name === 'flux') return 'overview';
  if (route.name === 'source' || route.name === 'index') return 'overview';
  // Le cockpit v2 est intercepté dans App() avant d'atteindre AppShell
  // (coquille dédiée) : cette branche n'est jamais réellement empruntée,
  // seulement nécessaire à l'exhaustivité du type Route.
  if (route.name === 'cockpit' || route.name === 'cockpit-connection' || route.name === 'cockpit-table' || route.name === 'cockpit-confirmations') return 'overview';
  // Tâche « auth-login » : la connexion est elle aussi interceptée dans
  // App() avant d'atteindre AppShell (écran autonome) — même remarque.
  if (route.name === 'login') return 'overview';
  return route.name;
}

function closePeerMobileMenu(current: HTMLDetailsElement, selector: string): void {
  if (!current.open) return;
  document.querySelector<HTMLDetailsElement>(selector)?.removeAttribute('open');
}

function closeOpenMobileMenus(): void {
  document
    .querySelectorAll<HTMLDetailsElement>('.product-mobile-context[open], .product-more[open]')
    .forEach((details) => details.removeAttribute('open'));
}
