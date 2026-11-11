import { Icon } from './icons';

/**
 * Turns a thrown request error into a sentence. The API client throws
 * `Error("500 Internal Server Error")`-style messages; shown raw they read as
 * a crash, so the status is kept only as a parenthetical for debugging.
 */
export function describeRequestError(e: unknown): string {
  const raw = e instanceof Error ? e.message.trim() : '';
  if (!raw || raw === 'failed') return 'Something went wrong. Try again in a moment.';
  if (/failed to fetch|networkerror|load failed/i.test(raw)) {
    return 'Mastisk isn’t reachable right now. Check that the server is running.';
  }
  const status = /^(\d{3})\b/.exec(raw);
  if (!status) {
    // The client unwraps a JSON `detail`, which for a crash is just the reason phrase.
    return /^internal server error$/i.test(raw) ? 'The server hit a problem handling this.' : raw;
  }
  const code = Number(status[1]);
  if (code === 404) return 'It may have been moved or deleted. (404)';
  if (code >= 500) return `The server hit a problem handling this. (${raw})`;
  return `The server didn’t accept this request. (${raw})`;
}

interface Props {
  title: string;
  detail?: string | null;
  onRetry?: () => void;
}

/** A failed request, shown in place: what failed, why, and a retry. */
export function InlineError({ title, detail, onRetry }: Props) {
  return (
    <div className="inline-error" role="alert">
      {Icon.alert}
      <div className="inline-error-text">
        <strong>{title}</strong>
        {detail && <span>{detail}</span>}
      </div>
      {onRetry && <button className="chip" type="button" onClick={onRetry}>Try again</button>}
    </div>
  );
}
