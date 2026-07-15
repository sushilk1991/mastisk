import { FormEvent, KeyboardEvent, useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../api';
import { formatApiDateTime } from '../date';
import type { TweetThread, TweetThreadFeedback, View } from '../types';

interface Props {
  threadId: number;
  onNavigate: (view: View, id?: string) => void;
  onLoaded?: (thread: TweetThread | null) => void;
}

type FeedbackTarget = 'thread' | number;

function feedbackLabel(item: TweetThreadFeedback) {
  return item.target_tweet_index === null ? 'Full draft' : `Tweet ${item.target_tweet_index + 1}`;
}

function statusLabel(status: TweetThread['status']) {
  if (status === 'done') return 'Ready to publish';
  if (status === 'failed') return 'Needs attention';
  if (status === 'running') return 'Writing';
  return 'Queued';
}

function FeedbackPill({ item }: { item: TweetThreadFeedback }) {
  return (
    <div className="tw-feedback-pill" data-applied={item.applied_at ? 'true' : 'false'}>
      <div className="tw-feedback-pill-top">
        <span>{feedbackLabel(item)}</span>
        <span>{item.applied_at ? 'Applied' : 'Pending'}</span>
      </div>
      <div className="tw-feedback-pill-body">{item.body}</div>
    </div>
  );
}

function ThreadSkeleton() {
  return (
    <div className="tw-page tw-thread-page" aria-label="Loading thread draft">
      <div className="tw-detail-skeleton tw-detail-skeleton-short" />
      <div className="tw-detail-skeleton tw-detail-skeleton-title" />
      <div className="tw-detail-skeleton tw-detail-skeleton-copy" />
      <div className="tw-detail-skeleton tw-detail-skeleton-sheet" />
      <div className="tw-detail-skeleton tw-detail-skeleton-sheet" />
    </div>
  );
}

function submitOnShortcut(event: KeyboardEvent<HTMLFormElement>) {
  if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
    event.preventDefault();
    event.currentTarget.requestSubmit();
  }
}

