import type {
  GmRuntimeState, HarnessStatus, Opportunity, RunSession, StateChange, Ticket, WorkTask,
} from '../../shared/contracts.ts';

// Pure logic of the "Сейчас" screen: one honest line per question the operator has, no I/O.
// Every line comes from an owner projection; nothing here is predicted or invented.

export type Tone = 'ok' | 'wait' | 'warn' | 'off';
export type Line = { text: string; tone: Tone };

const day = 24 * 3600 * 1000;
const stamp = (value: string) => Date.parse(value) || 0;
const newest = <T extends { updated_at: string }>(rows: T[]) => [...rows].sort((a, b) => stamp(b.updated_at) - stamp(a.updated_at));

export function ago(value: string | null | undefined, now: number): string {
  const at = value ? Date.parse(value) : Number.NaN;
  if (!Number.isFinite(at)) return '—';
  const seconds = Math.max(0, Math.round((now - at) / 1000));
  if (seconds < 60) return 'только что';
  if (seconds < 3600) return `${Math.round(seconds / 60)} мин назад`;
  if (seconds < day / 1000) return `${Math.round(seconds / 3600)} ч назад`;
  return `${Math.round(seconds / (day / 1000))} дн назад`;
}

// ---- status ------------------------------------------------------------------------------------
export function headline(run: RunSession | undefined): Line {
  if (!run) return { text: 'Нет подтверждённого запуска', tone: 'warn' };
  switch (run.state) {
    case 'ACTIVE': return run.activity === 'OPEN' ? { text: 'Работает', tone: 'ok' } :
      { text: run.activity === 'PAUSED' ? 'На паузе' : 'Переходит на паузу', tone: 'wait' };
    case 'STOPPED': return { text: 'Остановлена', tone: 'off' };
    case 'STARTING': return { text: 'Запускается', tone: 'wait' };
    case 'STOPPING': return { text: 'Останавливается', tone: 'wait' };
    default: return { text: 'Нужна проверка', tone: 'warn' };
  }
}
export function budgetLine(harness: HarnessStatus | undefined): Line | null {
  if (!harness?.enabled) return null;
  if (harness.spent_today == null || !harness.daily_budget) return { text: 'Расход за день: тариф не задан', tone: 'wait' };
  const share = harness.spent_today / harness.daily_budget;
  const unit = harness.currency === 'RUB' ? '₽' : harness.currency ?? '';
  const text = `Расход за день: ${harness.spent_today.toFixed(2)} из ${harness.daily_budget} ${unit} · вызовов ${harness.calls_today ?? 0}`;
  return { text, tone: share >= 1 ? 'warn' : share >= 0.8 ? 'wait' : 'ok' };
}

// ---- what needs the operator ----------------------------------------------------------------------
export function attention(run: RunSession | undefined, tickets: Ticket[], harness: HarnessStatus | undefined, changes: StateChange[]): Line[] {
  const lines: Line[] = [];
  if (run?.state === 'FAULTED') lines.push({ text: 'Состояние запуска неизвестно: сверка ресурсов', tone: 'warn' });
  if (run?.state === 'STOPPING' && run.unconfirmed_resource_ids?.length) lines.push({ text: 'Остановка ещё не подтверждена', tone: 'wait' });
  const asked = tickets.filter((ticket) => ticket.state === 'waiting_reply' && ticket.next_turn === 'operator').length;
  if (asked) lines.push({ text: `Агент ждёт вашего ответа в запросах: ${asked}`, tone: 'warn' });
  const decide = changes.filter((change) => change.status === 'AWAITING_APPROVAL').length;
  if (decide) lines.push({ text: `Изменения характера ждут вашего решения: ${decide}`, tone: 'warn' });
  if (harness?.notices?.length) lines.push({ text: 'В открытом разговоре есть удалённое из памяти', tone: 'wait' });
  if (harness?.enabled && !harness.ready && run?.state === 'ACTIVE') lines.push({ text: 'Окно агента (OpenCode) не готово', tone: 'warn' });
  const over = budgetLine(harness);
  if (over && over.tone === 'warn') lines.push(over);
  return lines;
}

