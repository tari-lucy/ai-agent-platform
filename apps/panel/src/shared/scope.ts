import type { Scope } from './contracts.ts';

export function partition(scope: Scope): Scope {
  return {
    workspace_id: scope.workspace_id, persona_id: scope.persona_id,
    memory_space_id: scope.memory_space_id, memory_mode: scope.memory_mode,
  };
}
export function scopeKey(scope: Scope): string {
  return JSON.stringify([
    scope.workspace_id, scope.persona_id, scope.memory_space_id, scope.memory_mode,
  ]);
}
export function samePartition(a: Scope, b: Scope): boolean {
  return scopeKey(a) === scopeKey(b);
}
export function permits(selected: Scope, received: Scope): boolean {
  return samePartition(selected, received) &&
    (['agent_id', 'run_id', 'session_id'] as const).every(
      (key) => !selected[key] || selected[key] === received[key],
    );
}
export function scopeLabel(scope: Scope): string {
  return `${scope.persona_id} · ${scope.memory_mode === 'test' ? 'ТЕСТ' : 'Рабочая память'} · ${scope.memory_space_id}`;
}
export const projectionKey = (resource: string, scope: Scope) =>
  ['projection', scopeKey(scope), resource] as const;
