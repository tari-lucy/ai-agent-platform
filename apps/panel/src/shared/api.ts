import type { OwnerOperation, LaboratoryRuns,
  SettingsTransferPreview, SettingsTransferStatus, SettingsTransferOperationPage,
  ProfileMarkdown, ProfileValidation, LaboratoryCatalogue, GmRuntimeStatePage, EditCommand, TicketMessagePage, TicketPage, CapabilityManifestPage, CapabilityBindingPage,
  ArtifactRef, LaboratoryReport, Artifact, BriefingPage, EncounterPage, PersonPage, PromisePage, RelationshipPage, TopicPage, LaboratoryReportPage, CapabilityPage, CharacterStatePage, OpportunityPage, StateChangePage, StoryArcPage, CostCalculationPage, LLMAssignmentPage, LLMAttemptPage, TariffPage, WorkspacePage, ClaimPage, ConnectionPage, ConsequencePlan, DeliveryPage, EpisodePage, OperationProjectionPage, PermissionPage, SummaryPage,
  OperatorLogin, OperatorSession, PanelCommand, ProfilePage, Receipt, RunSessionPage, Scope,
  SourcePage, StopCommand, StopReceipt, UtterancePage,
} from './contracts.ts';
import {
  apiLimits, recall, memoryCycles, privateApiStatus, ownerOperation,  laboratoryRuns,  settingsTransferPreview, settingsTransferStatus, settingsTransfers,
  laboratoryDownload, profileMarkdown, profileValidation, laboratoryCatalogue, gmSettings,
  artifact, briefings, capabilities, claims, connections, deliveries, episodes, inbound, memorySources, object, operation, operations, plan,
  permissions, profiles, receipt, workspaces,
  ticketMessages, tickets, capabilityManifests, capabilityBindings, workplaceLibrary, workTasks, workAttempts,
  characterStates, costCalculations, encounters, laboratoryReports, people, promises, relationships, topics, llmAssignments, llmAttempts, opportunities, stateChanges, storyArcs, tariffs, runs, session, stopReceipt, summaries, utterances,
} from './validation.ts';
import { partition } from './scope.ts';

const messages: Record<string, string> = {
  ACCESS_DENIED: 'Доступ запрещён. Войдите снова или проверьте разрешённую область.',
  VERSION_CONFLICT: 'Данные изменились. Обновите их перед повторной правкой.',
  IDEMPOTENCY_CONFLICT: 'Операция с этим идентификатором уже имеет другое содержимое.',
  STALE_CONTEXT: 'Контекст устарел. Получите актуальное состояние.',
  EXPIRED: 'Срок допуска истёк. Нужна новая проверка.',
  INVALID_ARGUMENTS: 'Запрос не принят. Проверьте поля.',
  CAPABILITY_UNAVAILABLE: 'Эта функция пока недоступна.',
  MODERATION_UNAVAILABLE: 'Модерация недоступна. Публичный выпуск закрыт.',
  RESOURCE_BUSY: 'Служба занята. Повторите действие позднее.',
  SERVICE_UNAVAILABLE: 'Служба недоступна или ещё не подключена.',
  OUTCOME_UNKNOWN: 'Результат неизвестен. Обновите состояние; не повторяйте действие вслепую.',
};
export class ApiFailure extends Error {
  readonly code: string;
  readonly status: number;
  constructor(code: string, status = 0) {
    super((Object.hasOwn(messages, code) ? messages[code] : undefined) ?? 'Связь с ядром потеряна или получен некорректный ответ.');
    this.code = code; this.status = status;
  }
}
export const failureMessage = (error: unknown) =>
  error instanceof ApiFailure ? error.message : 'Не удалось получить данные. Обновите состояние.';
type Fetcher = typeof fetch;
type ReadResource = 'api-limits' | 'memory-cycles' | 'private-api-status' | 'owner-operations' | 'laboratory-runs' | 'settings-transfers' | 'settings-transfer-status' | 'gm-settings' | 'laboratory-catalogue' | 'persona-markdown' | 'sessions' | 'personas' | 'utterances' | 'deliveries' | 'briefings' | 'sources' | 'operations' |
  'episodes' | 'claims' | 'summaries' | 'plans' | 'artifacts' | 'connections' | 'capabilities' | 'permissions' | 'workspaces' |
  'llm-attempts' | 'cost-calculations' | 'tariffs' | 'llm-assignments' | 'arcs' | 'opportunities' | 'changes' | 'character-states' | 'laboratory-reports' | 'people' | 'encounters' | 'relationships' | 'topics' | 'promises' | 'tickets' | 'ticket-messages' | 'capability-manifests' | 'capability-bindings' | 'workplace-library' | 'work-tasks' | 'work-attempts';

