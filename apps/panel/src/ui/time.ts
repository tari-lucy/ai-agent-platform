// One tolerant time formatter for every screen: a malformed or missing date shows a dash instead of crashing the page.
const formats = {
  short: new Intl.DateTimeFormat('ru-RU', { dateStyle: 'short', timeStyle: 'short' }),
  medium: new Intl.DateTimeFormat('ru-RU', { dateStyle: 'short', timeStyle: 'medium' }),
} as const;
export function formatTime(value: unknown, style: keyof typeof formats = 'short'): string {
  const at = typeof value === 'string' ? Date.parse(value) : Number.NaN;
  return Number.isFinite(at) ? formats[style].format(at) : '—';
}
