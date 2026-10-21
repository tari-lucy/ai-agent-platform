import test from 'node:test';
import assert from 'node:assert/strict';
import { workAttempts, workTasks } from '../src/shared/validation.ts';
import { abandonCommand, assignCommand, experienceOf, goalProblem, groups, spend, turn } from '../src/ui/work/model.ts';
import type { Episode, PrivateApiStatus, Scope, Ticket, WorkTask } from '../src/shared/contracts.ts';

const scope: Scope = { workspace_id: 'synthetic', persona_id: 'alpha', memory_space_id: 'alpha-work', memory_mode: 'work' };
const runScope: Scope = { ...scope, agent_id: 'primary', run_id: 'run-1', session_id: 'session-1' };
const T = (minute: number) => `2026-10-21T00:${String(minute).padStart(2, '0')}:00Z`;
const page = (items: unknown[]) => ({ items, limit: 50, next_cursor: null, complete: true });
const task = (id: string, over: Partial<WorkTask> = {}): WorkTask => ({
  id, task_id: id, scope, created_at: T(1), updated_at: T(1), version: 3, origin: 'assignment', goal: 'Цель ' + id, reason: '',
  state: 'active', waiting_ticket_ids: [], outcome: null, result_files: [], progress: [], ...over,
} as WorkTask);
const ticket = (id: string, next: Ticket['next_turn']) => ({ id, next_turn: next } as Ticket);

test('work projections are accepted only with coherent states', () => {
  assert.equal(workTasks(page([task('a')]), scope).items.length, 1);
  assert.throws(() => workTasks(page([task('w', { state: 'waiting', waiting_ticket_ids: [] })]), scope));
  assert.throws(() => workTasks(page([task('d', { state: 'done', outcome: null })]), scope));
  assert.throws(() => workTasks(page([task('x', { scope: { ...scope, memory_mode: 'test' } })]), scope));
  const attempt = { id: 'a.a0001', scope: runScope, created_at: T(1), updated_at: T(1), version: 1, task_id: 'a', attempt_no: 1,
    operation_id: 'op-1', capability_id: 'm3.activity', capability_version: 1, state: 'unknown' };
  assert.equal(workAttempts(page([attempt]), scope).items[0].state, 'unknown');
});

test('whose turn: the operator only when a waiting ticket is theirs; nobody moves while stopped', () => {
  const waiting = task('w', { state: 'waiting', waiting_ticket_ids: ['t1'] });
  assert.equal(turn(waiting, [ticket('t1', 'operator')], true), 'Нужен ваш ответ в обращении');
  assert.equal(turn(waiting, [ticket('t1', 'agent')], true), 'Ждёт ответа или возможности');
  assert.equal(turn(task('a'), [], false), 'Ход агента после запуска');
  assert.equal(turn(task('d', { state: 'done', outcome: 'Собственный вывод: x' }), [], true), 'Завершена');
  const { mine, working, done } = groups([waiting, task('a'), task('d', { state: 'abandoned', outcome: 'Отменено' })], [ticket('t1', 'operator')]);
  assert.deepEqual([mine, working, done].map((rows) => rows.map((row) => row.id)), [['w'], ['a'], ['d']]);
});

test('experience links follow recorded steps; spend shows only an enabled share', () => {
  const done = task('d', { progress: [{ at: T(2), attempt_id: 'd.a0002', capability_id: 'workplace.skills', status: 'succeeded', summary: 'ok' }] as WorkTask['progress'] });
  const episodes = [{ operation_id: 'work-d.a0002' }, { operation_id: 'work-other' }, { operation_id: 'op-dialogue' }] as Episode[];
  assert.deepEqual(experienceOf(done, episodes).map((row) => row.operation_id), ['work-d.a0002']);
  assert.equal(spend({ activity_daily_calls: 0 } as PrivateApiStatus), null);
  assert.equal(spend({ activity_daily_calls: 20, activity_calls_used: 3 } as PrivateApiStatus), 'Деятельность сегодня: 3 из 20 вызовов модели');
});

test('commands carry the stable area, the exact version and one request id', () => {
  assert.equal(goalProblem('  '), 'Опишите поручение.');
  const assign = assignCommand(runScope, ' Конспект ', '', 'req-1');
  assert.deepEqual([assign.type, assign.scope, assign.target, assign.arguments.data], ['work.assign', scope, 'work', { goal: 'Конспект', reason: '' }]);
  const drop = abandonCommand(runScope, task('a'), ' Не нужно ', 'req-2');
  assert.deepEqual([drop.type, drop.target, drop.expected_version, drop.arguments.data], ['work.abandon', 'a', 3, { task_id: 'a', reason: 'Не нужно' }]);
});
