export function megabytes(bytes: number): string {
  return (bytes / 1024 / 1024).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0) + ' МБ';
}

// The UTC day ``back`` days before ``at``: the egress log is kept per UTC day.
export function utcDay(at: Date, back = 0): string {
  return new Date(at.getTime() - back * 86400000).toISOString().slice(0, 10);
}
