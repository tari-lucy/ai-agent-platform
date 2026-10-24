import test from 'node:test';
import assert from 'node:assert/strict';
import { nativeAgents } from '../src/shared/validation.ts';
import { nativeAgent, nativeHref } from '../src/ui/opencode.ts';
import { PanelApi } from '../src/shared/api.ts';
import type { Scope } from '../src/shared/contracts.ts';

const scope: Scope = { workspace_id: 'synthetic', persona_id: 'alpha', memory_space_id: 'alpha-work', memory_mode: 'work' };
const other: Scope = { ...scope, persona_id: 'beacon', memory_space_id: 'beacon-work' };
const directory = 'aw_' + 'a'.repeat(64);
const agent = { directory, scope, label: 'alpha', active_runtime: true };

test('the bridge catalogue is accepted only with stable directories and at most 8 persons', () => {
  assert.equal(nativeAgents({ items: [agent] }).length, 1);
  assert.throws(() => nativeAgents({ items: [{ ...agent, directory: '../secret' }] }));
  assert.throws(() => nativeAgents({ items: Array.from({ length: 9 }, () => agent) }));
  assert.throws(() => nativeAgents({ items: [{ ...agent, scope: { persona_id: 'x' } }] }));
});

test('native addresses use the base64url directory and pick the person by partition', () => {
  const slug = Buffer.from(directory).toString('base64url');
  assert.equal(nativeHref(directory), '/opencode/' + slug + '/session');
  assert.equal(nativeHref(directory, 'ses 1'), '/opencode/' + slug + '/session/ses%201');
  const native = { agents: nativeAgents({ items: [agent] }) };
  assert.equal(nativeAgent(native, { ...scope, run_id: 'r1' } as Scope)?.directory, directory);
  assert.equal(nativeAgent(native, other), undefined);
  assert.equal(nativeAgent(null, scope), undefined);
});

test('the native window probe reads one fixed same-origin page and nothing else', async () => {
  const seen: string[] = [];
  const api = new PanelApi(async (input, init) => {
    seen.push(String(input) + ' ' + init?.redirect);
    return new Response('<html></html>', { status: String(input) === '/opencode/' ? 503 : 200 });
  });
  assert.equal(await api.nativeWindow(), false);
  assert.deepEqual(seen, ['/opencode/ error']);
});
