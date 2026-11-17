import type {
  CharacterState, GmRuntimeState, HarnessNow, HourBucket, HarnessStatus, Opportunity, RunSession, StateChange, Ticket, WorkTask,
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

// ---- character: traits with their last movement -----------------------------------------------------------
export const traitNames: Record<string, string> = {
  curiosity: 'любопытство', initiative: 'инициативность', openness_to_revision: 'открытость к пересмотру',
  caution: 'осторожность', perseverance: 'настойчивость', editorial_theatricality: 'театральность',
};
export type Trait = { key: string; label: string; value: number; delta: number | null; when: string | null };
const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null;

export function traits(state: CharacterState | undefined, changes: StateChange[]): Trait[] {
  const data = isRecord(state?.values) && isRecord(state.values.data) ? state.values.data : undefined;
  const values = data && isRecord(data.values) ? data.values : {};
  const applied = newest(changes.filter((change) => change.status === 'APPLIED'));
  const rows: Trait[] = [];
  for (const [key, raw] of Object.entries(values)) {
    if (typeof raw !== 'number' || !Number.isFinite(raw)) continue;
    const last = applied.find((change) => {
      const patch = isRecord(change.patch) && isRecord(change.patch.data) ? change.patch.data : undefined;
      return patch?.property === key;
    });
    const patch = last && isRecord(last.patch) && isRecord(last.patch.data) ? last.patch.data : undefined;
    const before = typeof patch?.before === 'number' ? patch.before : null;
    const after = typeof patch?.after === 'number' ? patch.after : null;
    rows.push({
      key, label: traitNames[key] ?? key.replaceAll('_', ' '), value: Math.min(1, Math.max(0, raw)),
      delta: before !== null && after !== null ? Math.round((after - before) * 100) / 100 : null, when: last?.updated_at ?? null,
    });
  }
  return rows;
}

// ---- game master funnel ------------------------------------------------------------------------------------
export type Funnel = { proposed: number; accepted: number; lived: number; stopped: number };
export function funnel(opportunities: Opportunity[]): Funnel {
  const count = (...states: Opportunity['status'][]) => opportunities.filter((opp) => states.includes(opp.status)).length;
  return {
    proposed: count('DRAFT', 'SCREENED', 'OFFERED'), accepted: count('ACCEPTED', 'REALIZING'), lived: count('RESOLVED'),
    stopped: count('BLOCKED', 'DECLINED', 'EXPIRED', 'CANCELLED'),
  };
}

// ---- hours ----------------------------------------------------------------------------------------------------
export type Ribbon = { hour: string; total: number; said: number; heard: number; posted: number; drew: number };
export function ribbon(hours: HourBucket[] | undefined): { rows: Ribbon[]; peak: number; busy: number } {
  const rows = (hours ?? []).map((bucket) => ({
    hour: bucket.hour, said: bucket.said ?? 0, heard: bucket.heard ?? 0, posted: bucket.posted ?? 0, drew: bucket.drew ?? 0,
    total: (bucket.said ?? 0) + (bucket.heard ?? 0) + (bucket.posted ?? 0) + (bucket.drew ?? 0),
  }));
  return { rows, peak: Math.max(1, ...rows.map((row) => row.total)), busy: rows.filter((row) => row.total > 0).length };
}


// ---- the ChatGPT plan: asleep or awake, and what she has used --------------------------------------
export function tokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1).replace('.', ',')} млн`;
  if (value >= 1_000) return `${Math.round(value / 1_000)} тыс`;
  return String(value);
}

function left(until: string, nowMs: number): string {
  const seconds = Math.max(0, Math.round((Date.parse(until) - nowMs) / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.ceil((seconds % 3600) / 60);
  if (hours === 0) return `${Math.max(1, minutes)} мин`;
  return minutes === 60 ? `${hours + 1} ч` : `${hours} ч ${minutes} мин`;
}

const clock = (value: string) => new Date(value).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });

// A time of day, with the date when it is not today.
function when(value: string, nowMs: number): string {
  const at = new Date(value);
  const time = at.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  return at.toDateString() === new Date(nowMs).toDateString() ? time : `${at.toLocaleDateString('ru-RU', { day: 'numeric', month: 'long' })} в ${time}`;
}

const windowName = (minutes: number | null | undefined) =>
  minutes === 300 ? 'окно 5 часов' : minutes === 10080 ? 'окно недели' : minutes ? `окно ${minutes} мин` : 'окно';

export type PlanCard = { asleep: boolean; headline: string; tone: Tone; lines: string[] } | null;

// The remaining allowance is not readable while she works (the provider says it only when it refuses),
// so this shows what is known: asleep or not, when the window opens, and what her turns have used.
export function planCard(picture: HarnessNow | undefined, nowMs: number): PlanCard {
  if (!picture || (!picture.sleep && !picture.plan_use)) return null;
  const { sleep, plan_use: use } = picture;
  const lines: string[] = [];
  if (sleep) {
    lines.push(sleep.source === 'default'
      ? 'Время сброса провайдер не назвал: посмотрю ещё раз через полчаса.'
      : `Окно лимита откроется в ${clock(sleep.until)} (через ${left(sleep.until, nowMs)}), тогда она проснётся сама.`);
  }
  if (use) {
    lines.push(`Потрачено токенов: за 5 часов ${tokens(use.window)}, за сутки ${tokens(use.day)}, за неделю ${tokens(use.week)}.`);
    if (use.last_limit_at && use.last_limit_tokens) {
      lines.push(`Прошлый раз лимит наступил ${ago(use.last_limit_at, nowMs)} после ${tokens(use.last_limit_tokens)} за 5 часов.`);
    }
    // The provider's own figures from the moment it refused: the only exact reading of the allowance there is.
    const known = (use.last_limit_windows ?? []).filter((win) => win.used_percent != null);
    if (known.length && use.last_limit_at) {
      const parts = known.map((win) => `${windowName(win.minutes)} израсходовано на ${Math.round(win.used_percent ?? 0)} %` +
        (win.resets_at && Date.parse(win.resets_at) > nowMs ? `, откроется ${when(win.resets_at, nowMs)}` : ''));
      lines.push(`По данным провайдера на момент отказа (${ago(use.last_limit_at, nowMs)}): ${parts.join('; ')}.`);
    }
    if (use.since && nowMs - Date.parse(use.since) < 7 * day) lines.push(`Считаю с ${clock(use.since)}: раньше данных нет.`);
  }
  return sleep
    ? { asleep: true, headline: `Спит: лимит подписки исчерпан, ещё ${left(sleep.until, nowMs)}`, tone: 'wait', lines }
    : { asleep: false, headline: 'Бодрствует, лимит подписки не исчерпан', tone: 'ok', lines };
}
