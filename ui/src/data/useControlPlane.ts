import { useEffect, useRef, useState } from 'react';
import {
  ControlPlaneClient,
  type ActionId,
  type PipelineActionReceipt,
} from './controlPlaneClient.ts';
import { ControlPlaneController, type ControlPlaneState } from './controlPlaneController.ts';
import { installSiteIdentity, siteIdentity } from '../domain/siteIdentity.ts';
import { fleetActionRequest } from '../domain/fleetView.ts';

export type { ControlPlaneState } from './controlPlaneController.ts';
const defaultClient = new ControlPlaneClient();

export function useControlPlane(client: ControlPlaneClient = defaultClient): {
  readonly state: ControlPlaneState;
  readonly refresh: () => void;
  readonly runAction: (pipelineId: string, action: ActionId) => Promise<PipelineActionReceipt>;
} {
  const [state, setState] = useState<ControlPlaneState>({ status: 'loading', connection: 'connecting' });
  const controller = useRef<ControlPlaneController | null>(null);
  const bootstrap = useRef<() => void>(() => {});
  useEffect(() => {
    let cancelled = false;
    let current: ControlPlaneController | null = null;
    // Aucune lecture n'est tentée avant que le service publie l'identité du
    // site : les parseurs la confrontent à chaque document. Si le service ne
    // la publie pas, l'écran échoue plutôt que d'inventer un périmètre.
    const start = () => {
      client.getOnboardingDefaults()
        .then((defaults) => {
          if (cancelled) return;
          installSiteIdentity(defaults.site);
          const next = new ControlPlaneController(client, setState);
          current = next;
          controller.current = next;
          next.start();
        })
        .catch(() => {
          if (!cancelled) {
            setState({
              status: 'failed',
              connection: 'offline',
              message: 'La configuration du site n’est pas disponible.',
            });
          }
        });
    };
    bootstrap.current = start;
    start();
    return () => {
      cancelled = true;
      current?.dispose();
      if (controller.current === current) controller.current = null;
    };
  }, [client]);
  return {
    state,
    refresh: () => {
      const current = controller.current;
      if (current) current.refresh();
      else bootstrap.current();
    },
    runAction: async (pipelineId, action) => {
      // Contrat exact du serveur : {fleet_id, environment, confirmation} —
      // aucune clé supplémentaire, le repli « dataset » a été retiré.
      const receipt = await client.runPipelineAction(
        pipelineId,
        action,
        fleetActionRequest(siteIdentity(), action),
      );
      if (receipt.state === 'succeeded') controller.current?.refresh();
      return receipt;
    },
  };
}
