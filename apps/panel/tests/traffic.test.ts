import test from 'node:test';
import assert from 'node:assert/strict';
import { egressLog, workSessionTranscript } from '../src/shared/validation.ts';
import { megabytes, utcDay } from '../src/ui/work/traffic.ts';
import type { EgressLog, Scope } from '../src/shared/contracts.ts';

const scope: Scope = { workspace_id: 'synthetic', persona_id: 'alpha', memory_space_id: 'alpha-work', memory_mode: 'work' };
const other: Scope = { ...scope, persona_id: 'beta', memory_space_id: 'beta-work' };
const row = (over: Record<string, unknown> = {}) => ({
  at: '2026-10-26T10:00:00Z', attempt: 'attempt-1', method: 'CONNECT', target: 'example.com:443', status: 'ok',
  reason: null, up: 100, down: 2000, seconds: 0.4, ...over,
});
const log = (over: Partial<EgressLog> = {}): EgressLog => ({
  scope, enabled: true, day: '2026-10-26', used_bytes: 2100, daily_limit_bytes: 1024 ** 3, turn_limit_bytes: 256 * 1024 ** 2,
  requests: 2, denied: 1, items: [row(), row({ status: 'denied', reason: 'EGRESS_DESTINATION_DENIED', up: 0, down: 0 })],
  complete: true, checked_at: '2026-10-26T10:05:00Z', ...over,
} as EgressLog);

test('the internet log is accepted only for the selected persona, day and closed statuses', () => {
  assert.deepEqual(egressLog(log(), scope, '2026-10-26'), log());
  assert.throws(() => egressLog(log({ scope: other }), scope, '2026-10-26'));
  assert.throws(() => egressLog(log(), scope, '2026-10-25'));
  assert.throws(() => egressLog(log({ items: [row({ status: 'bypassed' })] as EgressLog['items'] }), scope, '2026-10-26'));
  assert.throws(() => egressLog(log({ used_bytes: -1 }), scope, '2026-10-26'));
  assert.equal(egressLog(log({ enabled: false, items: [], requests: 0, denied: 0, used_bytes: 0 }), scope, '2026-10-26').enabled, false);
});

test('days are UTC and sizes read in megabytes', () => {
  const at = new Date('2026-10-26T01:30:00Z');
  assert.equal(utcDay(at), '2026-10-26');
  assert.equal(utcDay(at, 1), '2026-10-25');
  assert.equal(megabytes(1024 * 1024 * 1.5), '1.5 МБ');
  assert.equal(megabytes(300 * 1024 * 1024), '300 МБ');
});

test('a wake-up the agent set itself is a valid transcript entry', () => {
  const transcript = {
    scope, session_id: 'diary', session_version: 2, state: 'open', title: 'Самостоятельная работа', processed_sequence: 1,
    entries: [{ sequence: 1, speaker: 'wake', text: 'Будильник: проверить сборку', withheld: false, at: '2026-10-26T10:00:00Z' }],
    complete: true, checked_at: '2026-10-26T10:01:00Z',
  };
  assert.equal(workSessionTranscript(transcript, scope, 'diary').entries[0].speaker, 'wake');
});
