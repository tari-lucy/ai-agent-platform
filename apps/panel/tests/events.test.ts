import test from 'node:test';
import assert from 'node:assert/strict';
import { connectEvents, eventsUrl, type ConnectionState } from '../src/shared/events.ts';
import type { PanelEvent, Scope } from '../src/shared/contracts.ts';

const scope: Scope = { workspace_id: 'synthetic', persona_id: 'alpha', memory_space_id: 'alpha-work', memory_mode: 'work' };
const wire: PanelEvent = { id: 'event-1', event_id: 'event-1', scope, resource_id: 'run-1',
  resource_version: 3, type: 'run.changed', created_at: '2026-10-14T00:00:00Z',
  occurred_at: '2026-10-14T00:00:00Z', causal_id: null, operation_id: null };
class FakeSocket {
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  sent: string[] = []; closed = 0;
  send(data: string) { this.sent.push(data); }
  close() { this.closed++; this.end(1006); }
  open() { this.onopen?.({} as Event); }
  message(value: unknown) { this.onmessage?.({ data: JSON.stringify(value) } as MessageEvent); }
  end(code: number) { this.onclose?.({ code } as CloseEvent); }
}
function harness() {
  const sockets: FakeSocket[] = [], changes: PanelEvent[] = [], states: ConnectionState[] = [];
  let refresh = 0, serial = 0;
  const tasks = new Map<number, { action: () => void; delay: number }>();
  const dispose = connectEvents({
    scope, url: 'ws://127.0.0.1:9876/api/v1/events',
    socket: () => { const socket = new FakeSocket(); sockets.push(socket); return socket; },
    schedule: (action, delay) => { const token = ++serial; tasks.set(token, { action, delay }); return token as unknown as ReturnType<typeof setTimeout>; },
    cancel: (token) => { tasks.delete(token as unknown as number); },
    refresh: () => refresh++, changed: (event) => changes.push(event), state: (state) => states.push(state),
  });
  return { sockets, changes, states, tasks, dispose, refresh: () => refresh,
    next: () => {
      const scheduled = [...tasks.entries()].find(([, task]) => task.delay <= 8000);
      if (!scheduled) return false;
      tasks.delete(scheduled[0]); scheduled[1].action(); return true;
    } };
}
test('subscription has exact scope and no credentials/cursor; handshake only refreshes projections', () => {
  const h = harness(); h.sockets[0].open();
  assert.deepEqual(JSON.parse(h.sockets[0].sent[0]), { scope, resource_ids: [] });
  assert.equal(h.refresh(), 1); h.dispose();
});
test('duplicates, older versions, foreign partition and malformed text do not update state', () => {
  const h = harness(); const socket = h.sockets[0]; socket.open();
  socket.message(wire); socket.message(wire);
  socket.message({ ...wire, event_id: 'old', resource_version: 2 });
  socket.message({ ...wire, event_id: 'foreign', scope: { ...scope, memory_mode: 'test' } });
  assert.equal(h.changes.length, 1);
  socket.message({ ...wire, event_id: 'next', resource_version: 4 });
  assert.equal(h.changes.length, 2);
  socket.message({ prompt: 'private-body' }); assert.equal(socket.closed, 1);
  h.dispose();
});
test('reconnect is bounded even if every handshake opens before disconnecting', () => {
  const h = harness();
  for (let i = 0; i < 6; i++) {
    const socket = h.sockets[i]; socket.open(); socket.end(1006);
    if (i < 5) assert.equal(h.next(), true);
  }
  assert.equal(h.sockets.length, 6); assert.equal(h.refresh(), 6);
  assert.equal(h.states.at(-1), 'offline'); assert.equal(h.next(), false);
  h.dispose(); assert.equal(h.tasks.size, 0);
});
test('auth rejection requires explicit retry and cannot start generation', () => {
  const h = harness(); h.sockets[0].end(1008);
  assert.equal(h.states.at(-1), 'denied'); assert.equal(h.next(), false);
  assert.equal(h.refresh(), 0); h.dispose();
});
test('logout/scope change stops pending reconnects and ignores late callbacks', () => {
  const h = harness(); const old = h.sockets[0];
  old.open(); old.end(1006); h.dispose();
  old.message(wire); old.open(); old.end(1006);
  assert.equal(h.changes.length, 0); assert.equal(h.refresh(), 1);
  assert.equal(h.tasks.size, 0);
});
test('WebSocket URL uses current same origin, wss under TLS, never query tokens', () => {
  assert.equal(eventsUrl({ protocol: 'https:', host: 'localhost:9876' }), 'wss://localhost:9876/api/v1/events');
  assert.equal(eventsUrl({ protocol: 'http:', host: '[::1]:9876' }), 'ws://[::1]:9876/api/v1/events');
});
