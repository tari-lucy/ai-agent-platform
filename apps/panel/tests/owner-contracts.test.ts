import test from 'node:test';
import assert from 'node:assert/strict';
import { applyProfile, configureGM, startLaboratory, validateProfile, acknowledgeReply, reflectSession } from '../src/shared/owner-commands.ts';
import { appendPreview } from '../src/shared/private-preview.ts';
import { PanelApi } from '../src/shared/api.ts';
import { privateStream } from '../src/shared/validation.ts';
import type { Scope, RunSession, GmRuntimeState, Utterance, StreamChunk, StreamTerminal } from '../src/shared/contracts.ts';

const scope: Scope = { workspace_id: 'synthetic', persona_id: 'alpha', memory_space_id: 'work-a', memory_mode: 'work',
  agent_id: 'primary', run_id: 'r1', session_id: 's1' };
const run = { scope, version: 17, session_id: 's1' } as RunSession;

test('persona commands preserve the complete document and use profile CAS plus exact accepted digest', () => {
  const markdown = `## Канон
Вымышленный профиль.
## Свойства
Сохранены все поля.`;
  const validated = validateProfile(scope, 5, markdown, 'preview-1');
  assert.equal(validated.target, 'alpha');
  assert.equal(validated.expected_version, 5);
  assert.deepEqual(validated.arguments, { schema_id: 'persona.validate-args', data: { markdown } });
  const applied = applyProfile(scope, 5, markdown, 'a'.repeat(64), 'apply-1');
  assert.deepEqual(applied.arguments.data, { markdown, revision_digest: 'a'.repeat(64) });
});
test('GM uses its own version; laboratory uses supervisor and catalogue versions independently', () => {
  const gm = configureGM(scope, { id: 'gm-runtime', version: 8 } as GmRuntimeState, { intensity: 0.5 }, 'gm-1');
  assert.equal(gm.expected_version, 8);
  assert.equal(gm.arguments.schema_id, 'gm.configure-args');
  const lab = startLaboratory(run, { scenario_id: 'same-nickname', version: 3, title: 'Имя', tags: [] },
    { ...scope, agent_id: undefined, run_id: undefined, session_id: undefined, memory_mode: 'test', memory_space_id: 'fresh-lab' },
    'seed', 'lab-1');
  assert.equal(lab.expected_version, 17);
  assert.equal(lab.target, 'laboratory');
  assert.equal(lab.arguments.schema_id, 'laboratory.start-args');
  assert.equal((lab.arguments.data as Record<string, unknown>).scenario_version, 3);
  assert.equal((lab.arguments.data as Record<string, unknown>).mode, 'mock');
});
test('receipt is a distinct explicit mutation; reflection has finite episode input', () => {
  const ack = acknowledgeReply({ id: 'u1', revision: 3, version: 4, scope } as Utterance, 'receipt-1');
  assert.equal(ack.type, 'dialogue.acknowledge');
  assert.deepEqual(ack.arguments, { revision: 3 });
  assert.equal(ack.expected_version, 4);
  assert.deepEqual(reflectSession(run, ['episode-1'], 'reflect-1').arguments, { episode_ids: ['episode-1'] });
});
const chunk = (sequence = 1, text = 'Привет'): StreamChunk => ({
  id: 'chunk-' + sequence, scope, created_at: '2026-10-14T00:00:00Z', type: 'generation.chunk',
  generation_id: 'op-1', utterance_id: 'u1', attempt_id: 'a1', sequence, data_type: 'text', data: text, preliminary: true,
});
test('private preview deduplicates, bounds text, marks gaps and never treats a draft as a delivery', () => {
  const one = appendPreview(undefined, privateStream(chunk()) as StreamChunk);
  assert.equal(appendPreview(one, chunk()), one);
  const gap = appendPreview(one, chunk(3, '!'));
  assert.equal(gap.text, 'Привет!');
  assert.equal(gap.incomplete, true);
  const huge = appendPreview(undefined, chunk(1, 'x'.repeat(5000)));
  assert.equal(huge.text.length, 4000);
  assert.equal(huge.incomplete, true);
  const terminal: StreamTerminal = { ...chunk(4), type: 'generation.terminal', status: 'completed', saved_result_id: 'u1' };
  assert.equal(appendPreview(gap, terminal).status, 'completed');
  assert.throws(() => privateStream({ ...terminal, saved_result_id: null }));
});
test('typed catalogue client rejects a foreign partition and real mode', async () => {
  const reply = (body: object) => new PanelApi((async () => new Response(JSON.stringify(body))) as typeof fetch);
  const catalogue = { scope, mode: 'mock', scenarios: [{ scenario_id: 'same-nickname', version: 1, title: 'Имя', tags: [] }] };
  assert.equal((await reply(catalogue).laboratoryCatalogue(scope)).scenarios[0].version, 1);
  await assert.rejects(() => reply({ ...catalogue, scope: { ...scope, persona_id: 'other' } }).laboratoryCatalogue(scope));
  await assert.rejects(() => reply({ ...catalogue, mode: 'real' }).laboratoryCatalogue(scope));
});
