const SQLITE_UTC = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?$/;

export function parseApiDate(value: string): Date | null {
  const normalized = SQLITE_UTC.test(value) ? `${value.replace(' ', 'T')}Z` : value;
  const parsed = new Date(normalized);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

export function formatApiDateTime(value: string | null | undefined): string | null {
  if (!value) return null;
  const parsed = parseApiDate(value);
  if (!parsed) return value;
  return parsed.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}
