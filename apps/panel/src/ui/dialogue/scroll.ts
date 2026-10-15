// Pure scroll rules for the timeline. The DOM glue lives in Timeline.tsx.
export const NEAR_BOTTOM_PX = 48;

export type Metrics = { top: number; height: number; client: number };
export const atBottom = (m: Metrics, tolerance = NEAR_BOTTOM_PX): boolean => m.height - m.top - m.client <= tolerance;

// After the list changed: follow the end only if the reader was already there; otherwise keep
// the reader's place by holding the anchor entry at the same distance from the viewport top.
export type Plan =
  | { kind: 'follow' }
  | { kind: 'anchor'; offset: number }
  | { kind: 'stay' };
export function plan(wasAtBottom: boolean, anchorBefore: number | null, anchorAfter: number | null): Plan {
  if (wasAtBottom) return { kind: 'follow' };
  if (anchorBefore !== null && anchorAfter !== null) return { kind: 'anchor', offset: anchorAfter - anchorBefore };
  return { kind: 'stay' };
}

// Entries that arrived after the reader's last position at the bottom. The jump button counts
// only entries newer than the newest one the reader has seen at the bottom.
export function unseen(keysInOrder: string[], lastSeenKey: string | null): number {
  if (lastSeenKey === null) return 0;
  const at = keysInOrder.lastIndexOf(lastSeenKey);
  return at < 0 ? 0 : keysInOrder.length - at - 1;
}
