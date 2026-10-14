import type { EventSubscription, PanelEvent, Scope } from './contracts.ts';
import { partition, permits } from './scope.ts';
import { event } from './validation.ts';

export type ConnectionState = 'connecting' | 'connected' | 'reconnecting' | 'offline' | 'denied';
interface Socket {
  onopen: ((ev: Event) => void) | null;
  onmessage: ((ev: MessageEvent) => void) | null;
  onclose: ((ev: CloseEvent) => void) | null;
  onerror: ((ev: Event) => void) | null;
  send(data: string): void; close(): void;
}
export interface EventOptions {
  scope: Scope;
  url: string;
  socket?: (url: string) => Socket;
  schedule?: (action: () => void, delay: number) => ReturnType<typeof setTimeout>;
  cancel?: (timer: ReturnType<typeof setTimeout>) => void;
  refresh: () => void;
  changed: (event: PanelEvent) => void;
  state: (state: ConnectionState) => void;
}
// Five automatic reconnects per lifetime (even if handshakes flap), then explicit retry.
export function connectEvents(options: EventOptions): () => void {
  let stopped = false, attempts = 0, socket: Socket | null = null;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let handshake: ReturnType<typeof setTimeout> | undefined;
  const schedule = options.schedule ?? setTimeout, cancel = options.cancel ?? clearTimeout;
  const seen = new Set<string>(), versions = new Map<string, number>();
  const factory = options.socket ?? ((url: string) => new WebSocket(url));
  function connect() {
    if (stopped) return;
    options.state(attempts ? 'reconnecting' : 'connecting');
    let current: Socket;
    try { current = factory(options.url); } catch { options.state('offline'); return; }
    socket = current;
    const live = () => !stopped && socket === current;
    handshake = schedule(() => { if (live()) current.close(); }, 10000);
    current.onopen = () => {
      if (!live()) return;
      if (handshake !== undefined) cancel(handshake);
      const subscription: EventSubscription = { scope: partition(options.scope), resource_ids: [] };
      current.send(JSON.stringify(subscription)); // cookie carried by browser; no token/CSRF in URL.
      options.state('connected'); options.refresh(); // reads only; never replay a command/model request.
    };
    current.onmessage = (message) => {
      if (!live()) return;
      let parsed: PanelEvent;
      try {
        if (typeof message.data !== 'string' || new TextEncoder().encode(message.data).length > 16384) throw new Error();
        parsed = event(JSON.parse(message.data));
      } catch { current.close(); return; }
      if (!permits(options.scope, parsed.scope) || seen.has(parsed.event_id)) return;
      const key = `${parsed.type}:${parsed.resource_id}`;
      if (parsed.resource_version !== null && parsed.resource_version <= (versions.get(key) ?? 0)) return;
      seen.add(parsed.event_id);
      if (seen.size > 128) seen.delete(seen.values().next().value!);
      if (parsed.resource_version !== null) {
        versions.set(key, parsed.resource_version);
        if (versions.size > 128) versions.delete(versions.keys().next().value!);
      }
      options.changed(parsed);
    };
    current.onerror = () => { if (live()) current.close(); };
    current.onclose = (closed) => {
      if (!live()) return;
      if (handshake !== undefined) cancel(handshake);
      socket = null;
      if (closed.code === 1008) { options.state('denied'); return; }
      if (attempts >= 5) { options.state('offline'); return; }
      options.state('reconnecting');
      const delay = Math.min(500 * 2 ** attempts++, 8000);
      timer = schedule(connect, delay);
    };
  }
  connect();
  return () => {
    stopped = true;
    if (timer !== undefined) cancel(timer);
    if (handshake !== undefined) cancel(handshake);
    socket?.close(); socket = null; seen.clear(); versions.clear();
  };
}
export function eventsUrl(location: Pick<Location, 'protocol' | 'host'>): string {
  return `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/api/v1/events`;
}
export const affectedResources: Record<PanelEvent['type'], readonly string[]> = {
  'run.changed': ['sessions'], 'job.changed': ['operations', 'jobs'],
  'utterance.changed': ['utterances'], 'delivery.changed': ['deliveries'],
  'memory.changed': ['sources', 'episodes', 'claims', 'summaries'],
  'relationship.changed': ['people', 'relationships', 'topics', 'promises'],
  'arc.changed': ['arcs', 'opportunities'], 'change.changed': ['changes', 'character-states'],
  'check.changed': ['checks'], 'connection.changed': ['connections', 'capabilities'],
  'usage.changed': ['llm-attempts', 'cost-calculations'],
};
