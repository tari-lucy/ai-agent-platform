import type {
  BriefingPage, DeliveryPage, OperationProjectionPage, OperatorSession, PanelCommand, PanelEvent,
  ProfilePage, Receipt, RunSessionPage, Scope, SourcePage, StopReceipt, UtterancePage,
} from './contracts.ts';
import { permits, samePartition } from './scope.ts';

// Defensive checks at the browser boundary. Canonical schemas and all rights stay server-owned.
export const object = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);
const id = (value: unknown): value is string =>
  typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(value);
const version = (value: unknown) => Number.isSafeInteger(value) && Number(value) >= 1;
const date = (value: unknown) => typeof value === 'string' && Number.isFinite(Date.parse(value));
export function isScope(value: unknown): value is Scope {
  return object(value) &&
    ['workspace_id', 'persona_id', 'memory_space_id'].every((key) => id(value[key])) &&
    (value.memory_mode === 'work' || value.memory_mode === 'test') &&
    ['agent_id', 'run_id', 'session_id'].every((key) => value[key] == null || id(value[key]));
}
function demand(condition: unknown): asserts condition {
  if (!condition) throw new Error('Invalid server projection');
}
export function session(value: unknown): OperatorSession {
  demand(object(value) && id(value.principal_id) && date(value.expires_at) &&
    typeof value.csrf_token === 'string' && /^[A-Za-z0-9_-]{43}$/.test(value.csrf_token) &&
    Array.isArray(value.scopes) && value.scopes.length >= 1 && value.scopes.length <= 8 &&
    value.scopes.every(isScope));
  return value as OperatorSession;
}
const states = new Set(['STOPPED', 'STARTING', 'ACTIVE', 'STOPPING', 'FAULTED']);
function page(value: unknown, selected: Scope): asserts value is Record<string, unknown> & { items: Record<string, unknown>[] } {
  demand(object(value) && Array.isArray(value.items) && value.items.length <= 50 &&
    typeof value.complete === 'boolean' && (value.next_cursor === null || id(value.next_cursor)) &&
    (value.limit === undefined || value.limit === 50));
  demand(value.items.every((row: unknown) => object(row) && isScope(row.scope) &&
    samePartition(selected, row.scope) && id(row.id) && version(row.version) &&
    (row.schema_version === undefined || row.schema_version === '1.0')));
}
export function runs(value: unknown, selected: Scope): RunSessionPage {
  page(value, selected);
  demand(value.items.every((row) =>
    id(row.agent_id) && id(row.run_id) && id(row.session_id) &&
    ['agent_id', 'run_id', 'session_id'].every((key) => !(row.scope as Scope)[key as keyof Scope] || (row.scope as Scope)[key as keyof Scope] === row[key]) &&
    states.has(String(row.state)) && ['OPEN', 'PAUSING', 'PAUSED'].includes(String(row.activity)) &&
    ['observation', 'autonomous'].includes(String(row.autonomy)) &&
    typeof row.voice_enabled === 'boolean' && typeof row.gm_paused === 'boolean' &&
    Number.isSafeInteger(row.control_epoch) && Number(row.control_epoch) >= 0 &&
    date(row.updated_at) && date(row.created_at) &&
    (row.unconfirmed_resource_ids === undefined || (Array.isArray(row.unconfirmed_resource_ids) &&
      row.unconfirmed_resource_ids.length <= 200 && row.unconfirmed_resource_ids.every(id)))));
  return value as RunSessionPage;
}
export function profiles(value: unknown, selected: Scope): ProfilePage {
  page(value, selected);
  demand(value.items.every((row) => row.persona_id === selected.persona_id &&
    typeof row.canon === 'string' && typeof row.style === 'string' &&
    id(row.accepted_revision_id) && object(row.initial_values) && object(row.development_bounds) &&
    object(row.property_definitions)));
  return value as ProfilePage;
}
export function stopReceipt(value: unknown, selected: Scope, requestId: string): StopReceipt {
  demand(object(value) && isScope(value.scope) && permits(selected, value.scope) &&
    value.request_id === requestId && states.has(String(value.state)) &&
    ['accepted', 'completed', 'waiting', 'rejected'].includes(String(value.status)) &&
    Number.isSafeInteger(value.control_epoch) && Number(value.control_epoch) >= 0 &&
    (value.run_id === null || id(value.run_id)) &&
    (value.state !== 'STOPPED' || (value.status === 'completed' &&
      (!Array.isArray(value.unconfirmed_resource_ids) || value.unconfirmed_resource_ids.length === 0))) &&
    (value.unconfirmed_resource_ids === undefined || (Array.isArray(value.unconfirmed_resource_ids) &&
      value.unconfirmed_resource_ids.length <= 200 && value.unconfirmed_resource_ids.every(id))));
  return value as StopReceipt;
}
const eventTypes = new Set([
  'run.changed', 'job.changed', 'utterance.changed', 'delivery.changed', 'memory.changed',
  'relationship.changed', 'arc.changed', 'change.changed', 'check.changed', 'connection.changed', 'usage.changed',
]);
export function event(value: unknown): PanelEvent {
  demand(object(value) && isScope(value.scope) && id(value.id) && id(value.event_id) &&
    id(value.resource_id) && eventTypes.has(String(value.type)) &&
    (value.schema_version === undefined || value.schema_version === '1.0') &&
    (value.resource_version === null || version(value.resource_version)) && date(value.occurred_at));
  return value as PanelEvent;
}

