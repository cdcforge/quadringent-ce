import type { FunctionKeyEntry } from './useFunctionKeys.ts';

/**
 * FunctionKeyBar — bandeau discret de bas d'écran listant les raccourcis
 * réels de l'écran (fournis par `useFunctionKeys`). `aria-keyshortcuts`
 * annonce le raccourci clavier réel aux technologies d'assistance ; le clic
 * déclenche le même gestionnaire que la touche.
 */
export function FunctionKeyBar({
  entries,
  onTrigger,
}: {
  readonly entries: readonly FunctionKeyEntry[];
  readonly onTrigger: (key: FunctionKeyEntry['key']) => void;
}) {
  if (entries.length === 0) return null;
  return (
    <nav className="function-key-bar" aria-label="Raccourcis clavier de l’écran">
      <ul>
        {entries.map((entry) => (
          <li key={entry.key}>
            <button
              type="button"
              className="function-key-bar__key"
              disabled={entry.disabled}
              aria-keyshortcuts={entry.key}
              onClick={() => onTrigger(entry.key)}
            >
              <span className="function-key-bar__code mono">{entry.key}</span>
              <span className="function-key-bar__label">{entry.label}</span>
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}
