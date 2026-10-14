import test from 'node:test';
import assert from 'node:assert/strict';
import { formatTime } from '../src/ui/time.ts';

test('a malformed or missing date shows a dash and never throws', () => {
  for (const value of [undefined, null, '', 'never', 'not a date', 123, {}, [], '2026-13-45T00:00:00Z']) assert.equal(formatTime(value), '—', String(value));
  assert.equal(formatTime(undefined, 'medium'), '—');
});
test('a valid date is formatted in the Russian short and medium styles', () => {
  const short = formatTime('2026-10-14T09:05:07Z');
  const medium = formatTime('2026-10-14T09:05:07Z', 'medium');
  assert.match(short, /\d{2}\.\d{2}\.\d{4}, \d{2}:\d{2}$/);
  assert.match(medium, /\d{2}\.\d{2}\.\d{4}, \d{2}:\d{2}:\d{2}$/);
  assert.notEqual(short, medium);
});
