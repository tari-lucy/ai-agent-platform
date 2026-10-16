import { ApiFailure } from '../../shared/api.ts';

// Dialogue projections use descending event/creation time and id. Every refresh starts at
// the head; subsequent opaque cursors only fetch older records, without offset shifts.
export type Page<T> = { items: T[]; next_cursor: string | null; complete: boolean };
export type Walk<T> = {
  items: T[];
  pages: number;
  complete: boolean; // the server reported no further page
  error: string | null; // a failed later page: shown, never presented as the end of history
};
export const PAGE_BUDGET = 4; // pages read automatically (50 records each); the user may extend it
export const PAGE_BUDGET_MAX = 40; // hard bound for memory and requests

export async function walk<T extends { id: string }>(
  read: (cursor?: string) => Promise<Page<T>>, budget: number,
): Promise<Walk<T>> {
  const bound = Math.max(1, Math.min(budget, PAGE_BUDGET_MAX));
  const rows = new Map<string, T>();
  const seen = new Set<string>();
  let cursor: string | undefined, pages = 0;
  while (pages < bound) {
    let page: Page<T>;
    try { page = await read(cursor); }
    catch (error) {
      if (pages === 0) throw error; // nothing read: the whole read failed, not "history ended"
      return { items: [...rows.values()], pages, complete: false, error: error instanceof ApiFailure ? error.code : 'NETWORK' };
    }
    pages += 1;
    for (const item of page.items) rows.set(item.id, item); // a repeated id is one record
    if (page.complete || page.next_cursor === null) return { items: [...rows.values()], pages, complete: true, error: null };
    if (seen.has(page.next_cursor)) { // a cursor that does not advance would loop forever
      return { items: [...rows.values()], pages, complete: false, error: 'NETWORK' };
    }
    seen.add(page.next_cursor);
    cursor = page.next_cursor;
  }
  return { items: [...rows.values()], pages, complete: false, error: null };
}

export type HistoryNote = { tone: 'ok' | 'partial' | 'error'; text: string };
export function historyNote(walks: { complete: boolean; error: string | null }[], budget: number): HistoryNote {
  if (walks.some((w) => w.error)) {
    return { tone: 'error', text: 'Часть истории не загрузилась. Это не конец переписки: список может быть неполным.' };
  }
  if (walks.some((w) => !w.complete)) {
    return { tone: 'partial', text: `Загружено до ${budget} страниц по 50 записей; более ранние записи есть на сервере.` };
  }
  return { tone: 'ok', text: 'Вся переписка загружена.' };
}
