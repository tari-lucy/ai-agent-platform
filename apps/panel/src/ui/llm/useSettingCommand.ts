import { useState } from 'react';
import { ApiFailure, failureMessage, type PanelApi } from '../../shared/api.ts';
import type { PanelCommand } from '../../shared/contracts.ts';

// An uncertain POST is retried verbatim. Nothing is persisted in the browser.
export function useSettingCommand(api: PanelApi, refresh: () => void) {
  const [pending, setPending] = useState<PanelCommand | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  async function send(command: PanelCommand) {
    setPending(command); setBusy(true); setProblem(null); setSaved(false);
    try {
      const receipt = await api.command(command);
      if (receipt.status !== 'completed') {
        setProblem('Исход уточняется. Проверьте ту же операцию; новая правка пока закрыта.');
        return false;
      }
      setPending(null); setSaved(true); return true;
    } catch (error) {
      setProblem(failureMessage(error));
      if (error instanceof ApiFailure && !['NETWORK', 'OUTCOME_UNKNOWN', 'STALE_CONTEXT'].includes(error.code)) setPending(null);
      return false;
    } finally { setBusy(false); refresh(); }
  }
  return { pending, busy, problem, saved, send };
}
