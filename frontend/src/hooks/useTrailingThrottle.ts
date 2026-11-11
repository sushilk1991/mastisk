import { useEffect, useRef, useState } from 'react';

/**
 * Follow `value`, but publish at most one change per `ms`, `ms` after the
 * first unpublished change (trailing edge). A burst of N changes becomes one
 * publish carrying the latest value. Nothing is published while the tab is
 * hidden; the latest value is flushed when it becomes visible again.
 */
export function useTrailingThrottle<T>(value: T, ms: number): T {
  const [published, setPublished] = useState(value);
  const latest = useRef(value);
  const timer = useRef<number | null>(null);
  const heldWhileHidden = useRef(false);
  latest.current = value;

  useEffect(() => {
    if (timer.current !== null || Object.is(value, published)) return;
    timer.current = window.setTimeout(() => {
      timer.current = null;
      if (document.hidden) heldWhileHidden.current = true;
      else setPublished(latest.current);
    }, ms);
  }, [value, published, ms]);

  useEffect(() => {
    const onVisible = () => {
      if (document.hidden || !heldWhileHidden.current) return;
      heldWhileHidden.current = false;
      setPublished(latest.current);
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      document.removeEventListener('visibilitychange', onVisible);
      if (timer.current !== null) window.clearTimeout(timer.current);
      timer.current = null;
    };
  }, []);

  return published;
}
