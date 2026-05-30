import { FormEvent, useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../api';
import type { TweetThread, TweetThreadFeedback, View } from '../types';
import { Icon } from './icons';

interface Props {
  threadId: number;
  onNavigate: (view: View, id?: string) => void;
  onLoaded?: (thread: TweetThread | null) => void;
}

type FeedbackTarget = 'thread' | number;

function formatDate(value: string | null | undefined) {
  if (!value) return null;
  return new Date(value).toLocaleString();
}

function feedbackLabel(item: TweetThreadFeedback) {
  return item.target_tweet_index === null ? 'thread' : `tweet ${item.target_tweet_index + 1}`;
}

function FeedbackPill({ item }: { item: TweetThreadFeedback }) {
  return (
    <div className="tw-feedback-pill" data-applied={item.applied_at ? 'true' : 'false'}>
      <div className="tw-feedback-pill-top">
        <span>{feedbackLabel(item)}</span>
        <span>{item.applied_at ? 'applied' : 'pending'}</span>
      </div>
      <div className="tw-feedback-pill-body">{item.body}</div>
    </div>
  );
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
  }, [threadId]);

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
          setErr('thread not found');
          return;
        }
        const last = ref.current?.status;
        if (!last || last === 'pending' || last === 'running') {
          timer = window.setTimeout(fetchOnce, 5000);
        } else {
          setErr(e instanceof Error ? e.message : 'failed to refresh thread');
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
    const text = thread.thread.map((tweet, i) => `${i + 1}/${thread.thread.length} ${tweet}`).join('\n\n');
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setErr('clipboard unavailable');
    }
  }, [thread]);

  const setReworking = useCallback(() => {
    setThread(prev => prev ? {
      ...prev,
      status: 'pending',
      error: null,
      finished_at: null,
    } : prev);
    setPollToken(t => t + 1);
  }, []);

  const regenerate = useCallback(async () => {
    if (!thread) return;
    setRegenerating(true);
    setErr(null);
    try {
      await api.tweets.regenerate(thread.id);
      if (!mounted.current) return;
      setReworking();
    } catch (e) {
      if (!mounted.current) return;
      setErr(e instanceof Error ? e.message : 'regenerate failed');
    } finally {
      if (mounted.current) setRegenerating(false);
    }
  }, [thread, setReworking]);

  const submitFeedback = useCallback(async (target: FeedbackTarget, e: FormEvent) => {
    e.preventDefault();
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
        setTweetFeedback(prev => ({ ...prev, [target]: '' }));
        setOpenTweetFeedback(null);
      }
      setReworking();
    } catch (e) {
      if (!mounted.current) return;
      setErr(e instanceof Error ? e.message : 'feedback failed');
    } finally {
      if (mounted.current) setFeedbackBusy(null);
    }
  }, [thread, threadFeedback, tweetFeedback, setReworking]);

  const remove = useCallback(async () => {
    if (!thread) return;
    if (!confirm(`Delete thread #${thread.id}?`)) return;
    setDeleting(true);
    try {
      await api.tweets.delete(thread.id);
      if (!mounted.current) return;
      onNavigate('tweets');
    } catch (e) {
      if (!mounted.current) return;
      setErr(e instanceof Error ? e.message : 'delete failed');
      setDeleting(false);
    }
  }, [thread, onNavigate]);

  if (err && !thread) return <div className="view"><p style={{ color: 'var(--danger, crimson)' }}>{err}</p></div>;
  if (!thread) return <div className="view"><p style={{ color: 'var(--fg-faint)', fontFamily: 'var(--mono)', fontSize: 12 }}>loading...</p></div>;

  const title = thread.title || (thread.status === 'failed' ? 'Thread failed' : 'Thread in progress');
  const isWorking = thread.status === 'pending' || thread.status === 'running';
  const finishedAt = formatDate(thread.finished_at);
  const pendingFeedback = thread.feedback.filter(item => !item.applied_at);
  const appliedFeedback = thread.feedback.filter(item => item.applied_at);

  return (
    <div className="tw-page">
      <header className="tw-hero">
        <div className="tw-kicker">Tweet Thread · #{thread.id}</div>
        <div className="tw-hero-grid">
          <div>
            <h1 className="tw-title">{title}</h1>
            {thread.angle && <p className="tw-angle">{thread.angle}</p>}
          </div>
          <div className="tw-actions" aria-label="Thread actions">
            {thread.thread.length > 0 && (
              <button className="tw-btn tw-btn-secondary" onClick={copy}>
                <span>{copied ? 'Copied' : 'Copy'}</span>
              </button>
            )}
            {(thread.status === 'done' || thread.status === 'failed') && (
              <button className="tw-btn tw-btn-secondary" onClick={regenerate} disabled={regenerating}>
                <span>{regenerating ? 'Reworking' : 'Regenerate'}</span>
              </button>
            )}
            <button className="tw-btn tw-btn-ghost" onClick={remove} disabled={deleting}>
              <span>{deleting ? 'Deleting' : 'Delete'}</span>
            </button>
          </div>
        </div>

        <div className="tw-meta-strip">
          <span>{formatDate(thread.created_at)}</span>
          <span>{thread.window_days}d</span>
          <span data-status={thread.status}>{thread.status}</span>
          {thread.model && <span>{thread.model}</span>}
          {finishedAt && <span>finished {finishedAt}</span>}
        </div>

        {thread.theme && <div className="tw-theme">{thread.theme}</div>}
        {thread.url && (
          <a className="tw-url" href={thread.url} target="_blank" rel="noreferrer">{thread.url}</a>
        )}
        {err && <p className="tw-error">{err}</p>}
        {isWorking && <div className="tw-working">Reworking with the latest instructions...</div>}
      </header>

      <section className="tw-feedback-board" aria-labelledby="thread-feedback-title">
        <div>
          <div className="tw-section-label" id="thread-feedback-title">Thread feedback</div>
          <form className="tw-feedback-form" onSubmit={(e) => submitFeedback('thread', e)}>
            <textarea
              value={threadFeedback}
              onChange={(e) => setThreadFeedback(e.target.value)}
              placeholder="Make the hook sharper, cut tweet 7, add more lived detail..."
              rows={3}
              maxLength={2000}
              disabled={isWorking || feedbackBusy !== null}
            />
            <button
              className="tw-btn tw-btn-primary"
              type="submit"
              disabled={isWorking || feedbackBusy !== null || !threadFeedback.trim()}
            >
              <span className="tw-btn-icon">{Icon.arrow}</span>
              <span>{feedbackBusy === 'thread' ? 'Reworking' : 'Rework thread'}</span>
            </button>
          </form>
        </div>
        {(pendingFeedback.length > 0 || appliedFeedback.length > 0) && (
          <div className="tw-feedback-ledger">
            {pendingFeedback.map(item => <FeedbackPill key={item.id} item={item} />)}
            {appliedFeedback.slice(-3).map(item => <FeedbackPill key={item.id} item={item} />)}
          </div>
        )}
      </section>

      {thread.status === 'failed' && thread.error && (
        <p className="tw-error">{thread.error}</p>
      )}

      {thread.thread.length > 0 && (
        <section className="tw-thread-stack" aria-label="Tweet draft">
          {thread.thread.map((tweet, i) => {
            const itemFeedback = thread.feedback.filter(item => item.target_tweet_index === i);
            const formOpen = openTweetFeedback === i;
            return (
              <article
                className="tw-tweet-card"
                key={`${i}-${tweet.slice(0, 20)}`}
                style={{ animationDelay: `${Math.min(i, 8) * 55}ms` }}
              >
                <div className="tw-tweet-index">
                  <span>{i + 1}/{thread.thread.length}</span>
                  <span>{tweet.length}/280</span>
                </div>
                <div className="tw-tweet-body">{tweet}</div>
                <div className="tw-tweet-actions">
                  <button
                    className="tw-inline-action"
                    type="button"
                    onClick={() => setOpenTweetFeedback(formOpen ? null : i)}
                    disabled={isWorking || feedbackBusy !== null}
                  >
                    <span className="tw-btn-icon">{Icon.ask}</span>
                    <span>{formOpen ? 'Close feedback' : 'Feedback'}</span>
                  </button>
                </div>
                {formOpen && (
                  <form className="tw-tweet-feedback" onSubmit={(e) => submitFeedback(i, e)}>
                    <textarea
                      value={tweetFeedback[i] || ''}
                      onChange={(e) => setTweetFeedback(prev => ({ ...prev, [i]: e.target.value }))}
                      placeholder={`Feedback for tweet ${i + 1}`}
                      rows={2}
                      maxLength={2000}
                      disabled={isWorking || feedbackBusy !== null}
                    />
                    <button
                      className="tw-btn tw-btn-primary"
                      type="submit"
                      disabled={isWorking || feedbackBusy !== null || !(tweetFeedback[i] || '').trim()}
                    >
                      <span className="tw-btn-icon">{Icon.arrow}</span>
                      <span>{feedbackBusy === i ? 'Reworking' : 'Rework tweet'}</span>
                    </button>
                  </form>
                )}
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

      {thread.sources.length > 0 && (
        <section className="tw-panel">
          <div className="tw-section-label">Sources</div>
          <div className="tw-source-list">
            {thread.sources.map((source, i) => (
              <div key={i} className="tw-source-row">
                <div className="tw-source-title">{source.title || source.url || source.kind || 'source'}</div>
                {source.why && <div className="tw-source-why">{source.why}</div>}
                {source.url && (
                  <a href={source.url} target="_blank" rel="noreferrer">{source.url}</a>
                )}
              </div>
            ))}
          </div>
        </section>
      )}

      {thread.warnings.length > 0 && (
        <section className="tw-panel">
          <div className="tw-section-label">Warnings</div>
          <ul className="tw-warning-list">
            {thread.warnings.map((w, i) => <li key={i}>{w}</li>)}
          </ul>
        </section>
      )}
    </div>
  );
}
