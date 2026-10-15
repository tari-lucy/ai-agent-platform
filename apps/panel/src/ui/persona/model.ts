import { applyProfile, validateProfile } from '../../shared/owner-commands.ts';
import type { CharacterState, CreateCommand, Profile, ProfileValidation, Scope } from '../../shared/contracts.ts';

// Pure persona-editor logic: line diff, static vs live values, validate → apply gate, commands. No I/O.
export const TEXT_LIMIT = 16000;

export type DiffLine = { op: 'same' | 'add' | 'del'; text: string };
// Line diff by longest common subsequence: what the operator sees before anything is applied.
export function lineDiff(before: string, after: string): DiffLine[] {
  const a = before.split('\n'); const b = after.split('\n');
  const table: number[][] = Array.from({ length: a.length + 1 }, () => new Array<number>(b.length + 1).fill(0));
  for (let i = a.length - 1; i >= 0; i--) for (let j = b.length - 1; j >= 0; j--) {
    table[i][j] = a[i] === b[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
  }
  const out: DiffLine[] = []; let i = 0; let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) { out.push({ op: 'same', text: a[i] }); i++; j++; }
    else if (table[i + 1][j] >= table[i][j + 1]) out.push({ op: 'del', text: a[i++] });
    else out.push({ op: 'add', text: b[j++] });
  }
  while (i < a.length) out.push({ op: 'del', text: a[i++] });
  while (j < b.length) out.push({ op: 'add', text: b[j++] });
  return out;
}
export const changedLines = (diff: DiffLine[]) => diff.filter((line) => line.op !== 'same').length;

export type Draft = { canon: string; style: string };
export type Problems = { canon?: string; style?: string; general?: string };
// Field errors sit next to the fields. An unchanged draft is not a change.
export function draftProblems(profile: Profile, draft: Draft): Problems {
  const found: Problems = {};
  for (const key of ['canon', 'style'] as const) {
    if (!draft[key].trim()) found[key] = 'Поле не может быть пустым.';
    else if (draft[key].length > TEXT_LIMIT) found[key] = `Не длиннее ${TEXT_LIMIT} символов.`;
  }
  if (!found.canon && !found.style && draft.canon === profile.canon && draft.style === profile.style) found.general = 'Изменений нет.';
  return found;
}
export const hasProblems = (problems: Problems) => Object.keys(problems).length > 0;

// ---- static vs live ------------------------------------------------------------------------------------
export type PropertyRow = { name: string; definition: string; initial: string | null; live: string | null; drifted: boolean };
const show = (value: unknown): string | null => (value === undefined ? null : typeof value === 'string' ? value : JSON.stringify(value));
const dataOf = (payload: { data: unknown } | undefined): Record<string, unknown> => {
  const data = payload?.data;
  return typeof data === 'object' && data !== null && !Array.isArray(data) ? (data as Record<string, unknown>) : {};
};
// The canon's starting values and the lived values stay in separate columns; "drifted" only reports a difference; its cause belongs to the history.
export function propertyRows(profile: Profile | undefined, state: CharacterState | undefined): PropertyRow[] {
  const raw = dataOf(profile?.property_definitions);
  const defs: Record<string, unknown> = Array.isArray(raw.properties) ? Object.fromEntries(raw.properties.filter((p): p is Record<string, unknown> => typeof p === 'object' && p !== null && typeof p.name === 'string').map((p) => [p.name, p])) : raw;
  const values = (value: { data: unknown } | undefined) => {
    const data = dataOf(value);
    return typeof data.values === 'object' && data.values !== null && !Array.isArray(data.values) ? data.values as Record<string, unknown> : data;
  };
  const [initial, live] = [values(profile?.initial_values), values(state?.values)];
  const names = [...new Set([...Object.keys(defs), ...Object.keys(initial), ...Object.keys(live)])].sort();
  return names.map((name) => {
    const [start, now] = [show(initial[name]), show(live[name])];
    return { name, definition: show(defs[name]) ?? 'определение не задано', initial: start, live: now, drifted: start !== null && now !== null && start !== now };
  });
}
export function stateNote(profile: Profile | undefined, state: CharacterState | undefined): string | null {
  if (!profile) return 'Принятый профиль недоступен.';
  if (!state) return 'Живое состояние недоступно: показаны только начальные значения канона.';
  return state.profile_version === profile.version ? null : `Состояние привязано к версии профиля v${state.profile_version}, принятая — v${profile.version}.`;
}

