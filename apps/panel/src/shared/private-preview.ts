import type { Scope, StreamChunk, StreamTerminal } from './contracts.ts';
import { scopeKey } from './scope.ts';

export type PrivatePreview = {
  scope: Scope; generation: string; attempt: string; sequence: number; text: string; incomplete: boolean;
  status: 'preview' | 'completed' | 'cancelled' | 'rejected' | 'incomplete';
};
export const previewKey = (scope: Scope) => ['private-preview', scopeKey(scope)] as const;
// A bounded ephemeral preview, never a durable reply or a delivery receipt.
export function appendPreview(previous: PrivatePreview | undefined, event: StreamChunk | StreamTerminal): PrivatePreview {
  const same = previous?.generation === event.generation_id && previous.attempt === event.attempt_id;
  if (same && event.sequence <= previous.sequence) return previous;
  const current = same ? previous : undefined;
  const incomplete = (current?.incomplete ?? false) || event.sequence !== (current?.sequence ?? 0) + 1;
  const text = (current?.text ?? '') + (event.type === 'generation.chunk' ? event.data : '');
  return { scope: event.scope, generation: event.generation_id, attempt: event.attempt_id,
    sequence: event.sequence, text: text.slice(0, 4000), incomplete: incomplete || text.length > 4000,
    status: event.type === 'generation.chunk' ? 'preview' : event.status };
}
