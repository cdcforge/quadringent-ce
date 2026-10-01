import { useEffect, useRef, useState } from 'react';
import type { ConsoleSnapshot } from '../domain/types';
import type { MetricsSource } from './source';

export type LoadState =
  | { status: 'loading' }
  | { status: 'ready'; snapshot: ConsoleSnapshot; readAt: Date }
  | { status: 'failed'; message: string; readAt: Date };

export function useSnapshot(source: MetricsSource): {
  state: LoadState;
  reload: () => void;
} {
  const [state, setState] = useState<LoadState>({ status: 'loading' });
  const [nonce, setNonce] = useState(0);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    source
      .read()
      .then((snapshot) => {
        if (alive.current) setState({ status: 'ready', snapshot, readAt: new Date() });
      })
      .catch((error: unknown) => {
        if (!alive.current) return;
        setState({
          status: 'failed',
          message: error instanceof Error ? error.message : String(error),
          readAt: new Date(),
        });
      });
    return () => {
      alive.current = false;
    };
  }, [source, nonce]);

  return { state, reload: () => setNonce((n) => n + 1) };
}

/** Horloge de l'écran. L'âge d'une mesure doit vieillir sous les yeux : c'est
 *  la seule animation qui gagne sa place. Une seconde de pas, pas moins. */
export function useNow(intervalMs = 1000): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return now;
}
