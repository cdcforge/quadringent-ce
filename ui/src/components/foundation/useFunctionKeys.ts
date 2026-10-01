import { useEffect, useRef } from 'react';
import { type FunctionKeyId, functionKeyCopy } from '../../domain/operator.ts';

export interface FunctionKeyBinding {
  readonly key: FunctionKeyId;
  readonly onTrigger: () => void;
  /** Absente : la touche est déclarée mais inactive (aucune cible sur cet écran). */
  readonly disabled?: boolean;
}

export interface FunctionKeyEntry {
  readonly key: FunctionKeyId;
  readonly label: string;
  readonly disabled: boolean;
}

const KEYBOARD_EVENT_KEY: Readonly<Record<FunctionKeyId, string>> = {
  F3: 'F3', F5: 'F5', F9: 'F9', F12: 'F12',
};

/**
 * Résout, pour une touche clavier reçue et un état de saisie donné, la
 * liaison à déclencher — ou `null` si rien ne doit se produire. Pure et
 * testable indépendamment du DOM : le gestionnaire d'événement réel n'est
 * qu'un mince appelant.
 */
export function resolveFunctionKeyTrigger(
  bindings: readonly FunctionKeyBinding[],
  eventKey: string,
  isTyping: boolean,
): FunctionKeyBinding | null {
  if (isTyping) return null;
  const binding = bindings.find((item) => KEYBOARD_EVENT_KEY[item.key] === eventKey);
  if (!binding || binding.disabled) return null;
  return binding;
}

/** Une saisie en cours : input, textarea, select, ou tout élément éditable. */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || target.isContentEditable;
}

/**
 * Enregistre les raccourcis clavier réels (F3/F5/F9/F12) pour l'écran monté.
 * Les gestionnaires ne se déclenchent jamais pendant une saisie dans un champ
 * — la barre est un raccourci d'écran, pas un piège pour qui tape du texte.
 *
 * Retourne la liste des entrées à rendre dans `FunctionKeyBar`.
 */
export function useFunctionKeys(bindings: readonly FunctionKeyBinding[]): readonly FunctionKeyEntry[] {
  const bindingsRef = useRef(bindings);
  bindingsRef.current = bindings;

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      const binding = resolveFunctionKeyTrigger(bindingsRef.current, event.key, isTypingTarget(event.target));
      if (!binding) return;
      event.preventDefault();
      binding.onTrigger();
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, []);

  return bindings.map((binding) => ({
    key: binding.key,
    label: functionKeyCopy(binding.key).label,
    disabled: binding.disabled ?? false,
  }));
}
