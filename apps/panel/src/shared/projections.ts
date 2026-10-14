import type { RunSession, RunSessionPage, StopReceipt } from './contracts.ts';
import { samePartition } from './scope.ts';

// A delayed read must not overwrite a newer accepted projection for the same run.
export function mergeRuns(previous: RunSessionPage | undefined, next: RunSessionPage): RunSessionPage {
  return {
    ...next, items: next.items.map((row) => {
      const existing = previous?.items.find((old) => old.id === row.id && old.run_id === row.run_id &&
        samePartition(old.scope, row.scope));
      return existing && existing.version > row.version ? existing : row;
    }),
  };
}
export function stopTarget(rows: RunSession[]): RunSession | null {
  const active = rows.filter((row) => row.state !== 'STOPPED');
  if (active.length === 1) return active[0];
  // STOP remains available for the only known stopped owner, including bootstrap.
  return active.length === 0 && rows.length === 1 ? rows[0] : null;
}

// A later owner projection may confirm cancellation after the initial STOPPING receipt.
export function stopObservation(receipt: StopReceipt | null, target: RunSession | null): StopReceipt | null {
  if (!receipt || !target || receipt.run_id !== target.run_id ||
    receipt.control_epoch < target.control_epoch || !samePartition(receipt.scope, target.scope)) return null;
  if (receipt.control_epoch === target.control_epoch && target.version > (receipt.version ?? 0)) {
    return { ...receipt, state: target.state, version: target.version,
      unconfirmed_resource_ids: target.unconfirmed_resource_ids,
      status: target.state === 'STOPPED' ? 'completed' : 'waiting' };
  }
  return receipt;
}