export function TweetThreadView({ threadId, onNavigate, onLoaded }: Props) {
  const [thread, setThread] = useState<TweetThread | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [regenerating, setRegenerating] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [pollToken, setPollToken] = useState(0);
  const [threadFeedback, setThreadFeedback] = useState('');
  const [tweetFeedback, setTweetFeedback] = useState<Record<number, string>>({});
  const [openTweetFeedback, setOpenTweetFeedback] = useState<number | null>(null);
  const [feedbackBusy, setFeedbackBusy] = useState<FeedbackTarget | null>(null);
  const ref = useRef<TweetThread | null>(null);
  const tweetFeedbackRefs = useRef<Record<number, HTMLTextAreaElement | null>>({});
  const mounted = useRef(true);

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => { ref.current = thread; }, [thread]);
  useEffect(() => { if (thread) onLoaded?.(thread); }, [thread, onLoaded]);
  useEffect(() => () => onLoaded?.(null), [onLoaded]);

  useEffect(() => {
    setThread(null);
    setErr(null);
    setThreadFeedback('');
    setTweetFeedback({});
    setOpenTweetFeedback(null);
    ref.current = null;
    onLoaded?.(null);
  }, [threadId, onLoaded]);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const fetchOnce = async () => {
      try {
        const data = await api.tweets.get(threadId);
        if (cancelled) return;
        setThread(data);
        setErr(null);
        if (data.status === 'pending' || data.status === 'running') {
          timer = window.setTimeout(fetchOnce, 2000);
        }
      } catch (e) {
        if (cancelled) return;
        const apiErr = e instanceof ApiError ? e : null;
        if (apiErr?.status === 404) {
          setErr('This draft no longer exists.');
          return;
        }
        const last = ref.current?.status;
        if (!last) {
          setErr(e instanceof Error ? e.message : 'Mastisk could not reach this draft.');
          timer = window.setTimeout(fetchOnce, 5000);
        } else if (last === 'pending' || last === 'running') {
          timer = window.setTimeout(fetchOnce, 5000);
        } else {
          setErr(e instanceof Error ? e.message : 'Mastisk could not refresh this draft.');
        }
      }
    };
    fetchOnce();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [threadId, pollToken]);

  const copy = useCallback(async () => {
    if (!thread) return;
    const text = thread.thread.join('\n\n');
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setErr('Mastisk could not reach the clipboard. Copy the tweets individually instead.');
    }
  }, [thread]);

  const setReworking = useCallback(() => {
    setThread(previous => previous ? {
      ...previous,
      status: 'pending',
      error: null,
      finished_at: null,
    } : previous);
    setPollToken(token => token + 1);
  }, []);

  const regenerate = useCallback(async () => {
    if (!thread) return;
    if (thread.status === 'done' && !confirm('Regenerate this thread? The current draft will be replaced.')) return;
    setRegenerating(true);
    setErr(null);
    try {
      await api.tweets.regenerate(thread.id);
      if (!mounted.current) return;
      setReworking();
    } catch (e) {
      if (!mounted.current) return;
      setErr(e instanceof Error ? e.message : 'Mastisk could not regenerate this draft.');
    } finally {
      if (mounted.current) setRegenerating(false);
    }
  }, [thread, setReworking]);

  const submitFeedback = useCallback(async (target: FeedbackTarget, event: FormEvent) => {
    event.preventDefault();
    if (!thread) return;
    const text = target === 'thread' ? threadFeedback.trim() : (tweetFeedback[target] || '').trim();
    if (!text) return;
    setFeedbackBusy(target);
    setErr(null);
    try {
      await api.tweets.feedback(thread.id, {
        body: text,
        target_tweet_index: target === 'thread' ? null : target,
        rework: true,
      });
      if (!mounted.current) return;
      if (target === 'thread') {
        setThreadFeedback('');
      } else {
        setTweetFeedback(previous => ({ ...previous, [target]: '' }));
        setOpenTweetFeedback(null);
      }
      setReworking();
    } catch (e) {
      if (!mounted.current) return;
      setErr(e instanceof Error ? e.message : 'Mastisk could not apply that revision note.');
    } finally {
      if (mounted.current) setFeedbackBusy(null);
    }
  }, [thread, threadFeedback, tweetFeedback, setReworking]);

  const remove = useCallback(async () => {
    if (!thread) return;
    if (!confirm(`Delete thread #${thread.id}? This cannot be undone.`)) return;
    setDeleting(true);
    try {
      await api.tweets.delete(thread.id);
      if (!mounted.current) return;
      onNavigate('tweets');
    } catch (e) {
      if (!mounted.current) return;
      setErr(e instanceof Error ? e.message : 'Mastisk could not delete this draft.');
      setDeleting(false);
    }
  }, [thread, onNavigate]);

  const retryLoad = useCallback(() => {
    setErr(null);
    setPollToken(token => token + 1);
  }, []);

  const toggleTweetFeedback = useCallback((index: number) => {
    setOpenTweetFeedback((current) => {
      const next = current === index ? null : index;
      if (next !== null) {
        window.requestAnimationFrame(() => tweetFeedbackRefs.current[next]?.focus());
      }
      return next;
    });
  }, []);

  if (err && !thread) {
    return (
      <div className="tw-page tw-thread-page">
        <button className="tw-back-link" type="button" onClick={() => onNavigate('tweets')}>All drafts</button>
        <div className="tw-detail-error" role="alert">
          <span className="tw-note-number">!</span>
          <div>
            <h1>This draft could not be opened.</h1>
            <p>{err}</p>
            <div className="tw-detail-error-actions">
              <button className="tw-btn tw-btn-primary" type="button" onClick={retryLoad}>Try again</button>
              <button className="tw-btn tw-btn-secondary" type="button" onClick={() => onNavigate('tweets')}>Return to drafts</button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  if (!thread) return <ThreadSkeleton />;

  const title = thread.title || (thread.status === 'failed' ? 'Thread needs attention' : 'Thread in progress');
  const isWorking = thread.status === 'pending' || thread.status === 'running';
  const finishedAt = formatApiDateTime(thread.finished_at);
  const pendingFeedback = thread.feedback.filter(item => !item.applied_at);
  const appliedFeedback = thread.feedback.filter(item => item.applied_at);
  const totalCharacters = thread.thread.reduce((sum, tweet) => sum + tweet.length, 0);

  return (
    <div className="tw-page tw-thread-page">
      <button className="tw-back-link" type="button" onClick={() => onNavigate('tweets')}>All drafts</button>
      <span className="tw-sr-only" aria-live="polite">{copied ? 'Thread copied to clipboard.' : ''}</span>

      <header className="tw-detail-hero">
        <div className="tw-kicker">Draft #{thread.id}</div>
        <div className="tw-detail-heading">
          <div>
            <h1 className="tw-title">{title}</h1>
            {thread.angle && <p className="tw-angle">{thread.angle}</p>}
          </div>
          <div className="tw-actions" aria-label="Thread actions">
            {thread.thread.length > 0 && (
              <button className="tw-btn tw-btn-primary" type="button" onClick={copy}>
                {copied ? 'Copied to clipboard' : 'Copy thread'}
              </button>
            )}
            {(thread.status === 'done' || thread.status === 'failed') && (
              <button className="tw-btn tw-btn-secondary" type="button" onClick={regenerate} disabled={regenerating}>
                {regenerating ? 'Starting over…' : 'Regenerate'}
              </button>
            )}
            <details className="tw-more-actions">
              <summary className="tw-btn tw-btn-ghost">More</summary>
              <div>
                <button type="button" onClick={remove} disabled={deleting}>
                  {deleting ? 'Deleting draft…' : 'Delete draft'}
                </button>
              </div>
            </details>
          </div>
        </div>

        <div className="tw-meta-strip">
          <span data-status={thread.status}>{statusLabel(thread.status)}</span>
          <span>{thread.window_days}-day source window</span>
          <span>{formatApiDateTime(thread.created_at)}</span>
          {thread.model && <span>{thread.model}</span>}
          {finishedAt && <span>Finished {finishedAt}</span>}
        </div>

        {thread.theme && (
          <div className="tw-theme-line">
            <span>Brief</span>
            <p>{thread.theme}</p>
          </div>
        )}
        {thread.url && (
          <a className="tw-url" href={thread.url} target="_blank" rel="noreferrer">Open starting link</a>
        )}
      </header>

      {err && <p className="tw-form-error" role="alert">{err}</p>}
      {isWorking && (
        <div className="tw-working" role="status" aria-live="polite">
          <span />
          <div>
            <strong>{thread.thread.length > 0 ? 'Reworking your draft' : 'Building the thread'}</strong>
            <p>Mastisk is gathering evidence and checking the progression. This page updates automatically.</p>
          </div>
        </div>
      )}

      <div className="tw-workspace-grid">
        <main className="tw-thread-canvas">
          <div className="tw-canvas-heading">
            <div>
              <span className="tw-section-label">Working draft</span>
              <h2>The thread</h2>
            </div>
            <span>{thread.thread.length} tweets · {totalCharacters} characters</span>
          </div>

          {isWorking && thread.thread.length === 0 && (
            <div className="tw-writing-skeleton" aria-hidden="true">
              <span /><span /><span />
            </div>
          )}

          {thread.status === 'failed' && thread.thread.length === 0 && (
            <div className="tw-canvas-empty">
              <span className="tw-note-number">!</span>
              <div>
                <h3>The draft stopped before it produced a thread.</h3>
                <p>{thread.error || 'Try again, or adjust the brief before creating another draft.'}</p>
                <button className="tw-btn tw-btn-primary" type="button" onClick={regenerate} disabled={regenerating}>
                  {regenerating ? 'Restarting…' : 'Try this draft again'}
                </button>
              </div>
            </div>
          )}

          {thread.thread.length > 0 && (
            <section className="tw-thread-manuscript" aria-label="Tweet draft">
              {thread.thread.map((tweet, index) => {
                const itemFeedback = thread.feedback.filter(item => item.target_tweet_index === index);
                const formOpen = openTweetFeedback === index;
                const feedbackId = `tweet-feedback-${thread.id}-${index}`;
                return (
                  <article className="tw-tweet-segment" key={`${index}-${tweet.slice(0, 20)}`}>
                    <header className="tw-tweet-index">
                      <span>Tweet {String(index + 1).padStart(2, '0')}</span>
                      <span
                        data-near-limit={tweet.length > 250 ? 'true' : 'false'}
                        data-over-limit={tweet.length > 280 ? 'true' : 'false'}
                      >
                        {tweet.length}/280
                      </span>
                    </header>
                    <div className="tw-tweet-body">{tweet}</div>
                    <div className="tw-tweet-actions">
                      <button
                        className="tw-inline-action"
                        type="button"
                        aria-expanded={formOpen}
                        aria-controls={feedbackId}
                        onClick={() => toggleTweetFeedback(index)}
                        disabled={isWorking || feedbackBusy !== null}
                      >
                        {formOpen ? 'Close revision' : 'Revise this tweet'}
                      </button>
                    </div>

                    <div
                      className={`tw-tweet-feedback-shell ${formOpen ? 'is-open' : ''}`}
                      id={feedbackId}
                      aria-hidden={!formOpen}
                    >
                      <div>
                        <form className="tw-tweet-feedback" onSubmit={(event) => submitFeedback(index, event)} onKeyDown={submitOnShortcut}>
                          <label htmlFor={`${feedbackId}-input`}>What should change in tweet {index + 1}?</label>
                          <textarea
                            ref={(node) => { tweetFeedbackRefs.current[index] = node; }}
                            id={`${feedbackId}-input`}
                            value={tweetFeedback[index] || ''}
                            onChange={(event) => setTweetFeedback(previous => ({ ...previous, [index]: event.target.value }))}
                            placeholder="Make the claim more concrete, preserve the example, cut the throat-clearing…"
                            rows={3}
                            maxLength={2000}
                            disabled={!formOpen || isWorking || feedbackBusy !== null}
                          />
                          <button
                            className="tw-btn tw-btn-primary"
                            type="submit"
                            disabled={!formOpen || isWorking || feedbackBusy !== null || !(tweetFeedback[index] || '').trim()}
                          >
                            {feedbackBusy === index ? 'Applying revision…' : 'Rework this tweet'}
                          </button>
                        </form>
                      </div>
                    </div>

                    {itemFeedback.length > 0 && (
                      <div className="tw-tweet-feedback-list">
                        {itemFeedback.slice(-2).map(item => <FeedbackPill key={item.id} item={item} />)}
                      </div>
                    )}
                  </article>
                );
              })}
            </section>
          )}
        </main>

        <aside className="tw-editorial-rail" aria-label="Revision and evidence">
          <section className="tw-editorial-panel" aria-labelledby="thread-feedback-title">
            <span className="tw-section-label">Editorial pass</span>
            <h2 id="thread-feedback-title">Revise the whole arc</h2>
            <p>Describe the outcome you want. Mastisk will preserve the evidence and rewrite the progression.</p>
            <form className="tw-feedback-form" onSubmit={(event) => submitFeedback('thread', event)} onKeyDown={submitOnShortcut}>
              <label htmlFor="thread-feedback">Revision note</label>
              <textarea
                id="thread-feedback"
                value={threadFeedback}
                onChange={(event) => setThreadFeedback(event.target.value)}
                placeholder="Sharpen the hook, keep the Jensen example, and end on the unresolved test."
                rows={5}
                maxLength={2000}
                disabled={isWorking || feedbackBusy !== null}
              />
              <button
                className="tw-btn tw-btn-primary"
                type="submit"
                disabled={isWorking || feedbackBusy !== null || !threadFeedback.trim()}
              >
                {feedbackBusy === 'thread' ? 'Applying revision…' : 'Rework full draft'}
              </button>
            </form>
          </section>

          {(pendingFeedback.length > 0 || appliedFeedback.length > 0) && (
            <details className="tw-rail-disclosure" open>
              <summary>
                <span>Revision history</span>
                <span>{thread.feedback.length}</span>
              </summary>
              <div className="tw-feedback-ledger">
                {pendingFeedback.map(item => <FeedbackPill key={item.id} item={item} />)}
                {appliedFeedback.slice(-4).map(item => <FeedbackPill key={item.id} item={item} />)}
              </div>
            </details>
          )}

          {thread.sources.length > 0 && (
            <details className="tw-rail-disclosure">
              <summary>
                <span>Evidence used</span>
                <span>{thread.sources.length}</span>
              </summary>
              <div className="tw-source-list">
                {thread.sources.map((source, index) => {
                  const link = source.url && /^https?:\/\//.test(source.url) ? source.url : null;
                  return (
                    <div key={index} className="tw-source-row">
                      <div className="tw-source-title">{source.title || link || source.kind || 'Source'}</div>
                      {source.why && <div className="tw-source-why">{source.why}</div>}
                      {link && <a href={link} target="_blank" rel="noreferrer">Open source</a>}
                    </div>
                  );
                })}
              </div>
            </details>
          )}

          {thread.warnings.length > 0 && (
            <details className="tw-rail-disclosure tw-warning-disclosure" open>
              <summary>
                <span>Evidence notes</span>
                <span>{thread.warnings.length}</span>
              </summary>
              <ul className="tw-warning-list">
                {thread.warnings.map((warning, index) => <li key={index}>{warning}</li>)}
              </ul>
            </details>
          )}
        </aside>
      </div>
    </div>
  );
}