// ---- plan -------------------------------------------------------------------------------------------
const planRank: Record<WorkTask['state'], number> = { active: 0, waiting: 1, open: 2, done: 3, abandoned: 4 };
export const taskLabels: Record<WorkTask['state'], string> = {
  active: 'делает', waiting: 'ждёт', open: 'в планах', done: 'сделано', abandoned: 'отменено',
};
export function plan(tasks: WorkTask[], limit = 5): { open: WorkTask[]; done: WorkTask | null } {
  const live = tasks.filter((task) => planRank[task.state] <= 2)
    .sort((a, b) => planRank[a.state] - planRank[b.state] || stamp(b.updated_at) - stamp(a.updated_at));
  const finished = newest(tasks.filter((task) => task.state === 'done'));
  return { open: live.slice(0, limit), done: finished[0] ?? null };
}
export function lastStep(task: WorkTask | undefined): string | null {
  const step = task?.progress?.[task.progress.length - 1];
  return step ? step.summary : null;
}

// ---- character ----------------------------------------------------------------------------------------
export const changeWords: Record<StateChange['status'], string> = {
  PROPOSED: 'предложено', VALIDATING: 'проверяется', READY: 'готово', AWAITING_APPROVAL: 'ждёт вас',
  APPLIED: 'применено', REJECTED: 'отклонено', CONFLICT: 'конфликт версий', CANCELLED: 'отменено',
};
export function characterNews(changes: StateChange[], limit = 3): StateChange[] {
  return newest(changes.filter((change) => change.status !== 'CANCELLED')).slice(0, limit);
}

// ---- game master ------------------------------------------------------------------------------------------
export type GmSummary = { paused: boolean; intensity: number | null; offered: number; lived: number; blocked: number; latest: Opportunity | null };
export function gmSummary(settings: GmRuntimeState | undefined, opportunities: Opportunity[], run: RunSession | undefined): GmSummary {
  const sorted = newest(opportunities);
  return {
    paused: run?.gm_paused ?? false, intensity: settings ? settings.intensity : null,
    offered: opportunities.filter((opp) => ['OFFERED', 'ACCEPTED', 'REALIZING'].includes(opp.status)).length,
    lived: opportunities.filter((opp) => opp.status === 'RESOLVED').length,
    blocked: opportunities.filter((opp) => opp.status === 'BLOCKED').length,
    latest: sorted[0] ?? null,
  };
}

// ---- launch modes ----------------------------------------------------------------------------------------
export type Autonomy = 'observation' | 'autonomous';
export const autonomyKey = 'ai-panel.start-autonomy';
type Store = Pick<Storage, 'getItem' | 'setItem'> | undefined | null;
// The panel always started the agent autonomously; the choice is kept in the browser and defaults to that.
export function loadAutonomy(storage: Store): Autonomy {
  try { return storage?.getItem(autonomyKey) === 'observation' ? 'observation' : 'autonomous'; } catch { return 'autonomous'; }
}
export function saveAutonomy(storage: Store, mode: Autonomy): void {
  try { storage?.setItem(autonomyKey, mode); } catch { /* private window: the choice just is not kept */ }
}

// ---- plan in one line --------------------------------------------------------------------------------------
export function planLine(open: WorkTask[]): { now: string | null; next: string | null; more: number } {
  const [first, second] = open;
  return { now: first?.goal ?? null, next: second?.goal ?? null, more: Math.max(0, open.length - 2) };
}

// ---- the quiet row of chips: only what the cards above do not already say -------------------------------------
const linkNames: Record<string, string> = { avatar: 'аватар', speech: 'речь', listening: 'слух', obs: 'OBS', twitch: 'Twitch', youtube: 'YouTube' };
const linkStates: Record<string, { word: string; tone: Tone }> = {
  on: { word: 'работает', tone: 'ok' }, idle: { word: 'ждёт', tone: 'off' }, down: { word: 'нет связи', tone: 'warn' },
};
export function chips(run: RunSession | undefined, harness: HarnessStatus | undefined): Line[] {
  const rows: Line[] = [];
  if (run && run.activity !== 'OPEN') rows.push({ text: run.activity === 'PAUSED' ? 'деятельность на паузе' : 'уходит на паузу', tone: 'wait' });
  for (const link of harness?.live ?? []) {
    const state = linkStates[link.state] ?? linkStates.idle;
    rows.push({ text: `${linkNames[link.name] ?? link.name}: ${state.word}`, tone: state.tone });
  }
  return rows;
}
