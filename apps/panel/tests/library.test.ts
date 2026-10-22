import test from 'node:test';
import assert from 'node:assert/strict';
import { workplaceFileContent, workplaceLibrary } from '../src/shared/validation.ts';
import { knowledge, skillOrder, sourcesLabel, statusLabels } from '../src/ui/library/model.ts';
import type { ManagedFile, Scope, SkillVersion, WorkplaceLibrary } from '../src/shared/contracts.ts';

const scope: Scope = { workspace_id: 'synthetic', persona_id: 'alpha', memory_space_id: 'alpha-work', memory_mode: 'work' };
const other: Scope = { ...scope, memory_space_id: 'alpha-test', memory_mode: 'test' };
const T = (minute: number) => `2026-10-21T00:${String(minute).padStart(2, '0')}:00Z`;
const ref = (id: string, version = 1, where = scope) => ({ artifact_id: id, version, scope: where, checksum: 'a'.repeat(64), size_bytes: 10, media_type: 'text/markdown' });
const source = { source_id: 'src-1', version: 1, scope, availability: 'available' };
const skill = (id: string, over: Partial<SkillVersion> = {}): SkillVersion => ({
  id, skill_id: id, scope, created_at: T(1), updated_at: T(1), version: 1, title: 'Навык ' + id, applicability: 'Когда нужно.',
  limitations: 'Нет.', method: ref('skill-method.' + id), helper_files: [], uses_capabilities: [], status: 'experimental', evidence: [],
  change_reason: 'Первая версия.', ...over,
} as SkillVersion);
const file = (id: string, over: Partial<ManagedFile> = {}): ManagedFile => ({
  id, scope, created_at: T(1), updated_at: T(1), version: 1, reference: ref(id), area: 'knowledge', path: id + '.md', title: 'Файл ' + id,
  kind: 'note', provenance: 'generated', dependencies: [], derived_from: [], access: { api_transfer: false, public_disclosure: false },
  state: 'active', ...over,
} as ManagedFile);
const library = (over: Partial<WorkplaceLibrary> = {}): WorkplaceLibrary => ({
  scope, skills: [skill('a')], files: [file('note-1'), file('skill-method.a', { area: 'skills', kind: 'method' })], complete: true, checked_at: T(9), ...over,
});

test('the library view is accepted only for the selected partition and closed shapes', () => {
  assert.deepEqual(workplaceLibrary(library(), scope), library());
  assert.throws(() => workplaceLibrary(library({ scope: other }), scope));
  assert.throws(() => workplaceLibrary(library({ files: [file('x', { scope: other, reference: ref('x', 1, other) })] }), scope));
  // A verified skill without evidence and a hidden file are not a valid projection.
  assert.throws(() => workplaceLibrary(library({ skills: [skill('v', { status: 'verified', evidence: [] })] }), scope));
  assert.throws(() => workplaceLibrary(library({ files: [file('gone', { state: 'deleted' })] }), scope));
  const verified = skill('v', { status: 'verified', evidence: [source] as SkillVersion['evidence'] });
  assert.equal(workplaceLibrary(library({ skills: [verified] }), scope).skills[0].status, 'verified');
});

test('working skills come first and knowledge kinds stay apart from methods', () => {
  const order = skillOrder([
    skill('old', { status: 'retired', updated_at: T(9) }),
    skill('new', { updated_at: T(5) }),
    skill('checked', { status: 'verified', evidence: [source] as SkillVersion['evidence'] }),
  ]).map((row) => row.id);
  assert.deepEqual(order, ['checked', 'new', 'old']);
  const parts = knowledge(library({
    files: [file('n'), file('h', { kind: 'hypothesis', provenance: 'assumption' }), file('m', { area: 'skills', kind: 'method' }), file('r', { area: 'reference', kind: 'material', provenance: 'read' })],
  }));
  assert.deepEqual([parts.notes, parts.hypotheses, parts.other].map((rows) => rows.map((row) => row.id)), [['n'], ['h'], ['r']]);
  assert.equal(statusLabels.experimental, 'Экспериментальный');
  assert.equal(sourcesLabel(file('s', { dependencies: [source] as ManagedFile['dependencies'] })), '1 источник');
});

test('file text is accepted only for the asked file, with text or withheld', () => {
  const content = (over: Record<string, unknown> = {}) => ({
    scope, file_id: 'note-1', version: 2, title: 'Заметка', area: 'knowledge', kind: 'note', provenance: 'generated',
    media_type: 'text/markdown', text: 'Текст.', withheld: false, truncated: false, checked_at: T(9), ...over,
  });
  assert.equal(workplaceFileContent(content(), scope, 'note-1').text, 'Текст.');
  assert.throws(() => workplaceFileContent(content(), scope, 'other'));
  assert.throws(() => workplaceFileContent(content({ scope: other }), scope, 'note-1'));
  assert.throws(() => workplaceFileContent(content({ withheld: true }), scope, 'note-1'));  // text with withheld
  assert.equal(workplaceFileContent(content({ withheld: true, text: null }), scope, 'note-1').withheld, true);
  assert.throws(() => workplaceFileContent(content({ withheld: true, text: null, truncated: true }), scope, 'note-1'));
});
