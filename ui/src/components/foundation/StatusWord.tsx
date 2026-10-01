/**
 * StatusWord — un statut se lit comme un mot, jamais comme une seule couleur.
 *
 * Cinq états produit : `live`, `copying`, `paused`, `attention`, `stopped`.
 * Le rendu porte toujours le mot (`<span>` visible) à côté d'un petit carré
 * décoratif (`aria-hidden`) : la couleur seule n'est jamais le seul porteur
 * de sens (WCAG 1.4.1). Le composant ne décide d'aucun libellé lui-même —
 * les libellés viennent de `statusWordCopy` ci-dessous, en français.
 */
export type StatusWordState = 'live' | 'copying' | 'paused' | 'attention' | 'stopped';

export interface StatusWordCopy {
  readonly label: string;
  readonly description: string;
}

const STATUS_WORD_COPY: Readonly<Record<StatusWordState, StatusWordCopy>> = {
  live: { label: 'En direct', description: 'La capture est active et à jour.' },
  copying: { label: 'Copie en cours', description: 'La copie initiale avance.' },
  paused: { label: 'En pause', description: 'La lecture est arrêtée volontairement ; la position est conservée.' },
  attention: { label: 'Attention', description: 'Un point demande une vérification.' },
  stopped: { label: 'Arrêté', description: 'La capture est arrêtée.' },
};

export function statusWordCopy(state: StatusWordState): StatusWordCopy {
  return STATUS_WORD_COPY[state];
}

export function StatusWord({
  state,
  compact = false,
}: {
  readonly state: StatusWordState;
  /** Rendu resserré pour une cellule de tableau (BandedTable) ou une liste dense. */
  readonly compact?: boolean;
}) {
  const copy = statusWordCopy(state);
  return (
    <span
      className={`status-word status-word--${state}${compact ? ' status-word--compact' : ''}`}
      role="status"
    >
      <span aria-hidden="true" className="status-word__mark" />
      <span className="status-word__label">{copy.label}</span>
      <span className="sr-only"> — {copy.description}</span>
    </span>
  );
}
