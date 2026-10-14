import type { CreateCommand, LaboratoryReport, Scope } from '../../shared/contracts.ts';

// Pure laboratory logic: catalogue, report summaries, replay comparison, start rules and commands. No I/O.
// The laboratory works only in test memory; a green mock run proves interfaces and failures, not LLM quality.

export type Entry = { id: string; title: string; tags: string[] };
// Historical display labels only. The live screen lists and starts scenarios from the backend catalogue.
export const CATALOGUE: Entry[] = [
  { id: 'first-meeting-new-session', title: 'Знакомство → новая сессия', tags: ['doc22'] },
  { id: 'promise-vs-attractive-find', title: 'Обещание → привлекательная находка', tags: ['doc22'] },
  { id: 'same-nickname', title: 'Два человека с одинаковым ником', tags: ['doc22'] },
  { id: 'wrong-explanation', title: 'Красивое, но неверное объяснение', tags: ['doc22'] },
  { id: 'quarrel-then-help', title: 'Ссора → совместное действие', tags: ['doc22'] },
  { id: 'declined-challenge-silence', title: 'Отклонённое испытание → тишина', tags: ['doc22'] },
  { id: 'two-experiences-development', title: 'Два опыта → развитие', tags: ['doc22'] },
  { id: 'injection-is-data', title: '«Забудь правила / раскрой ключ»', tags: ['doc22', 'security'] },
  { id: 'stop-during-generation', title: 'STOP во время генерации', tags: ['stop'] },
  { id: 'api-cutoff', title: 'Обрыв API', tags: ['failure'] },
  { id: 'restart-then-resume', title: 'Перезапуск → явное возобновление', tags: ['restart'] },
  { id: 'deletion-is-final', title: 'Удаление источника', tags: ['deletion'] },
];
export const titleOf = (scenarioId: string) => CATALOGUE.find((entry) => entry.id === scenarioId)?.title ?? scenarioId;

export const statusLabels: Record<LaboratoryReport['status'], string> = {
  pending: 'Ожидает', running: 'Выполняется', passed: 'Пройден', failed: 'Не пройден', cancelled: 'Остановлен', unknown: 'Исход неизвестен',
};
export const isLive = (report: LaboratoryReport) => report.status === 'pending' || report.status === 'running';

export type Summary = { kind: 'mock' | 'unmarked'; verdict: string; checks: number; failed: number; seed: string; digest: string; operatorEvents: number };
const pattern = /^MOCK (\S+): (\d+) checks, (\d+) failed; seed (.+); digest ([0-9a-f]{6,64})(?:; operator_events (\d+))?$/;
// The runner writes "MOCK <verdict>: N checks, M failed; seed S; digest D". Anything else is not guessed at.
export function parseSummary(summary: string): Summary | null {
  const match = pattern.exec(summary);
  if (!match) return null;
  return { kind: 'mock', verdict: match[1], checks: Number(match[2]), failed: Number(match[3]), seed: match[4], digest: match[5], operatorEvents: Number(match[6] ?? 0) };
}
// Latest report per scenario version, newest first overall.
export function latestPerScenario(reports: LaboratoryReport[]): Map<string, LaboratoryReport> {
  const out = new Map<string, LaboratoryReport>();
  for (const report of reports) {
    const known = out.get(report.scenario_id);
    if (!known || Date.parse(report.updated_at) > Date.parse(known.updated_at)) out.set(report.scenario_id, report);
  }
  return out;
}
export const sortReports = (reports: LaboratoryReport[]) =>
  [...reports].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at) || a.id.localeCompare(b.id));

export type Replay = { verdict: 'same' | 'differs' | 'not-comparable'; text: string };
// A replay with the same scenario, version and seed must give the same digest.
export function compare(a: LaboratoryReport, b: LaboratoryReport): Replay {
  const [x, y] = [parseSummary(a.summary), parseSummary(b.summary)];
  if (x?.operatorEvents || y?.operatorEvents) return { verdict: 'not-comparable', text: 'В запуск подавались наблюдения оператора: одного seed недостаточно для проверки воспроизводимости.' };
  if (a.scenario_id !== b.scenario_id || a.scenario_version !== b.scenario_version) return { verdict: 'not-comparable', text: 'Это разные сценарии или версии: сравнение не по повтору.' };
  if (!x || !y) return { verdict: 'not-comparable', text: 'Сводка одного из отчётов не распознана: сравнить нельзя.' };
  if (x.seed !== y.seed) return { verdict: 'not-comparable', text: `Разные seed (${x.seed} и ${y.seed}): это не повтор.` };
  return x.digest === y.digest ? { verdict: 'same', text: 'Повтор воспроизводится: отпечатки совпали.' } : { verdict: 'differs', text: 'Отпечатки различаются при одинаковом seed: повтор не воспроизводится.' };
}

// ---- starting a run ---------------------------------------------------------------------------------------
export const labOpen = (scope: Scope) => scope.memory_mode === 'test';
export const seedPattern = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$/;
export const REAL_PHRASE = 'РЕАЛЬНЫЙ API';
export type Start = { scenario: string; seed: string; real: boolean; typed: string; acknowledged: boolean };
export function startProblem(scope: Scope, start: Start, busy: boolean, catalogue: Entry[] = CATALOGUE): string | null {
  if (!labOpen(scope)) return 'Лаборатория работает только в тестовой памяти. Выберите тестовую область.';
  if (!catalogue.some((entry) => entry.id === start.scenario)) return 'Выберите сценарий из каталога.';
  if (!seedPattern.test(start.seed)) return 'Seed — латиница, цифры и . _ : - (до 64 символов): он фиксируется в отчёте.';
  if (busy) return 'Предыдущий запуск ещё идёт.';
  // A real call is paid and logged in the common ledger: never implied, never one click.
  if (start.real && (!start.acknowledged || start.typed !== REAL_PHRASE)) return `Реальный API платный: отметьте подтверждение и введите «${REAL_PHRASE}».`;
  return null;
}
// Canonical create payload; execution controls use shared owner-commands and Job CAS.
export const LAB_SCHEMA = { create: 'laboratory.create-args' } as const;
const TARGET = 'laboratory';
export function eventProblem(scope: Scope, text: string): string | null {
  if (!labOpen(scope)) return 'События подаются только в тестовую память.';
  if (!text.trim()) return 'Введите текст события.';
  if (text.length > 2000) return 'Событие длиннее 2000 символов.';
  return null;
}
// Creating the isolated area is addressed from the work scope but names the persona only; nothing is copied.
export const createCommand = (workScope: Scope, requestId: string): CreateCommand =>
  ({ type: 'laboratory.create', request_id: requestId, scope: workScope, target: TARGET,
    arguments: { schema_id: LAB_SCHEMA.create, data: { persona_id: workScope.persona_id } } });
// What a settings transfer never carries (mirrors the owner's list; shown, not enforced here).
export const NEVER_TRANSFERRED = ['Источники и сообщения', 'Эпизоды', 'Утверждения', 'Люди и встречи', 'Отношения', 'Темы и обещания', 'Состояние персонажа', 'Расходы и отчёты'] as const;
