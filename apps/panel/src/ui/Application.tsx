import { Alert, Button, Paper, PasswordInput, Stack, Text, Title } from '@mantine/core';
import { QueryClientProvider } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { HashRouter } from 'react-router';
import type { OperatorSession } from '../shared/contracts.ts';
import { ApiFailure, PanelApi, failureMessage } from '../shared/api.ts';
import { createProjectionCache } from '../shared/cache.ts';
import { Workspace } from './Workspace.tsx';

export function Application() {
  const [cache] = useState(createProjectionCache);
  const [auth, setAuth] = useState<OperatorSession | null>(null);
  const [booting, setBooting] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [api] = useState(() => new PanelApi(fetch, () => {
    cache.clear(); setAuth(null); setError('Сессия завершилась. Войдите снова.');
  }));
  useEffect(() => {
    let current = true;
    void api.restore().then((session) => {
      if (current) { api.authorize(session); setAuth(session); setError(null); }
    }).catch((failure) => {
      if (current && !(failure instanceof ApiFailure && failure.status === 401)) setError(failureMessage(failure));
    }).finally(() => { if (current) setBooting(false); });
    return () => { current = false; api.clear(); cache.clear(); };
  }, [api, cache]);
  useEffect(() => {
    if (!auth) return;
    let timer: ReturnType<typeof setTimeout>;
    const expiry = Date.parse(auth.expires_at);
    function checkExpiry() {
      const remaining = expiry - Date.now();
      if (remaining > 0) {
        timer = setTimeout(checkExpiry, Math.min(remaining, 3600000));
      } else {
        api.clear(); cache.clear(); setAuth(null); setError('Срок сессии истёк. Войдите снова.');
      }
    }
    timer = setTimeout(checkExpiry, Math.max(0, Math.min(expiry - Date.now(), 3600000)));
    return () => clearTimeout(timer);
  }, [api, auth, cache]);
  async function login(form: HTMLFormElement) {
    const password = String(new FormData(form).get('password') ?? '');
    form.reset(); // do not retain password in component/query/storage/URL state.
    setBusy(true); setError(null);
    try {
      if (new TextEncoder().encode(password).length > 1024) throw new ApiFailure('INVALID_ARGUMENTS');
      const session = await api.login(password);
      api.authorize(session); cache.clear(); setAuth(session);
    } catch (failure) { setError(failureMessage(failure)); }
    finally { setBusy(false); }
  }
  async function logout() {
    await api.logout(); api.clear(); cache.clear(); setAuth(null); setError(null);
  }
  return <QueryClientProvider client={cache}><HashRouter>
    {auth ? <Workspace key={auth.expires_at} api={api} auth={auth} onLogout={logout} /> :
      <main className="login-page">
        <div className="login-intro"><Text className="eyebrow">AI Agent Platform</Text>
          <Title order={1}>Рабочее пространство<br />для вашего персонажа</Title>
          <Text c="dimmed" mt="lg">Диалог, память и развитие — под вашим контролем.</Text></div>
        <Paper className="login-card" withBorder radius="lg" p="xl">
          <Stack gap="lg"><div><Title order={2}>Вход оператора</Title><Text c="dimmed" size="sm" mt="xs">Локальный доступ к панели управления.</Text></div>
            {booting ? <Text role="status">Проверяем сессию…</Text> :
              <form onSubmit={(event) => { event.preventDefault(); void login(event.currentTarget); }}>
                <PasswordInput name="password" label="Пароль" aria-label="Пароль" autoComplete="current-password" required maxLength={1024}
                  disabled={busy} visibilityToggleButtonProps={{ "aria-label": "Показать или скрыть пароль" }} mb="lg" />
                <Button type="submit" fullWidth disabled={busy}>{busy ? 'Проверяем пароль…' : 'Войти'}</Button>
              </form>}
            {error && <Alert color="orange" role="alert">{error}</Alert>}
            <Text size="xs" c="dimmed">Вход не запускает агента. После перезапуска запуск остаётся остановленным.</Text>
          </Stack>
        </Paper>
      </main>}
  </HashRouter></QueryClientProvider>;
}