export class PanelApi {
  #auth: OperatorSession | null = null;
  #generation = 0;
  #requests = new Set<AbortController>();
  readonly fetcher: Fetcher;
  readonly unauthorized: () => void;
  constructor(fetcher: Fetcher = fetch, unauthorized: () => void = () => {}) {
    this.fetcher = fetcher.bind(globalThis); this.unauthorized = unauthorized;
  }
  get authenticated(): boolean { return this.#auth !== null; }
  authorize(value: OperatorSession): void {
    this.clear(); this.#auth = value;
  }
  clear(): void {
    this.#generation++; this.#auth = null;
    for (const controller of this.#requests) controller.abort();
    this.#requests.clear();
  }
  async #request(path: string, body?: unknown, signal?: AbortSignal, timeout = 15000,
    download?: { reference: ArtifactRef; scenario: string; version: number }, memoryExportScope?: Scope): Promise<unknown> {
    // Relative allowlisted paths only: no endpoints, credentials or redirects to other sites.
    if (!/^\/api\/v1\//.test(path) || path.includes('..') || path.includes('#')) throw new ApiFailure('INVALID_ARGUMENTS');
    const generation = this.#generation;
    const controller = new AbortController();
    if (signal?.aborted) controller.abort();
    const abort = () => controller.abort();
    signal?.addEventListener('abort', abort, { once: true });
    this.#requests.add(controller);
    const timer = setTimeout(abort, timeout);
    try {
      const response = await this.fetcher(path, {
        method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin',
        cache: 'no-store', redirect: 'error', signal: controller.signal,
        headers: body === undefined ? {} : {
          'Content-Type': 'application/json', ...(this.#auth ? { 'X-CSRF-Token': this.#auth.csrf_token } : {}),
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      if (generation !== this.#generation || controller.signal.aborted) throw new ApiFailure('STALE_CONTEXT');
      if (response.status === 401) {
        const wasAuthenticated = this.#auth !== null;
        this.clear(); if (wasAuthenticated) this.unauthorized();
        throw new ApiFailure('ACCESS_DENIED', 401);
      }
      if (response.status === 204 && response.ok) return null;
      const reader = response.body?.getReader();
      if (!reader) throw new ApiFailure('NETWORK');
      const chunks: Uint8Array[] = []; let length = 0;
      const limit = download && response.ok ? download.reference.size_bytes : 1048576;
      try {
        while (true) {
          const part = await reader.read();
          if (part.done) break;
          length += part.value.byteLength;
          if (length > limit) { await reader.cancel(); throw new ApiFailure('NETWORK'); }
          chunks.push(part.value);
        }
      } finally { reader.releaseLock(); }
      if (generation !== this.#generation || controller.signal.aborted) throw new ApiFailure('STALE_CONTEXT');
      const bytes = new Uint8Array(length); let offset = 0;
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
      if (download && response.ok) {
        const { reference } = download;
        if (length !== reference.size_bytes || response.headers.get('Content-Type')?.split(';')[0] !== 'application/json' ||
            response.headers.get('ETag') !== `"${reference.checksum}"`) throw new ApiFailure('NETWORK');
        const digest = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), (b) => b.toString(16).padStart(2, '0')).join('');
        if (generation !== this.#generation || controller.signal.aborted) throw new ApiFailure('STALE_CONTEXT');
        if (digest !== reference.checksum) throw new ApiFailure('NETWORK');
        const report: unknown = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
        if (!object(report) || report.mock !== true || report.scenario_id !== download.scenario ||
            report.scenario_version !== download.version) throw new ApiFailure('NETWORK');
        return new Blob([bytes], { type: 'application/json' });
      }
      if (memoryExportScope && response.ok) {
        const checksum = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)),
          (b) => b.toString(16).padStart(2, '0')).join('');
        if (generation !== this.#generation || controller.signal.aborted) throw new ApiFailure('STALE_CONTEXT');
        if (response.headers.get('Content-Type')?.split(';')[0] !== 'application/json' ||
            response.headers.get('ETag') !== `"${checksum}"`) throw new ApiFailure('NETWORK');
        const document: unknown = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
        if (!object(document) || document.schema_id !== 'memory.source-export' || document.schema_version !== '1.0') {
          throw new ApiFailure('NETWORK');
        }
        memorySources({ items: [document.source], complete: true, next_cursor: null }, memoryExportScope);
        return new Blob([bytes], { type: 'application/json' });
      }
      let value: unknown;
      try { value = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)); }
      catch { throw new ApiFailure('NETWORK'); }
      if (!response.ok) throw new ApiFailure(
        object(value) && typeof value.code === 'string' && Object.hasOwn(messages, value.code) ? value.code : 'NETWORK',
        response.status,
      );
      return value;
    } catch (error) {
      if (error instanceof ApiFailure) throw error;
      throw new ApiFailure('NETWORK');
    } finally {
      clearTimeout(timer); signal?.removeEventListener('abort', abort); this.#requests.delete(controller);
    }
  }
  async restore(): Promise<OperatorSession> { return session(await this.#request('/api/v1/auth/session')); }
  async login(password: string): Promise<OperatorSession> {
    const body: OperatorLogin = { password };
    return session(await this.#request('/api/v1/auth/login', body));
  }
  async logout(): Promise<void> { await this.#request('/api/v1/auth/logout', {}); }
  async #read(resource: ReadResource, scope: Scope, signal?: AbortSignal, cursor?: string, identifier?: string, order?: 'desc', utteranceId?: string): Promise<unknown> {
    const query = new URLSearchParams({ ...partition(scope) as Record<string, string>, limit: '50' });
    if (cursor !== undefined) {
      if (!(order === 'desc' ? /^t1\.[A-Za-z0-9_-]{1,509}$/ : /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/).test(cursor)) throw new ApiFailure('INVALID_ARGUMENTS');
      query.set('cursor', cursor);
    }
    if (order) query.set('order', order);
    if (utteranceId !== undefined) {
      if (!/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(utteranceId)) throw new ApiFailure('INVALID_ARGUMENTS');
      query.set('utterance_id', utteranceId);
    }
    if (identifier !== undefined && !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(identifier)) throw new ApiFailure('INVALID_ARGUMENTS');
    const path = identifier === undefined ? resource : `${resource}/${identifier}`;
    return this.#request(`/api/v1/${path}?${query}`, undefined, signal);
  }
  async previewSettingsTransfer(command: EditCommand): Promise<SettingsTransferPreview> {
    return settingsTransferPreview(await this.#request('/api/v1/laboratory/transfer-preview', command), command);
  }
  async transferStatus(scope: Scope, operationId: string, signal?: AbortSignal): Promise<SettingsTransferStatus> {
    return settingsTransferStatus(await this.#read('settings-transfer-status', scope, signal, undefined, operationId), scope, operationId);
  }
  async settingsTransfers(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<SettingsTransferOperationPage> {
    return settingsTransfers(await this.#read('settings-transfers', scope, signal, cursor), scope);
  }
  async memoryCycles(scope: Scope, signal?: AbortSignal): Promise<import('./contracts.ts').MemoryCyclePage> {
    return memoryCycles(await this.#read('memory-cycles', scope, signal), scope);
  }
  async apiLimits(scope: Scope, signal?: AbortSignal): Promise<import('./contracts.ts').ApiLimits> {
    return apiLimits(await this.#read('api-limits', scope, signal), scope);
  }
  async workTasks(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<import('./contracts.ts').WorkTaskPage> {
    return workTasks(await this.#read('work-tasks', scope, signal, cursor), scope);
  }
  async workAttempts(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<import('./contracts.ts').WorkAttemptPage> {
    return workAttempts(await this.#read('work-attempts', scope, signal, cursor), scope);
  }
  async workplaceLibrary(scope: Scope, signal?: AbortSignal): Promise<import('./contracts.ts').WorkplaceLibrary> {
    return workplaceLibrary(await this.#read('workplace-library', scope, signal), scope);
  }
  async privateApiStatus(scope: Scope, signal?: AbortSignal): Promise<import('./contracts.ts').PrivateApiStatus> {
    return privateApiStatus(await this.#read('private-api-status', scope, signal), scope);
  }
  async profileMarkdown(scope: Scope, signal?: AbortSignal): Promise<ProfileMarkdown> {
    return profileMarkdown(await this.#read('persona-markdown', scope, signal, undefined, scope.persona_id), scope);
  }
  async validateProfile(command: EditCommand): Promise<ProfileValidation> {
    if (command.type !== 'persona.validate' || command.target !== command.scope.persona_id) throw new ApiFailure('INVALID_ARGUMENTS');
    return profileValidation(await this.#request(`/api/v1/personas/${encodeURIComponent(command.target)}/validate`, command), command.scope, command.request_id);
  }
  async gmSettings(scope: Scope, signal?: AbortSignal): Promise<GmRuntimeStatePage> {
    return gmSettings(await this.#read('gm-settings', scope, signal), scope);
  }
  async laboratoryRuns(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<LaboratoryRuns> {
    return laboratoryRuns(await this.#read('laboratory-runs', scope, signal, cursor), scope);
  }
  async laboratoryCatalogue(scope: Scope, signal?: AbortSignal): Promise<LaboratoryCatalogue> {
    return laboratoryCatalogue(await this.#read('laboratory-catalogue', scope, signal), scope);
  }
  async laboratoryReportContent(scope: Scope, report: LaboratoryReport, signal?: AbortSignal): Promise<Blob> {
    const checked = laboratoryDownload(report, scope);
    const reference = { ...checked.report_artifact! };
    const query = new URLSearchParams(partition(scope) as Record<string, string>);
    return await this.#request(`/api/v1/laboratory-reports/${checked.id}/content?${query}`, undefined, signal, 15000,
      { reference, scenario: checked.scenario_id, version: checked.scenario_version }) as Blob;
  }
  async runs(scope: Scope, signal?: AbortSignal): Promise<RunSessionPage> {
    return runs(await this.#read('sessions', scope, signal), scope);
  }
  async profiles(scope: Scope, signal?: AbortSignal): Promise<ProfilePage> {
    return profiles(await this.#read('personas', scope, signal), scope);
  }
  // Dialogue reads newest event/creation first; opaque cursors continue towards older records.
  async utterances(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<UtterancePage> {
    return utterances(await this.#read('utterances', scope, signal, cursor, undefined, 'desc'), scope);
  }
  async deliveries(scope: Scope, signal?: AbortSignal, cursor?: string, utteranceId?: string): Promise<DeliveryPage> {
    const result = deliveries(await this.#read('deliveries', scope, signal, cursor, undefined, 'desc', utteranceId), scope);
    if (utteranceId !== undefined && result.items.some((row) => row.utterance_id !== utteranceId)) throw new ApiFailure('NETWORK');
    return result;
  }
  async briefings(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<BriefingPage> {
    return briefings(await this.#read('briefings', scope, signal, cursor), scope);
  }
  async inbound(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<SourcePage> {
    return inbound(await this.#read('sources', scope, signal, cursor, undefined, 'desc'), scope);
  }
  async operations(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<OperationProjectionPage> {
    return operations(await this.#read('operations', scope, signal, cursor, undefined, 'desc'), scope);
  }
  // M4-04 memory reads. A cursor continues the server order; it never widens rights.
  async recall(scope: Scope, query: string, signal?: AbortSignal): Promise<import('./contracts.ts').RecallResult> {
    if (!query.trim() || query.length > 1000) throw new ApiFailure('INVALID_ARGUMENTS');
    return recall(await this.#request('/api/v1/memory/recall', {
      scope: partition(scope), query, limit: 12, context_token_limit: 2000, purpose: 'api_transfer',
    }, signal), scope);
  }
  async memorySource(scope: Scope, identifier: string, signal?: AbortSignal): Promise<import('./contracts.ts').Source> {
    const row = await this.#read('sources', scope, signal, undefined, identifier);
    const source = memorySources({ items: [row], limit: 50, next_cursor: null, complete: true }, scope).items[0];
    if (source.id !== identifier) throw new ApiFailure('NETWORK');
    return source;
  }
  async sources(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<SourcePage> {
    return memorySources(await this.#read('sources', scope, signal, cursor), scope);
  }
  async episodes(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<EpisodePage> {
    return episodes(await this.#read('episodes', scope, signal, cursor), scope);
  }
  async claims(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<ClaimPage> {
    return claims(await this.#read('claims', scope, signal, cursor), scope);
  }
  async summaries(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<SummaryPage> {
    return summaries(await this.#read('summaries', scope, signal, cursor), scope);
  }
  async plan(scope: Scope, planId: string, signal?: AbortSignal): Promise<ConsequencePlan> {
    return plan(await this.#read('plans', scope, signal, undefined, planId), scope, planId);
  }
  async ownerOperation(scope: Scope, operationId: string, signal?: AbortSignal): Promise<OwnerOperation> {
    return ownerOperation(await this.#read('owner-operations', scope, signal, undefined, operationId), scope, operationId);
  }
  async operation(scope: Scope, operationId: string, signal?: AbortSignal): Promise<OperationProjectionPage['items'][number]> {
    return operation(await this.#read('operations', scope, signal, undefined, operationId), scope, operationId);
  }
  async artifact(scope: Scope, artifactId: string, signal?: AbortSignal): Promise<Artifact> {
    return artifact(await this.#read('artifacts', scope, signal, undefined, artifactId), scope, artifactId);
  }
  // M4-07 workspace reads: states and setting names only; the server never sends secret values.
  async connections(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<ConnectionPage> {
    return connections(await this.#read('connections', scope, signal, cursor), scope);
  }
  async capabilities(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<CapabilityPage> {
    return capabilities(await this.#read('capabilities', scope, signal, cursor), scope);
  }
  async permissions(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<PermissionPage> {
    return permissions(await this.#read('permissions', scope, signal, cursor), scope);
  }
  async workspaces(scope: Scope, signal?: AbortSignal): Promise<WorkspacePage> {
    return workspaces(await this.#read('workspaces', scope, signal), scope);
  }
  // M4-08 usage reads: ids, statuses, counts and money only; the server holds no prompt or reply here.
  async llmAttempts(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<LLMAttemptPage> {
    return llmAttempts(await this.#read('llm-attempts', scope, signal, cursor), scope);
  }
  async costCalculations(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<CostCalculationPage> {
    return costCalculations(await this.#read('cost-calculations', scope, signal, cursor), scope);
  }
  async tariffs(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<TariffPage> {
    return tariffs(await this.#read('tariffs', scope, signal, cursor), scope);
  }
  async llmAssignments(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<LLMAssignmentPage> {
    return llmAssignments(await this.#read('llm-assignments', scope, signal, cursor), scope);
  }
  // M4-05 story and development reads: server order, first page plus cursor.
  async arcs(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<StoryArcPage> {
    return storyArcs(await this.#read('arcs', scope, signal, cursor), scope);
  }
  async opportunities(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<OpportunityPage> {
    return opportunities(await this.#read('opportunities', scope, signal, cursor), scope);
  }
  async changes(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<StateChangePage> {
    return stateChanges(await this.#read('changes', scope, signal, cursor), scope);
  }
  async characterStates(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<CharacterStatePage> {
    return characterStates(await this.#read('character-states', scope, signal, cursor), scope);
  }
  // M4-06 laboratory reports (test memory only).
  async laboratoryReports(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<LaboratoryReportPage> {
    return laboratoryReports(await this.#read('laboratory-reports', scope, signal, cursor), scope);
  }
  // M4-03 people reads: server order, first page plus cursor.
  async people(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<PersonPage> {
    return people(await this.#read('people', scope, signal, cursor), scope);
  }
  async encounters(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<EncounterPage> {
    return encounters(await this.#read('encounters', scope, signal, cursor), scope);
  }
  async relationships(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<RelationshipPage> {
    return relationships(await this.#read('relationships', scope, signal, cursor), scope);
  }
  async topics(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<TopicPage> {
    return topics(await this.#read('topics', scope, signal, cursor), scope);
  }
  async promises(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<PromisePage> {
    return promises(await this.#read('promises', scope, signal, cursor), scope);
  }
  // AW-01 tickets owner reads; replies go through command().
  async tickets(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<TicketPage> {
    return tickets(await this.#read('tickets', scope, signal, cursor), scope);
  }
  async ticketMessages(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<TicketMessagePage> {
    return ticketMessages(await this.#read('ticket-messages', scope, signal, cursor), scope);
  }
  async capabilityManifests(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<CapabilityManifestPage> {
    return capabilityManifests(await this.#read('capability-manifests', scope, signal, cursor), scope);
  }
  async capabilityBindings(scope: Scope, signal?: AbortSignal, cursor?: string): Promise<CapabilityBindingPage> {
    return capabilityBindings(await this.#read('capability-bindings', scope, signal, cursor), scope);
  }
  async stop(scope: Scope, target: string, requestId: string = crypto.randomUUID()): Promise<StopReceipt> {
    const body: StopCommand = { type: 'agent.stop', request_id: requestId, scope: { ...scope, agent_id: target }, target };
    return stopReceipt(await this.#request(`/api/v1/agents/${encodeURIComponent(target)}/stop`, body, undefined, 6000), body.scope, requestId);
  }
  // Future screen owners may use this explicit port. Never retry mutations automatically.
  async command(command: PanelCommand): Promise<Receipt | StopReceipt> {
    return receipt(await this.#request('/api/v1/commands', command), command);
  }
  async memoryExport(scope: Scope, operationId: string): Promise<Blob> {
    if (!/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(operationId)) throw new ApiFailure('INVALID_ARGUMENTS');
    const query = new URLSearchParams(partition(scope) as Record<string, string>);
    const result = await this.#request(`/api/v1/memory/exports/${operationId}/content?${query}`,
      undefined, undefined, 15000, undefined, scope);
    if (!(result instanceof Blob)) throw new ApiFailure('NETWORK');
    return result;
  }
}