// ---- validate → apply ----------------------------------------------------------------------------------
// Existing canonical owner API; validation is a typed report, not a mutation receipt.
export const draftKey = (profile: Profile, draft: Draft) => JSON.stringify([profile.id, profile.version, draft.canon, draft.style]);
export type Checked = { key: string; digest: string | null };
export function checkedFrom(profile: Profile, draft: Draft, report: ProfileValidation): Checked {
  const ready = report.ready && !report.version_conflict && !report.unchanged &&
    report.current_version === profile.version && !report.issues.length && !report.blockers.length;
  return { key: draftKey(profile, draft), digest: ready ? report.digest : null };
}
export function applyBlocker(profile: Profile, draft: Draft, checked: Checked | null): string | null {
  if (!checked) return 'Сначала проверьте правку.';
  if (checked.key !== draftKey(profile, draft)) return 'Черновик или версия профиля изменились после проверки: проверьте заново.';
  if (!checked.digest) return 'Ядро не вернуло проверенную редакцию: применять нечего.';
  return null;
}
// Preserve every accepted definition/bound/initial value while editing canon and style.
export function profileDraftMarkdown(profile: Profile, draft: Draft): string {
  const block = (data: unknown) => '```json\n' + JSON.stringify(data, null, 2) + '\n```';
  return [
    '<!-- ai-agent-persona-profile/1 -->', '## Канон\n\n' + draft.canon,
    '## Стиль\n\n' + draft.style, '## Свойства\n\n' + block(profile.property_definitions.data),
    '## Начальные значения\n\n' + block(profile.initial_values.data),
    '## Границы развития\n\n' + block(profile.development_bounds.data),
  ].join('\n\n') + '\n';
}
export const validateCommand = (scope: Scope, profile: Profile, draft: Draft, requestId: string) =>
  validateProfile(scope, profile.version, profileDraftMarkdown(profile, draft), requestId);
export const applyCommand = (scope: Scope, profile: Profile, draft: Draft, digest: string, requestId: string) =>
  applyProfile(scope, profile.version, profileDraftMarkdown(profile, draft), digest, requestId);

// ---- create and copy -------------------------------------------------------------------------------------
export const idPattern = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
export type NewPersona = { mode: 'create' | 'copy'; persona: string; space: string; canon: string; style: string };
export const emptyNew: NewPersona = { mode: 'copy', persona: '', space: '', canon: '', style: '' };
export function newProblem(input: NewPersona, existing: string[]): string | null {
  if (!idPattern.test(input.persona)) return 'Идентификатор личности — латиница, цифры и . _ : - (до 128 символов).';
  if (existing.includes(input.persona)) return 'Личность с таким идентификатором уже есть.';
  if (!idPattern.test(input.space)) return 'Укажите область памяти для новой личности: она не может совпадать с чужой.';
  if (input.mode === 'create') {
    if (!input.canon.trim() || !input.style.trim()) return 'Для новой личности нужны канон и стиль.';
    if (input.canon.length > TEXT_LIMIT || input.style.length > TEXT_LIMIT) return `Канон и стиль — не длиннее ${TEXT_LIMIT} символов.`;
  }
  return null;
}
export function newCommand(scope: Scope, source: Profile | undefined, input: NewPersona, requestId: string): CreateCommand {
  const data: Record<string, string> = { persona_id: input.persona, memory_space_id: input.space };
  if (input.mode === 'copy' && source) data.source_profile_id = source.id;
  if (input.mode === 'create') { data.canon = input.canon; data.style = input.style; }
  return { type: input.mode === 'create' ? 'persona.create' : 'persona.copy', request_id: requestId, scope, target: 'persona',
    arguments: { schema_id: 'persona.' + input.mode, data } };
}
