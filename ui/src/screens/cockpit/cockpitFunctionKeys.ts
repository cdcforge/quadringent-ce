/**
 * Comportement F9 partagé par les trois écrans du cockpit : amène le focus
 * clavier sur le premier bouton d'action du panneau de contrôles principal
 * de l'écran (id `cockpit-primary-controls`, posé par chaque écran sur son
 * panneau le plus pertinent — flotte à l'accueil, connexion sur l'écran
 * connexion, table sur l'écran table). Ne déclenche jamais une action
 * directement : l'opérateur choisit ensuite explicitement pause/reprise,
 * ce qui préserve le parcours effet annoncé → confirmation.
 */
export function focusPrimaryControls(): void {
  document.querySelector<HTMLButtonElement>('#cockpit-primary-controls .controls-panel__action')?.focus();
}

export function navigateBack(): void {
  window.history.back();
}