export function receipt(value: unknown, command: PanelCommand): Receipt | StopReceipt {
  if (command.type === 'agent.stop') return stopReceipt(value, command.scope, command.request_id);
  demand(object(value) && isScope(value.scope) && permits(command.scope, value.scope) &&
    value.request_id === command.request_id &&
    ['accepted', 'completed', 'waiting', 'rejected'].includes(String(value.status)) &&
    (value.version == null || version(value.version)) &&
    ['resource_id', 'operation_id'].every((key) => value[key] == null || id(value[key])));
  return value as Receipt;
}

// M4-02: dialogue projections. Same defensive stance: scope, ids, enumerations and sizes only.
const text = (value: unknown, max = 20000) => typeof value === 'string' && value.length <= max;
const moderation = new Set(['draft', 'checking', 'approved', 'blocked']);
const deliveryStates = new Set(['QUEUED', 'SENDING', 'DELIVERED', 'PARTIAL', 'FAILED', 'CANCELLED', 'UNKNOWN']);
const operationStates = new Set(['PROPOSED', 'AWAITING_APPROVAL', 'READY', 'QUEUED', 'RUNNING', 'WAITING',
  'SUCCEEDED', 'REJECTED', 'FAILED', 'CANCELLED', 'EXPIRED', 'OUTCOME_UNKNOWN']);
export function utterances(value: unknown, selected: Scope): UtterancePage {
  page(value, selected);
  demand(value.items.every((row) => id(row.utterance_id) && version(row.revision) && text(row.content) &&
    moderation.has(String(row.moderation_status)) && id(row.audience_id) && date(row.created_at) &&
    Array.isArray(row.channel_ids) && row.channel_ids.length >= 1 && row.channel_ids.length <= 200 &&
    row.channel_ids.every(id)));
  return value as UtterancePage;
}
export function deliveries(value: unknown, selected: Scope): DeliveryPage {
  page(value, selected);
  demand(value.items.every((row) => id(row.utterance_id) && version(row.revision) && id(row.channel_id) &&
    id(row.attempt_id) && deliveryStates.has(String(row.status)) && date(row.created_at)));
  return value as DeliveryPage;
}
export function briefings(value: unknown, selected: Scope): BriefingPage {
  page(value, selected);
  demand(value.items.every((row) => id(row.author_id) && text(row.content) &&
    ['context', 'canon_proposal', 'scenario'].includes(String(row.type)) &&
    ['active', 'cancelled', 'expired'].includes(String(row.status)) && date(row.expires_at)));
  return value as BriefingPage;
}
export function inbound(value: unknown, selected: Scope): SourcePage {
  page(value, selected);
  demand(value.items.every((row) => typeof row.provenance === 'string' && date(row.occurred_at) &&
    (row.content == null || text(row.content)) && (row.author_id == null || id(row.author_id)) &&
    ['available', 'expired', 'deleted', 'unavailable'].includes(String(row.availability))));
  return value as SourcePage;
}
export function operations(value: unknown, selected: Scope): OperationProjectionPage {
  page(value, selected);
  demand(value.items.every((row) => id(row.operation_id) && operationStates.has(String(row.status)) &&
    date(row.deadline) && (row.waiting_reason == null || text(row.waiting_reason, 500))));
  return value as OperationProjectionPage;
}
