import { FormEvent, KeyboardEvent, useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import { formatApiDateTime } from '../date';
import type { TweetThread, View } from '../types';

interface Props {
  onNavigate: (view: View, id?: string) => void;
}

const WINDOW_CHOICES = [1, 3, 7, 14, 30] as const;
const PAGE_SIZE = 50;

function statusLabel(status: TweetThread['status']) {
  if (status === 'done') return 'Ready';
  if (status === 'failed') return 'Needs attention';
  if (status === 'running') return 'Writing';
  return 'Queued';
}

function TweetListSkeleton() {
  return (
    <div className="tw-list-skeleton" aria-label="Loading recent drafts">
      {[0, 1, 2].map((item) => (
        <div className="tw-list-skeleton-row" key={item}>
          <span />
          <span />
          <span />
        </div>
      ))}
    </div>
  );
}

export function TweetThreadsView({ onNavigate }: Props) {
  const [rows, setRows] = useState<TweetThread[] | null>(null);
  const [theme, setTheme] = useState('');
  const [url, setUrl] = useState('');
  const [windowDays, setWindowDays] = useState<(typeof WINDOW_CHOICES)[number]>(7);
  const [includeWeb, setIncludeWeb] = useState(true);
  const [useBrowser, setUseBrowser] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const refresh = useCallback((preserveLoaded = false) => {
    setLoadError(null);
    api.tweets.list({ limit: PAGE_SIZE })
      .then((data) => {
        setRows((current) => {
          if (!preserveLoaded || !current || current.length <= PAGE_SIZE) return data;
          const refreshedIds = new Set(data.map((row) => row.id));
          return [...data, ...current.filter((row) => !refreshedIds.has(row.id))];
        });
        if (!preserveLoaded) setHasMore(data.length === PAGE_SIZE);
      })
      .catch((e) => setLoadError(e instanceof Error ? e.message : 'Could not load recent drafts.'));
  }, []);

  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => {
    if (!rows?.some((row) => row.status === 'pending' || row.status === 'running')) return;
    const timer = window.setInterval(() => refresh(true), 3000);
    return () => window.clearInterval(timer);
  }, [rows, refresh]);

  const submit = useCallback(async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setSubmitError(null);
    try {
      const created = await api.tweets.create({
        theme: theme.trim(),
        url: url.trim(),
        window_days: windowDays,
        include_web: includeWeb,
        use_browser_context: useBrowser,
      });
      onNavigate('tweet_thread', String(created.id));
    } catch (e) {
      setSubmitError(e instanceof Error ? e.message : 'Mastisk could not start this draft. Try again.');
      setBusy(false);
    }
  }, [theme, url, windowDays, includeWeb, useBrowser, onNavigate]);

  const loadMore = useCallback(async () => {
    const before = rows?.at(-1)?.id;
    if (before === undefined) return;
    setLoadingMore(true);
    setLoadError(null);
    try {
      const next = await api.tweets.list({ limit: PAGE_SIZE, before });
      setRows((current) => current ? [...current, ...next] : next);
      setHasMore(next.length === PAGE_SIZE);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : 'Could not load older drafts.');
    } finally {
      setLoadingMore(false);
    }
  }, [rows]);

  const submitShortcut = useCallback((event: KeyboardEvent<HTMLFormElement>) => {
    if ((event.metaKey || event.ctrlKey) && event.key === 'Enter' && !busy) {
      event.preventDefault();
      event.currentTarget.requestSubmit();
    }
  }, [busy]);

  const sourceSummary = `${windowDays}-day window · web ${includeWeb ? 'on' : 'off'} · browser ${useBrowser ? 'on' : 'off'}`;

  return (
    <div className="tw-index-page">
      <header className="tw-index-intro">
        <div className="tw-kicker">Thread studio</div>
        <h1>Turn one sharp observation into a thread.</h1>
        <p>
          Start with the claim you want to make. Mastisk brings the evidence, drafts the arc,
          and keeps every revision in one place.
        </p>
      </header>

      <form className="tw-compose" onSubmit={submit} onKeyDown={submitShortcut} aria-labelledby="tweet-compose-title">
        <div className="tw-compose-main">
          <div className="tw-compose-heading">
            <div>
              <span className="tw-section-label">New draft</span>
              <h2 id="tweet-compose-title">What are you trying to say?</h2>
            </div>
            <span className="tw-compose-count">{theme.length}/500</span>
          </div>

          <label className="tw-field" htmlFor="tweet-theme">
            <span className="tw-field-label">Core observation</span>
            <textarea
              id="tweet-theme"
              value={theme}
              onChange={(e) => setTheme(e.target.value)}
              placeholder="For example: agent reliability comes from the loop around the model, not the prompt alone."
              rows={5}
              maxLength={500}
              disabled={busy}
            />
            <span className="tw-field-help">
              Leave this blank if you want Mastisk to find a timely idea from your recent work.
            </span>
          </label>

          <label className="tw-field" htmlFor="tweet-url">
            <span className="tw-field-label">Starting link <span>optional</span></span>
            <input
              id="tweet-url"
              type="url"
              inputMode="url"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://x.com/... or any useful source"
              disabled={busy}
            />
          </label>

          <details className="tw-compose-sources">
            <summary>
              <span>Source controls</span>
              <span>{sourceSummary}</span>
            </summary>
            <div className="tw-compose-sources-body">
              <fieldset className="tw-window-fieldset">
                <legend>Look back</legend>
                <div className="tw-window-options" role="radiogroup" aria-label="Source window in days">
                  {WINDOW_CHOICES.map((days) => (
                    <label key={days}>
                      <input
                        type="radio"
                        name="tweet-source-window"
                        value={days}
                        checked={windowDays === days}
                        onChange={() => setWindowDays(days)}
                        disabled={busy}
                      />
                      <span>{days}d</span>
                    </label>
                  ))}
                </div>
              </fieldset>

              <div className="tw-source-toggles">
                <label>
                  <input
                    type="checkbox"
                    checked={includeWeb}
                    onChange={(e) => setIncludeWeb(e.target.checked)}
                    disabled={busy}
                  />
                  <span>
                    <strong>Recent web</strong>
                    <small>Look beyond your Mastisk corpus.</small>
                  </span>
                </label>
                <label>
                  <input
                    type="checkbox"
                    checked={useBrowser}
                    onChange={(e) => setUseBrowser(e.target.checked)}
                    disabled={busy}
                  />
                  <span>
                    <strong>Chrome context</strong>
                    <small>Read the supplied URL or your active tab.</small>
                  </span>
                </label>
              </div>
            </div>
          </details>

          {submitError && (
            <p className="tw-form-error" role="alert">{submitError}</p>
          )}

          <div className="tw-compose-footer">
            <p aria-live="polite">
              {busy ? 'Opening a draft and gathering evidence…' : 'You can revise the whole thread or any single tweet afterward.'}
            </p>
            <button className="tw-btn tw-btn-primary tw-generate-btn" type="submit" disabled={busy}>
              {busy ? 'Starting draft…' : 'Draft the thread'}
            </button>
          </div>
        </div>

        <aside className="tw-compose-note" aria-label="How thread drafting works">
          <span className="tw-note-number">01</span>
          <h3>Lead with tension.</h3>
          <p>A useful thread starts with a claim that can be argued, not a topic that can be summarized.</p>
          <ol>
            <li>Find recent evidence</li>
            <li>Choose one defensible angle</li>
            <li>Build a tweet-by-tweet progression</li>
          </ol>
        </aside>
      </form>

      <section className="tw-draft-archive" aria-labelledby="tweet-drafts-title">
        <div className="tw-archive-heading">
          <div>
            <span className="tw-section-label">Your workspace</span>
            <h2 id="tweet-drafts-title">Recent drafts</h2>
          </div>
          {rows && <span>{rows.length} shown</span>}
        </div>

        {rows === null && !loadError && <TweetListSkeleton />}

        {loadError && (
          <div className="tw-archive-message" role="alert">
            <div>
              <strong>Recent drafts did not load.</strong>
              <span>{loadError}</span>
            </div>
            <button className="tw-btn tw-btn-secondary" type="button" onClick={() => refresh()}>Try again</button>
          </div>
        )}

        {rows && rows.length === 0 && (
          <div className="tw-archive-empty">
            <span className="tw-note-number">00</span>
            <div>
              <h3>Your first draft starts above.</h3>
              <p>Give Mastisk one observation—or let it surface an idea from your recent work.</p>
            </div>
          </div>
        )}

        {rows && rows.length > 0 && (
          <ol className="tw-draft-list">
            {rows.map((row) => (
              <li key={row.id}>
                <button
                  type="button"
                  className="tw-draft-row"
                  onClick={() => onNavigate('tweet_thread', String(row.id))}
                >
                  <span className="tw-draft-id">#{row.id}</span>
                  <span className="tw-draft-copy">
                    <strong>{row.title || (row.status === 'failed' ? 'Thread needs attention' : 'Thread in progress')}</strong>
                    <span>{row.angle || row.theme || 'Mastisk is finding the angle.'}</span>
                  </span>
                  <span className="tw-draft-meta">
                    <span data-status={row.status}>{statusLabel(row.status)}</span>
                    <span>{row.thread.length} tweets</span>
                    <span>{formatApiDateTime(row.created_at)}</span>
                  </span>
                </button>
              </li>
            ))}
          </ol>
        )}

        {rows && rows.length > 0 && hasMore && (
          <div className="tw-load-more">
            <button className="tw-btn tw-btn-secondary" type="button" onClick={loadMore} disabled={loadingMore}>
              {loadingMore ? 'Loading older drafts…' : 'Load older drafts'}
            </button>
          </div>
        )}
      </section>
    </div>
  );
}
