import type { ProofFocus } from '../domain/proofFocus.ts';
import { ProofChain } from './ProofChain.tsx';

export function WorkspaceProofline({
  proof,
  onRefresh,
}: {
  readonly proof: ProofFocus;
  readonly onRefresh: () => void;
}) {
  return <ProofChain proof={proof} variant="workspace" onRefresh={onRefresh} />;
}
