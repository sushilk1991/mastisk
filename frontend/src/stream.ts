import { useEffect, useRef, useState } from 'react';

const FLUSH_MS = 150;

export function useFeedStream<T = unknown>(initial: T[] = []) {
  const [rows, setRows] = useState<T[]>(initial);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    const es = new EventSource('/api/feed/stream');
    esRef.current = es;
    // Agents emit rows in bursts. Each setRows re-renders the whole app, so
    // collect a burst and commit it once.
    let pending: T[] = [];
    let timer: number | null = null;
    const flush = () => {
      timer = null;
      const batch = pending;
      pending = [];
      setRows((prev) => [...batch, ...prev].slice(0, 100));
    };
    es.addEventListener('tick', (evt) => {
      try {
        pending.unshift(JSON.parse((evt as MessageEvent).data) as T);
        if (timer === null) timer = window.setTimeout(flush, FLUSH_MS);
      } catch { /* ignore */ }
    });
    es.onerror = () => { /* auto-reconnects */ };
    return () => {
      es.close();
      esRef.current = null;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, []);

  return { rows, setRows };
}
