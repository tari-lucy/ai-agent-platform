import type { AccountLink, EditCommand, Encounter, Person, PromiseRecord, RunSession, Relationship, SourceRef, Topic } from '../../shared/contracts.ts';

// Pure people logic: identity, confirmed accounts, history, relationship grounds, topics, promises. No I/O.
// A nickname never identifies a person; only a confirmed platform account does.
export const confirmationLabels: Record<AccountLink['confirmation'], string> = { confirmed: 'Подтверждён', hypothesis: 'Гипотеза', rejected: 'Отклонён' };
export const accountKey = (account: AccountLink) => `${account.platform_id}\u0000${account.account_id}`;
export const confirmedAccounts = (person: Person) => person.accounts.filter((a) => a.confirmation === 'confirmed');
export const otherAccounts = (person: Person) => person.accounts.filter((a) => a.confirmation !== 'confirmed');

const normal = (name: string) => name.trim().toLowerCase();
// Different people may share a display name; this names the collision, it never merges anyone.
export function sharedNames(people: Person[]): Map<string, string[]> {
  const byName = new Map<string, Set<string>>();
  for (const person of people) for (const account of person.accounts) {
    const key = normal(account.display_name);
    if (key) byName.set(key, (byName.get(key) ?? new Set()).add(person.person_id));
  }
  return new Map([...byName].filter(([, ids]) => ids.size > 1).map(([name, ids]) => [name, [...ids].sort()]));
}
export function collisionsOf(person: Person, shared: Map<string, string[]>): string[] {
  const names = new Set(person.accounts.map((account) => normal(account.display_name)));
  return [...shared].filter(([name]) => names.has(name)).flatMap(([, ids]) => ids.filter((id) => id !== person.person_id)).filter((id, index, all) => all.indexOf(id) === index).sort();
}
export const label = (person: Person) => person.accounts.find((a) => a.confirmation === 'confirmed')?.display_name ?? person.accounts[0]?.display_name ?? person.person_id;

export const involves = <T extends { participant_ids: string[] }>(rows: T[], personId: string) => rows.filter((row) => row.participant_ids.includes(personId));
export function encountersOf(rows: Encounter[], personId: string): Encounter[] {
  return rows.filter((row) => row.person_ids.includes(personId)).sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at) || a.id.localeCompare(b.id));
}
export const relationshipOf = (rows: Relationship[], personId: string): Relationship | undefined => rows.find((row) => row.person_id === personId);
export function groundsSummary(refs: SourceRef[]): string {
  const lost = refs.filter((ref) => ref.availability !== 'available').length;
  return lost ? `${refs.length} основ., недоступно: ${lost}` : `${refs.length} основ.`;
}

export const promiseLabels: Record<PromiseRecord['status'], string> = { open: 'Открыто', fulfilled: 'Выполнено', withdrawn: 'Снято', unknown: 'Исход неизвестен' };
export const topicLabels: Record<Topic['status'], string> = { open: 'Открыта', reserved: 'Зарезервирована', awaiting_answer: 'Ждёт ответа', answered: 'Отвечена', closed: 'Закрыта' };
// Open and doubtful promises first: a deleted conversation does not fulfil a promise.
export function orderPromises(rows: PromiseRecord[]): PromiseRecord[] {
  const rank = (row: PromiseRecord) => (row.status === 'unknown' ? 0 : row.status === 'open' ? 1 : 2);
  return [...rows].sort((a, b) => rank(a) - rank(b) || (Date.parse(a.deadline ?? '9999') - Date.parse(b.deadline ?? '9999')) || a.id.localeCompare(b.id));
}
export const overdue = (row: PromiseRecord, now: number) => row.status === 'open' && !!row.deadline && Date.parse(row.deadline) < now;

// ---- linking accounts ---------------------------------------------------------------------------------------
// ASSUMPTION (owner decision pending): payload schema ids of person.link / person.unlink are not defined yet.
export const PEOPLE_SCHEMA = { link: 'person.link', unlink: 'person.unlink' } as const;
export type Link = { platform: string; account: string; reason: string; confirmed: boolean };
export const emptyLink: Link = { platform: '', account: '', reason: '', confirmed: false };
const idPattern = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
export function ownerOf(people: Person[], platform: string, account: string): string | null {
  const key = `${platform}\u0000${account}`;
  return people.find((person) => confirmedAccounts(person).some((a) => accountKey(a) === key))?.person_id ?? null;
}
export function linkProblem(person: Person, people: Person[], link: Link): string | null {
  if (!idPattern.test(link.platform)) return 'Платформа — латиница, цифры и . _ : -.';
  if (!link.account.trim() || link.account.length > 512) return 'Укажите идентификатор аккаунта (до 512 символов).';
  if (!link.reason.trim()) return 'Укажите основание: почему это тот же человек.';
  if (link.reason.length > 1000) return 'Основание длиннее 1000 символов.';
  const owner = ownerOf(people, link.platform, link.account);
  // Two people are never fused by a link; merging is a separate, reviewed operation.
  if (owner && owner !== person.person_id) return `Этот аккаунт уже подтверждён у другого человека (${owner}). Слияние людей — отдельная операция.`;
  if (owner === person.person_id) return 'Этот аккаунт уже подтверждён у человека.';
  if (!link.confirmed) return 'Подтвердите, что это один и тот же человек.';
  return null;
}
const edit = (type: EditCommand['type'], run: RunSession, person: Person, schema: string, data: Record<string, string>, requestId: string): EditCommand =>
  ({ type, request_id: requestId, scope: run.scope, target: run.session_id, expected_version: person.version, arguments: { schema_id: schema, data } });
export const linkCommand = (run: RunSession, person: Person, link: Link, requestId: string) =>
  edit('person.link', run, person, PEOPLE_SCHEMA.link, { person_id: person.person_id, platform_id: link.platform, account_id: link.account.trim(), reason: link.reason.trim() }, requestId);
export const unlinkCommand = (run: RunSession, person: Person, account: AccountLink, reason: string, requestId: string) =>
  edit('person.unlink', run, person, PEOPLE_SCHEMA.unlink, { person_id: person.person_id, platform_id: account.platform_id, account_id: account.account_id, reason: reason.trim() }, requestId);
export const unlinkProblem = (reason: string) => (reason.trim() ? (reason.length > 1000 ? 'Основание длиннее 1000 символов.' : null) : 'Укажите основание отвязки.');
