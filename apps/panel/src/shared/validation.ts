import type {
  Artifact, BriefingPage, EncounterPage, PersonPage, PromisePage, RelationshipPage, TopicPage, LaboratoryReportPage, CapabilityPage, CharacterStatePage, OpportunityPage, StateChangePage, StoryArcPage, ClaimPage, ConnectionPage, ConsequencePlan, CostCalculationPage, LLMAssignmentPage, LLMAttemptPage, TariffPage, DeliveryPage, EpisodePage, SummaryPage, OperationProjectionPage, OperatorSession, PanelCommand, PanelEvent,
  PermissionPage, ProfilePage, Receipt, WorkspacePage, RunSessionPage, Scope, SourcePage, StopReceipt, UtterancePage,
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
    typeof value.complete === 'boolean' && (value.next_cursor === null || id(value.next_cursor) || (typeof value.next_cursor === 'string' && /^t1\.[A-Za-z0-9_-]{1,509}$/.test(value.next_cursor))) &&
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

// M4-04: memory projections. Content may be absent for expired/deleted sources; never invented.
const availability = new Set(['available', 'expired', 'deleted', 'unavailable']);
const refs = (value: unknown) => Array.isArray(value) && value.length <= 200 && value.every((ref) =>
  object(ref) && id(ref.source_id) && version(ref.version) && isScope(ref.scope) && availability.has(String(ref.availability)));
const knowledge = new Set(['observed', 'reported', 'canon', 'hypothesis', 'interpretation']);
export function memorySources(value: unknown, selected: Scope): SourcePage {
  page(value, selected);
  demand(value.items.every((row) => typeof row.provenance === 'string' && id(row.source_type) && date(row.occurred_at) &&
    (row.content == null || text(row.content)) && (row.author_id == null || id(row.author_id)) &&
    availability.has(String(row.availability)) && object(row.retention) && id(row.retention.policy_id) &&
    (row.retention.expires_at === null || date(row.retention.expires_at)) &&
    ['read_allowed', 'api_transfer_allowed', 'public_disclosure_allowed'].every((key) => typeof row[key] === 'boolean') &&
    (row.availability === 'available' || (row.content == null && row.artifact == null))));
  return value as SourcePage;
}
export function episodes(value: unknown, selected: Scope): EpisodePage {
  page(value, selected);
  demand(value.items.every((row) => text(row.choice) && text(row.actual_outcome) && refs(row.sources) &&
    Array.isArray(row.participant_ids) && row.participant_ids.every(id) && ['observation', 'simulation'].includes(String(row.provenance))));
  return value as EpisodePage;
}
export function claims(value: unknown, selected: Scope): ClaimPage {
  page(value, selected);
  demand(value.items.every((row) => id(row.subject_id) && text(row.content) && knowledge.has(String(row.knowledge_type)) &&
    refs(row.sources) && typeof row.confidence === 'number' && row.confidence >= 0 && row.confidence <= 1 &&
    (row.conflict_ids === undefined || (Array.isArray(row.conflict_ids) && row.conflict_ids.every(id)))));
  return value as ClaimPage;
}
export function summaries(value: unknown, selected: Scope): SummaryPage {
  page(value, selected);
  demand(value.items.every((row) => text(row.content) && refs(row.sources) && date(row.period_start) && date(row.period_end) &&
    Array.isArray(row.episode_ids) && row.episode_ids.every(id)));
  return value as SummaryPage;
}
// A plan names exactly what will be removed; the screen refuses one that omits the chosen target.
export function plan(value: unknown, selected: Scope, planId: string): ConsequencePlan {
  demand(object(value) && value.id === planId && isScope(value.scope) && samePartition(selected, value.scope) &&
    version(value.version) && id(value.operation_type) && text(value.consequences, 4000) && date(value.expires_at) &&
    Array.isArray(value.dependencies) && value.dependencies.length <= 200 &&
    value.dependencies.every((dep) => object(dep) && id(dep.resource_id) && version(dep.version)));
  return value as ConsequencePlan;
}
export function operation(value: unknown, selected: Scope, operationId: string): OperationProjectionPage['items'][number] {
  demand(object(value) && value.operation_id === operationId && isScope(value.scope) && samePartition(selected, value.scope) &&
    operationStates.has(String(value.status)) && date(value.deadline) && version(value.version));
  return value as OperationProjectionPage['items'][number];
}
export function artifact(value: unknown, selected: Scope, artifactId: string): Artifact {
  demand(object(value) && value.id === artifactId && isScope(value.scope) && samePartition(selected, value.scope) &&
    object(value.reference) && value.reference.artifact_id === artifactId && typeof value.protected === 'boolean' &&
    (value.expires_at === null || date(value.expires_at)));
  return value as Artifact;
}

// M4-07: workspace projections. Secrets never appear here: only setting NAMES and verification states.
export function connections(value: unknown, selected: Scope): ConnectionPage {
  page(value, selected);
  demand(value.items.every((row) => text(row.name, 200) && id(row.adapter_id) && typeof row.configured === 'boolean' &&
    ['unverified', 'verified', 'failed'].includes(String(row.verification)) &&
    Array.isArray(row.capabilities) && row.capabilities.length <= 200 && row.capabilities.every(id)));
  return value as ConnectionPage;
}
export function capabilities(value: unknown, selected: Scope): CapabilityPage {
  page(value, selected);
  demand(value.items.every((row) => id(row.adapter_id) &&
    ['none', 'local_write', 'external_effect', 'public_output'].includes(String(row.effect)) &&
    ['ready', 'degraded', 'unavailable'].includes(String(row.health)) &&
    ['supported', 'unsupported', 'unknown'].includes(String(row.idempotency)) &&
    typeof row.cancellation_supported === 'boolean' && Array.isArray(row.required_permissions)));
  return value as CapabilityPage;
}
export function permissions(value: unknown, selected: Scope): PermissionPage {
  page(value, selected);
  demand(value.items.every((row) => id(row.principal_id) && id(row.capability_id) && id(row.operation_type) &&
    ['denied', 'prepare_only', 'operator_decision', 'automatic'].includes(String(row.level)) &&
    date(row.expires_at) && (row.revoked_at == null || date(row.revoked_at))));
  return value as PermissionPage;
}
export function workspaces(value: unknown, selected: Scope): WorkspacePage {
  page(value, selected);
  demand(value.items.every((row) => (row.active_agent_id === null || id(row.active_agent_id)) && version(row.policy_version) &&
    Array.isArray(row.profile_ids) && row.profile_ids.every(id) && Array.isArray(row.connection_ids) && row.connection_ids.every(id)));
  return value as WorkspacePage;
}

// M4-08: usage and price projections. Unknown stays null/unknown; nothing here can hold a prompt or a secret.
const moneyText = (value: unknown) => typeof value === 'string' && /^(0|[1-9][0-9]*)(\.[0-9]+)?$/.test(value) && value.length <= 64;
const tokenCount = (value: unknown) => value == null || (Number.isSafeInteger(value) && Number(value) >= 0);
export function llmAttempts(value: unknown, selected: Scope): LLMAttemptPage {
  page(value, selected);
  demand(value.items.every((row) => id(row.operation_id) && id(row.attempt_id) && id(row.role_id) && id(row.assignment_id) &&
    version(row.assignment_version) && id(row.requested_model_id) && (row.reported_model_id == null || id(row.reported_model_id)) &&
    ['running', 'completed', 'failed', 'cancelled', 'incomplete', 'unknown'].includes(String(row.status)) && date(row.started_at) &&
    (row.completed_at == null || date(row.completed_at)) && object(row.usage) &&
    ['complete', 'partial', 'unknown'].includes(String(row.usage.completeness)) &&
    ['provider', 'local_estimate', 'unknown'].includes(String(row.usage.source)) &&
    ['input_tokens', 'output_tokens', 'cached_input_tokens', 'reasoning_output_tokens'].every((key) => tokenCount((row.usage as Record<string, unknown>)[key])) &&
    (row.tariff_id == null) === (row.tariff_version == null)));
  return value as LLMAttemptPage;
}
export function costCalculations(value: unknown, selected: Scope): CostCalculationPage {
  page(value, selected);
  demand(value.items.every((row) => id(row.attempt_id) && ['complete', 'partial', 'unknown'].includes(String(row.completeness)) &&
    ['provider_usage', 'local_estimate', 'unknown'].includes(String(row.provenance)) &&
    (row.amount == null || moneyText(row.amount)) && (row.currency == null || /^[A-Z]{3}$/.test(String(row.currency))) &&
    // an unknown cost carries no amount; a known one names its currency
    (row.completeness !== 'unknown' || row.amount == null) && (row.amount == null || row.currency != null)));
  return value as CostCalculationPage;
}
export function tariffs(value: unknown, selected: Scope): TariffPage {
  page(value, selected);
  demand(value.items.every((row) => id(row.connection_id) && id(row.model_id) && Number.isSafeInteger(row.unit_tokens) && Number(row.unit_tokens) >= 1 &&
    /^[A-Z]{3}$/.test(String(row.currency)) && date(row.effective_at) &&
    ['input_rate', 'output_rate', 'cached_input_rate', 'reasoning_output_rate'].every((key) => row[key] == null || moneyText(row[key]))));
  return value as TariffPage;
}
export function llmAssignments(value: unknown, selected: Scope): LLMAssignmentPage {
  page(value, selected);
  demand(value.items.every((row) => id(row.role_id) && id(row.connection_id) && id(row.model_id) &&
    ['unverified', 'verified', 'failed'].includes(String(row.capability_verification)) &&
    (row.fallback_assignment_id == null || id(row.fallback_assignment_id))));
  return value as LLMAssignmentPage;
}

// M4-05: story and development projections. Free text is shown as plain text; nothing here executes.
const arcStatus = new Set(['open', 'paused', 'closed']);
const opportunityStatus = new Set(['DRAFT', 'SCREENED', 'OFFERED', 'ACCEPTED', 'REALIZING', 'RESOLVED', 'BLOCKED', 'DECLINED', 'EXPIRED', 'CANCELLED']);
const changeStatus = new Set(['PROPOSED', 'VALIDATING', 'READY', 'AWAITING_APPROVAL', 'APPLIED', 'REJECTED', 'CONFLICT', 'CANCELLED']);
export function storyArcs(value: unknown, selected: Scope): StoryArcPage {
  page(value, selected);
  demand(value.items.every((row) => arcStatus.has(String(row.status)) && text(row.direction, 4000) && text(row.boundaries, 4000) && refs(row.grounds) &&
    (row.cooldown_until == null || date(row.cooldown_until)) && (row.episode_ids === undefined || (Array.isArray(row.episode_ids) && row.episode_ids.every(id)))));
  return value as StoryArcPage;
}
export function opportunities(value: unknown, selected: Scope): OpportunityPage {
  page(value, selected);
  demand(value.items.every((row) => id(row.arc_id) && opportunityStatus.has(String(row.status)) && text(row.applicability, 4000) && text(row.boundaries, 4000) &&
    date(row.expires_at) && refs(row.grounds) && (row.choice == null || text(row.choice)) && (row.actual_outcome == null || text(row.actual_outcome)) &&
    (row.reason_code == null || text(row.reason_code, 500)) && (row.rolls === undefined || (Array.isArray(row.rolls) && row.rolls.length <= 3))));
  return value as OpportunityPage;
}
export function stateChanges(value: unknown, selected: Scope): StateChangePage {
  page(value, selected);
  demand(value.items.every((row) => id(row.proposal_id) && changeStatus.has(String(row.status)) && version(row.base_version) &&
    (row.new_version == null || version(row.new_version)) && object(row.patch) && text(row.interpretation, 4000) && text(row.expected_manifestation, 4000) &&
    typeof row.uncertainty === 'number' && row.uncertainty >= 0 && row.uncertainty <= 1 && refs(row.evidence)));
  return value as StateChangePage;
}
export function characterStates(value: unknown, selected: Scope): CharacterStatePage {
  page(value, selected);
  demand(value.items.every((row) => id(row.persona_id) && version(row.profile_version) && version(row.state_version) && object(row.values)));
  return value as CharacterStatePage;
}

// M4-06: laboratory reports. The summary is short anonymised text; no dialogue or secret is in it.
export function laboratoryReports(value: unknown, selected: Scope): LaboratoryReportPage {
  page(value, selected);
  demand(value.items.every((row) => id(row.scenario_id) && version(row.scenario_version) && id(row.operation_id) &&
    ['pending', 'running', 'passed', 'failed', 'cancelled', 'unknown'].includes(String(row.status)) && text(row.summary, 500) &&
    // a laboratory report lives only in test memory
    (row.scope as Scope).memory_mode === 'test' &&
    (row.report_artifact == null || (object(row.report_artifact) && id(row.report_artifact.artifact_id)))));
  return value as LaboratoryReportPage;
}

// M4-03: people, encounters, relationships, topics, promises. A nickname never identifies a person.
const confirmation = new Set(['confirmed', 'hypothesis', 'rejected']);
export function people(value: unknown, selected: Scope): PersonPage {
  page(value, selected);
  demand(value.items.every((row) => id(row.person_id) && Array.isArray(row.accounts) && row.accounts.length <= 200 &&
    row.accounts.every((account: unknown) => object(account) && id(account.platform_id) && text(account.account_id, 512) && text(account.display_name, 512) &&
      confirmation.has(String(account.confirmation)) && refs(account.evidence))));
  return value as PersonPage;
}
export function encounters(value: unknown, selected: Scope): EncounterPage {
  page(value, selected);
  demand(value.items.every((row) => Array.isArray(row.person_ids) && row.person_ids.length >= 1 && row.person_ids.every(id) && refs(row.sources)));
  return value as EncounterPage;
}
export function relationships(value: unknown, selected: Scope): RelationshipPage {
  page(value, selected);
  demand(value.items.every((row) => id(row.person_id) && object(row.aspects) && refs(row.grounds)));
  return value as RelationshipPage;
}
export function topics(value: unknown, selected: Scope): TopicPage {
  page(value, selected);
  demand(value.items.every((row) => id(row.topic_id) && ['open', 'reserved', 'awaiting_answer', 'answered', 'closed'].includes(String(row.status)) &&
    Array.isArray(row.participant_ids) && row.participant_ids.every(id) && refs(row.sources) && (row.answer == null || text(row.answer)) &&
    (row.cooldown_until == null || date(row.cooldown_until))));
  return value as TopicPage;
}
export function promises(value: unknown, selected: Scope): PromisePage {
  page(value, selected);
  demand(value.items.every((row) => ['open', 'fulfilled', 'withdrawn', 'unknown'].includes(String(row.status)) && text(row.content) && text(row.completion_criterion) &&
    Array.isArray(row.participant_ids) && row.participant_ids.length >= 1 && row.participant_ids.every(id) && refs(row.sources) &&
    (row.deadline == null || date(row.deadline)) && (row.closure_result == null || text(row.closure_result)) &&
    // a closed promise carries its closure
    (!['fulfilled', 'withdrawn'].includes(String(row.status)) || row.closure_result != null)));
  return value as PromisePage;
}

export function privateStream(value: unknown): import('./contracts.ts').StreamChunk | import('./contracts.ts').StreamTerminal {
  demand(object(value) && isScope(value.scope) && id(value.id) &&
    id(value.generation_id) && id(value.utterance_id) && id(value.attempt_id) &&
    Number.isSafeInteger(value.sequence) && Number(value.sequence) >= 1);
  demand(id(value.scope.run_id) && id(value.scope.agent_id));
  if (value.type === 'generation.chunk') {
    demand(value.preliminary === true && value.data_type === 'text' &&
      typeof value.data === 'string' && value.data.length <= 16000);
  } else {
    demand(value.type === 'generation.terminal' &&
      ['completed', 'rejected', 'cancelled', 'incomplete'].includes(String(value.status)) &&
      (value.saved_result_id === null || id(value.saved_result_id)));
    demand(value.status !== 'completed' || id(value.saved_result_id));
  }
  return value as import('./contracts.ts').StreamChunk | import('./contracts.ts').StreamTerminal;
}
export function profileMarkdown(value: unknown, selected: Scope): import('./contracts.ts').ProfileMarkdown {
  demand(object(value) && isScope(value.scope) && samePartition(selected, value.scope) &&
    id(value.profile_id) && value.profile_id === selected.persona_id && version(value.version) &&
    typeof value.markdown === 'string' && value.markdown.length <= 30000);
  return value as import('./contracts.ts').ProfileMarkdown;
}
export function profileValidation(value: unknown, selected: Scope, requestId: string): import('./contracts.ts').ProfileValidation {
  demand(object(value) && isScope(value.scope) && samePartition(selected, value.scope) &&
    value.request_id === requestId && typeof value.ready === 'boolean' &&
    typeof value.version_conflict === 'boolean' && typeof value.unchanged === 'boolean' &&
    Array.isArray(value.issues) && Array.isArray(value.blockers) &&
    (value.digest === null || typeof value.digest === 'string' && /^[a-f0-9]{64}$/.test(value.digest)) &&
    (value.current_version === null || version(value.current_version)));
  return value as import('./contracts.ts').ProfileValidation;
}
export function laboratoryCatalogue(value: unknown, selected: Scope): import('./contracts.ts').LaboratoryCatalogue {
  demand(object(value) && isScope(value.scope) && samePartition(selected, value.scope) &&
    value.mode === 'mock' && Array.isArray(value.scenarios) && value.scenarios.length <= 100 &&
    value.scenarios.every((s) => object(s) && id(s.scenario_id) && version(s.version) &&
      typeof s.title === 'string' && Array.isArray(s.tags) && s.tags.every((t) => typeof t === 'string')));
  return value as import('./contracts.ts').LaboratoryCatalogue;
}
export function gmSettings(value: unknown, selected: Scope): import('./contracts.ts').GmRuntimeStatePage {
  page(value, selected);
  demand(value.items.every((r) => typeof r.intensity === 'number' && r.intensity >= 0 && r.intensity <= 1 &&
    version(r.rules_version) && object(r.rules) && r.rules.schema_id === 'm3.rules' && object(r.rules.data) &&
    Array.isArray(r.rules.data.rules) && r.rules.data.rules.length <= 200 &&
    r.rules.data.rules.every((rule: unknown) => object(rule) && id(rule.rule_id) &&
      ['project', 'profile', 'session'].includes(String(rule.level)) && ['hard', 'soft', 'desired'].includes(String(rule.kind)) &&
      typeof rule.description === 'string' && rule.description.length > 0 && rule.description.length <= 1500)));
  return value as unknown as import('./contracts.ts').GmRuntimeStatePage;
}

// Full bytes use a separate bounded route, never an Artifact pathname or remote URL.
export function laboratoryDownload(value: unknown, selected: Scope): LaboratoryReportPage['items'][number] {
  laboratoryReports({ items: [value], limit: 50, next_cursor: null, complete: true }, selected);
  demand(object(value) && object(value.report_artifact));
  const ref = value.report_artifact;
  demand(isScope(ref.scope) && samePartition(selected, ref.scope) && version(ref.version) &&
    typeof ref.checksum === 'string' && /^[a-f0-9]{64}$/.test(ref.checksum) &&
    Number.isSafeInteger(ref.size_bytes) && Number(ref.size_bytes) > 0 && Number(ref.size_bytes) <= 2000000 &&
    ref.media_type === 'application/json');
  return value as LaboratoryReportPage['items'][number];
}

const transferKeys = new Set(['profile', 'gm_intensity', 'story_rules_version']);
function selection(value: unknown): value is string[] {
  return Array.isArray(value) && value.length > 0 && value.length <= 3 &&
    value.every((key) => typeof key === 'string' && transferKeys.has(key)) && new Set(value).size === value.length;
}
function transferRow(value: unknown, selected: Scope): asserts value is Record<string, unknown> {
  demand(object(value) && id(value.id) && version(value.version) && isScope(value.scope) && samePartition(value.scope, selected) &&
    selected.memory_mode === 'work' && isScope(value.source_scope) && value.source_scope.memory_mode === 'test' &&
    value.source_scope.workspace_id === selected.workspace_id && id(value.operator_id) &&
    typeof value.preview_digest === 'string' && /^[a-f0-9]{64}$/.test(value.preview_digest) &&
    selection(value.selected_keys) && Array.isArray(value.applied_keys) && value.applied_keys.length <= 3 &&
    value.applied_keys.every((key: unknown) => (value.selected_keys as unknown[]).includes(key)) &&
    new Set(value.applied_keys).size === value.applied_keys.length &&
    ['planned', 'completed', 'reconciliation_required', 'acknowledged'].includes(String(value.status)) &&
    (value.status !== 'completed' || value.applied_keys.length === value.selected_keys.length) &&
    (value.operation_id == null || id(value.operation_id)));
}
export function settingsTransfers(value: unknown, selected: Scope): import('./contracts.ts').SettingsTransferOperationPage {
  page(value, selected); value.items.forEach((row) => transferRow(row, selected));
  return value as unknown as import('./contracts.ts').SettingsTransferOperationPage;
}
export function settingsTransferStatus(value: unknown, selected: Scope, operationId: string): import('./contracts.ts').SettingsTransferStatus {
  demand(object(value) && isScope(value.scope) && samePartition(value.scope, selected) && value.operation_id === operationId &&
    (value.record === null || object(value.record)));
  if (value.record !== null) { transferRow(value.record, selected); demand(value.record.operation_id === operationId); }
  return value as unknown as import('./contracts.ts').SettingsTransferStatus;
}
export function settingsTransferPreview(value: unknown, command: import('./contracts.ts').EditCommand): import('./contracts.ts').SettingsTransferPreview {
  const args = command.arguments.data as Record<string, unknown>;
  demand(object(value) && id(value.id) && version(value.version) && isScope(value.scope) &&
    samePartition(command.scope, value.scope) && value.scope.memory_mode === 'work' &&
    isScope(value.source_scope) && isScope(args.source_scope) && samePartition(value.source_scope, args.source_scope) &&
    value.source_scope.memory_mode === 'test' && value.source_scope.workspace_id === value.scope.workspace_id &&
    value.request_id === command.request_id && value.operation_id === args.operation_id &&
    id(value.operator_id) && version(value.target_version) && value.target_version === command.expected_version &&
    typeof value.preview_digest === 'string' && /^[a-f0-9]{64}$/.test(value.preview_digest) &&
    date(value.expires_at) && selection(value.keys) && JSON.stringify(value.keys) === JSON.stringify(args.keys) &&
    Array.isArray(value.items) && value.items.length === value.keys.length &&
    value.items.every((item: unknown, index: number) => object(item) && item.key === (value.keys as unknown[])[index] &&
      'before' in item && 'after' in item && Array.isArray(item.changes) && item.changes.length <= 200 &&
      item.changes.every((text: unknown) => typeof text === 'string' && text.length <= 512) &&
      Array.isArray(item.blockers) && item.blockers.length <= 200 && item.blockers.every((text: unknown) => typeof text === 'string' && text.length <= 512)) &&
    typeof value.blocked === 'boolean' && value.blocked === value.items.some((item) => (item as { blockers: unknown[] }).blockers.length > 0) &&
    Array.isArray(value.never_transferred) && value.never_transferred.length <= 30 &&
    value.never_transferred.every((item: unknown) => typeof item === 'string' && item.length <= 512));
  return value as unknown as import('./contracts.ts').SettingsTransferPreview;
}

export function laboratoryRuns(value: unknown, selected: Scope): import('./contracts.ts').LaboratoryRuns {
  demand(object(value) && isScope(value.scope) && samePartition(value.scope, selected) &&
    Array.isArray(value.items) && value.items.length <= 200 && version(value.limit) && Number(value.limit) <= 200 &&
    (value.next_cursor === null || id(value.next_cursor)) && typeof value.complete === 'boolean');
  demand(value.items.every((row: unknown) => object(row) && isScope(row.scope) && samePartition(row.scope, selected) &&
    id(row.scope.run_id) && isScope(row.lab_scope) && row.lab_scope.memory_mode === 'test' &&
    row.lab_scope.workspace_id === selected.workspace_id && id(row.operation_id) && version(row.version) &&
    id(row.scenario_id) && version(row.scenario_version) &&
    ['QUEUED', 'RUNNING', 'WAITING', 'PAUSED', 'SUCCEEDED', 'FAILED', 'REJECTED', 'CANCELLED', 'EXPIRED', 'OUTCOME_UNKNOWN'].includes(String(row.status)) &&
    typeof row.ready === 'boolean' && (!row.ready || row.status === 'RUNNING') &&
    (row.virtual_time === null || date(row.virtual_time)) &&
    (row.accepted_events === null || Number.isSafeInteger(row.accepted_events) && Number(row.accepted_events) >= 0 && Number(row.accepted_events) <= 32)));
  return value as unknown as import('./contracts.ts').LaboratoryRuns;
}

export function ownerOperation(value: unknown, selected: Scope, operationId: string): import('./contracts.ts').OwnerOperation {
  demand(object(value) && value.id === operationId && isScope(value.scope) && samePartition(value.scope, selected) &&
    version(value.version) && id(value.request_id) && id(value.operator_id) && id(value.target_id) &&
    ['planned', 'completed', 'reconciliation_required'].includes(String(value.state)) &&
    Array.isArray(value.result_ids) && value.result_ids.length <= 200 && value.result_ids.every(id) &&
    Array.isArray(value.expected_result_ids) && value.expected_result_ids.length <= 200 && value.expected_result_ids.every(id) &&
    date(value.expires_at));
  demand(value.state === 'completed' ? object(value.receipt) && value.receipt.operation_id === operationId &&
    value.receipt.request_id === value.request_id && isScope(value.receipt.scope) && samePartition(value.receipt.scope, selected) &&
    value.receipt.status === 'completed' && id(value.receipt.resource_id) && version(value.receipt.version) : value.receipt === null);
  return value as unknown as import('./contracts.ts').OwnerOperation;
}


export function privateApiStatus(value: unknown, selected: Scope): import('./contracts.ts').PrivateApiStatus {
  demand(object(value) && isScope(value.scope) && samePartition(selected, value.scope) &&
    typeof value.enabled === 'boolean' && date(value.checked_at) &&
    Number.isSafeInteger(value.attempts_used) && Number(value.attempts_used) >= 0);
  if (value.enabled) {
    demand(['lifetime', 'utc_day'].includes(String(value.period)) &&
      Number.isSafeInteger(value.attempt_limit) && Number(value.attempt_limit) >= 1 &&
      value.attempts_remaining === Math.max(0, Number(value.attempt_limit) - Number(value.attempts_used)) &&
      (value.period === 'utc_day' ? date(value.resets_at) && Date.parse(String(value.resets_at)) > Date.parse(String(value.checked_at)) : value.resets_at === null));
  } else demand(value.period === null && value.attempt_limit === null && value.attempts_remaining === null &&
    value.attempts_used === 0 && value.resets_at === null);
  if (value.memory_processing === true) {
    demand(value.enabled === true && value.period === 'utc_day' &&
      Number.isSafeInteger(value.memory_daily_calls) && Number(value.memory_daily_calls) >= 3 &&
      Number(value.memory_daily_calls) <= 32 && Number(value.memory_daily_calls) < Number(value.attempt_limit) &&
      Number.isSafeInteger(value.memory_calls_used) && Number(value.memory_calls_used) >= 0 && Number(value.memory_calls_used) <= Number(value.attempts_used) &&
      value.dialogue_remaining === Math.max(0, Number(value.attempts_remaining) - Math.max(0, Number(value.memory_daily_calls)-Number(value.memory_calls_used))));
  } else demand((value.memory_processing === undefined || value.memory_processing === false) &&
    (value.memory_daily_calls === undefined || value.memory_daily_calls === 0) &&
    (value.memory_calls_used === undefined || value.memory_calls_used === 0) &&
    (value.dialogue_remaining == null || value.dialogue_remaining === value.attempts_remaining));
  return value as import('./contracts.ts').PrivateApiStatus;
}


export function memoryCycles(value: unknown, selected: Scope): import('./contracts.ts').MemoryCyclePage {
  page(value, selected);
  demand(value.items.every((r) =>
    Array.isArray(r.episode_ids) && r.episode_ids.length >= 1 && r.episode_ids.length <= 10 && r.episode_ids.every(id) &&
    Array.isArray(r.source_ids) && r.source_ids.length >= 1 && r.source_ids.length <= 20 && r.source_ids.every(id) &&
    typeof r.input_digest === 'string' && /^[a-f0-9]{64}$/.test(r.input_digest) &&
    (r.operation_id == null || id(r.operation_id)) &&
    ['queued', 'extract', 'consolidate', 'reflect', 'done'].includes(String(r.stage)) &&
    ['queued', 'running', 'completed', 'failed', 'unknown'].includes(String(r.status)) &&
    (r.status === 'completed') === (r.stage === 'done') &&
    (r.status !== 'queued' || r.stage === 'queued') &&
    (['failed','unknown'].includes(String(r.status)) ? typeof r.error_code === 'string' : r.error_code == null) &&
    Array.isArray(r.record_ids) && r.record_ids.length <= 100 && r.record_ids.every(id) &&
    date(r.created_at) && date(r.updated_at)));
  return value as import('./contracts.ts').MemoryCyclePage;
}
