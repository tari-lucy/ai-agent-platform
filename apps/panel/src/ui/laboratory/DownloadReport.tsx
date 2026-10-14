import { Alert, Button, Stack } from '@mantine/core';
import { useEffect, useRef, useState } from 'react';
import { failureMessage, type PanelApi } from '../../shared/api.ts';
import type { LaboratoryReport, Scope } from '../../shared/contracts.ts';
import { scopeKey } from '../../shared/scope.ts';

export function DownloadReport({ api, scope, report }: { api: PanelApi; scope: Scope; report: LaboratoryReport }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const active = useRef<AbortController | null>(null);
  const urls = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  const selected = scopeKey(scope);
  useEffect(() => {
    const allocated = urls.current;
    return () => {
      active.current?.abort();
      active.current = null;
      for (const [url, timer] of allocated) { clearTimeout(timer); URL.revokeObjectURL(url); }
      allocated.clear();
    };
  }, [api, selected, report.id, report.version, report.report_artifact?.checksum]);
  async function download() {
    if (active.current) return;
    const controller = new AbortController();
    active.current = controller;
    setBusy(true); setError(null);
    try {
      const blob = await api.laboratoryReportContent(scope, report, controller.signal);
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(blob);
      const timer = setTimeout(() => { URL.revokeObjectURL(url); urls.current.delete(url); }, 1000);
      urls.current.set(url, timer);
      const anchor = document.createElement('a');
      anchor.href = url; anchor.download = 'laboratory-report.json';
      document.body.append(anchor);
      try { anchor.click(); } finally { anchor.remove(); }
    } catch (failure) {
      if (!controller.signal.aborted) setError(failureMessage(failure));
    } finally {
      if (active.current === controller) { active.current = null; setBusy(false); }
    }
  }
  return <Stack gap={4} align="flex-end">
    <Button size="compact-xs" variant="light" disabled={busy || !report.report_artifact}
      onClick={() => void download()}>{busy ? 'Получаем отчёт…' : 'Скачать полный JSON'}</Button>
    {!report.report_artifact && <span>Полный отчёт ещё недоступен.</span>}
    {error && <Alert color="orange" role="alert" title="Отчёт не скачан">{error}</Alert>}
  </Stack>;
}
