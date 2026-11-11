import { startTransition, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { View } from './types';
import { viewReady } from './views';

export interface Route {
  view: View;
  articleId: string | null;
  agentId: string | null;
  noteId: number | null;
  roundtableId: number | null;
  repoSlug: string | null;
  blogPostId: number | null;
  tweetThreadId: number | null;
  libraryBookSlug: string | null;
  libraryQuoteId: string | null;
  goalId: number | null;
  lessonId: number | null;
  date: string | null;
}

const VIEW_PATHS: Record<string, View> = {
  '': 'today',
  '/': 'today',
  '/today': 'today',
  '/digest': 'digest',
  '/digest/audit': 'digest_audit',
  '/queue': 'queue',
  '/feed': 'feed',
  '/agents': 'agents',
  '/graph': 'graph',
  '/ingest': 'ingest',
  '/health': 'lint',
  '/lint': 'lint',
  '/mobile': 'mobile',
  '/open-questions': 'open_questions',
  '/settings': 'settings',
  '/notes': 'notes',
  '/library': 'library',
  '/tasks': 'tasks',
  '/projects': 'projects',
  '/routines': 'routines',
  '/journal': 'journal',
  '/people': 'people',
  '/inventory': 'inventory',
  '/content': 'content',
  '/inbox-triage': 'inbox_triage',
  '/roundtables': 'roundtables',
  '/repos': 'repos',
  '/blog': 'blog',
  '/tweets': 'tweets',
  '/podcasts': 'podcasts',
  '/suggestions': 'suggestions',
  '/learning': 'learning',
  '/automations': 'automations',
};

const PATH_FOR_VIEW: Record<View, string> = {
  article: '/a/',
  today: '/',
  digest: '/digest',
  digest_audit: '/digest/audit',
  queue: '/queue',
  feed: '/feed',
  agents: '/agents',
  agent_detail: '/agents/',
  graph: '/graph',
  ingest: '/ingest',
  lint: '/health',
  mobile: '/mobile',
  open_questions: '/open-questions',
  settings: '/settings',
  notes: '/notes',
  note: '/notes/',
  library: '/library',
  tasks: '/tasks',
  projects: '/projects',
  routines: '/routines',
  journal: '/journal',
  people: '/people',
  inventory: '/inventory',
  content: '/content',
  inbox_triage: '/inbox-triage',
  roundtables: '/roundtables',
  roundtable: '/roundtables/',
  repos: '/repos',
  repo: '/repos/',
  blog: '/blog',
  blog_post: '/blog/',
  tweets: '/tweets',
  tweet_thread: '/tweets/',
  podcasts: '/podcasts',
  podcast: '/p/',
  suggestions: '/suggestions',
  learning: '/learning',
  learning_goal: '/learning/goal/',
  learning_lesson: '/learning/lesson/',
  automations: '/automations',
};

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

function emptyRoute(view: View): Route {
  return {
    view, articleId: null, agentId: null, noteId: null, roundtableId: null, repoSlug: null,
    blogPostId: null, tweetThreadId: null, libraryBookSlug: null, libraryQuoteId: null,
    goalId: null, lessonId: null, date: null,
  };
}

export function parseRoute(pathname: string): Route {
  if (pathname.startsWith('/a/')) {
    const raw = pathname.slice(3).split('/')[0];
    if (raw) return { ...emptyRoute('article'), articleId: decodeURIComponent(raw) };
  }
  if (pathname.startsWith('/agents/')) {
    const raw = pathname.slice('/agents/'.length).split('/')[0];
    if (raw) return { ...emptyRoute('agent_detail'), agentId: decodeURIComponent(raw) };
    return emptyRoute('agents');
  }
  if (pathname === '/digest/audit' || pathname.startsWith('/digest/audit/')) {
    const raw = pathname.slice('/digest/audit'.length).replace(/^\//, '').split('/')[0];
    if (raw && ISO_DATE.test(raw)) {
      return { ...emptyRoute('digest_audit'), date: raw };
    }
    return emptyRoute('digest_audit');
  }
  if (pathname.startsWith('/digest/')) {
    const raw = pathname.slice('/digest/'.length).split('/')[0];
    if (raw && ISO_DATE.test(raw)) {
      return { ...emptyRoute('digest'), date: raw };
    }
    // Malformed date — fall through to today's digest.
    return emptyRoute('digest');
  }
  if (pathname.startsWith('/notes/')) {
    const raw = pathname.slice('/notes/'.length).split('/')[0];
    const id = Number(raw);
    if (raw && Number.isFinite(id) && id > 0) {
      return { ...emptyRoute('note'), noteId: id };
    }
    return emptyRoute('notes');
  }
  if (pathname.startsWith('/library/books/')) {
    const raw = pathname.slice('/library/books/'.length).split('/')[0];
    if (raw) return { ...emptyRoute('library'), libraryBookSlug: decodeURIComponent(raw) };
    return emptyRoute('library');
  }
  if (pathname.startsWith('/library/quotes/')) {
    const raw = pathname.slice('/library/quotes/'.length).split('/')[0];
    if (raw) return { ...emptyRoute('library'), libraryQuoteId: decodeURIComponent(raw) };
    return emptyRoute('library');
  }
  if (pathname.startsWith('/roundtables/')) {
    const raw = pathname.slice('/roundtables/'.length).split('/')[0];
    const id = Number(raw);
    if (raw && Number.isFinite(id) && id > 0) {
      return { ...emptyRoute('roundtable'), roundtableId: id };
    }
    return emptyRoute('roundtables');
  }
  if (pathname.startsWith('/repos/')) {
    const raw = pathname.slice('/repos/'.length);
    if (raw && raw.includes('/')) {
      return { ...emptyRoute('repo'), repoSlug: raw };
    }
    return emptyRoute('repos');
  }
  if (pathname.startsWith('/blog/')) {
    const raw = pathname.slice('/blog/'.length).split('/')[0];
    const id = Number(raw);
    if (raw && Number.isFinite(id) && id > 0) {
      return { ...emptyRoute('blog_post'), blogPostId: id };
    }
    return emptyRoute('blog');
  }
  if (pathname.startsWith('/tweets/')) {
    const raw = pathname.slice('/tweets/'.length).split('/')[0];
    const id = Number(raw);
    if (raw && Number.isFinite(id) && id > 0) {
      return { ...emptyRoute('tweet_thread'), tweetThreadId: id };
    }
    return emptyRoute('tweets');
  }
  if (pathname.startsWith('/learning/goal/')) {
    const raw = pathname.slice('/learning/goal/'.length).split('/')[0];
    const id = Number(raw);
    if (raw && Number.isFinite(id) && id > 0) {
      return { ...emptyRoute('learning_goal'), goalId: id };
    }
    return emptyRoute('learning');
  }
  if (pathname.startsWith('/learning/lesson/')) {
    const raw = pathname.slice('/learning/lesson/'.length).split('/')[0];
    const id = Number(raw);
    if (raw && Number.isFinite(id) && id > 0) {
      return { ...emptyRoute('learning_lesson'), lessonId: id };
    }
    return emptyRoute('learning');
  }
  if (pathname.startsWith('/p/')) {
    const raw = pathname.slice(3).split('/')[0];
    if (raw) return { ...emptyRoute('podcast'), articleId: decodeURIComponent(raw) };
  }
  const view = VIEW_PATHS[pathname];
  if (view) return emptyRoute(view);
  return emptyRoute('today');
}

export function routeToPath(view: View, arg?: string | null): string {
  if (view === 'article' && arg) return `/a/${encodeURIComponent(arg)}`;
  if (view === 'agent_detail' && arg) return `/agents/${encodeURIComponent(arg)}`;
  if (view === 'podcast' && arg) return `/p/${encodeURIComponent(arg)}`;
  if (view === 'digest' && arg && ISO_DATE.test(arg)) return `/digest/${arg}`;
  if (view === 'digest_audit' && arg && ISO_DATE.test(arg)) return `/digest/audit/${arg}`;
  if (view === 'note' && arg) return `/notes/${arg}`;
  if (view === 'library' && arg?.startsWith('book:')) return `/library/books/${encodeURIComponent(arg.slice(5))}`;
  if (view === 'library' && arg?.startsWith('quote:')) return `/library/quotes/${encodeURIComponent(arg.slice(6))}`;
  if (view === 'roundtable' && arg) return `/roundtables/${arg}`;
  if (view === 'repo' && arg) return `/repos/${arg}`;
  if (view === 'blog_post' && arg) return `/blog/${arg}`;
  if (view === 'tweet_thread' && arg) return `/tweets/${arg}`;
  if (view === 'learning_goal' && arg) return `/learning/goal/${arg}`;
  if (view === 'learning_lesson' && arg) return `/learning/lesson/${arg}`;
  return PATH_FOR_VIEW[view] ?? '/';
}

const SETTLE_CAP_MS = 220;
const SETTLE_QUIET_MS = 48;

const wait = (ms: number) => new Promise<void>((resolve) => { window.setTimeout(resolve, ms); });
const mainEl = () => document.querySelector<HTMLElement>('main.main');

/** Cross-fade between views only where the browser supports it, the tab is
 *  visible, and the reader has not asked for reduced motion. */
function canCrossFade(): boolean {
  return typeof document.startViewTransition === 'function'
    && document.visibilityState === 'visible'
    && !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/** Resolves when the main region has stopped changing for a moment. Views
 *  fetch their data after mounting; against the local daemon that lands within
 *  a few tens of milliseconds, and waiting for it lets the cross-fade end on
 *  content instead of a loading line. The caller caps the wait. */
function settled(): Promise<void> {
  const main = mainEl();
  if (!main) return Promise.resolve();
  return new Promise<void>((resolve) => {
    let timer = window.setTimeout(finish, SETTLE_QUIET_MS);
    const observer = new MutationObserver(() => {
      window.clearTimeout(timer);
      timer = window.setTimeout(finish, SETTLE_QUIET_MS);
    });
    function finish() { observer.disconnect(); resolve(); }
    observer.observe(main, { childList: true, subtree: true });
    window.setTimeout(finish, SETTLE_CAP_MS);
  });
}

/** After a navigation, keyboard and screen-reader focus moves to the new
 *  view. A field the reader is typing in (the ask drawer, a dialog) keeps it. */
function focusMain(): void {
  const active = document.activeElement as HTMLElement | null;
  if (active?.closest('input, textarea, select, [contenteditable="true"], [role="dialog"]')) return;
  mainEl()?.focus({ preventScroll: true });
}

export function useRoute() {
  const [route, setRouteNow] = useState<Route>(() => parseRoute(window.location.pathname));

  // Views are lazy chunks. Committing a route change as a React transition
  // keeps the current view on screen until the next one is ready, instead of
  // flashing the Suspense fallback on every navigation.
  //
  // `onCommit` runs in the layout phase of the render that puts the new route
  // on screen, so the scroll reset and the view-transition snapshot both see
  // the new view.
  const onCommit = useRef<(() => void) | null>(null);
  const navSeq = useRef(0);
  useLayoutEffect(() => {
    const done = onCommit.current;
    onCommit.current = null;
    done?.();
  }, [route]);

  const setRoute = useCallback((next: Route, fresh = false) => {
    const seq = ++navSeq.current;
    const commit = () => new Promise<void>((resolve) => {
      // A navigation that overtakes this one must not leave it waiting.
      onCommit.current?.();
      onCommit.current = () => {
        // A new page starts at the top; back/forward keeps where the browser was.
        if (fresh) mainEl()?.scrollTo(0, 0);
        resolve();
      };
      startTransition(() => setRouteNow(next));
    });
    const landed = () => { if (seq === navSeq.current) focusMain(); };

    if (!canCrossFade()) {
      void commit().then(landed);
      return;
    }
    // Wait for the chunk before freezing the page for the snapshot, so a slow
    // download never holds a frozen frame.
    void viewReady(next.view).then(() => {
      if (seq !== navSeq.current) return;
      const transition = document.startViewTransition(() =>
        // The new snapshot is taken when this resolves: after the route is on
        // screen and its first data has rendered, or after SETTLE_CAP_MS.
        Promise.race([commit().then(settled), wait(SETTLE_CAP_MS)]),
      );
      transition.finished.catch(() => {}).then(landed);
    });
  }, []);

  useEffect(() => {
    const onPop = () => setRoute(parseRoute(window.location.pathname));
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, [setRoute]);

  const buildRoute = (view: View, arg?: string): Route => {
    const next = emptyRoute(view);
    if (view === 'article' && arg) next.articleId = arg;
    else if (view === 'agent_detail' && arg) next.agentId = arg;
    else if (view === 'podcast' && arg) next.articleId = arg;
    else if (view === 'digest' && arg && ISO_DATE.test(arg)) next.date = arg;
    else if (view === 'digest_audit' && arg && ISO_DATE.test(arg)) next.date = arg;
    else if (view === 'note' && arg) next.noteId = Number(arg);
    else if (view === 'library' && arg?.startsWith('book:')) next.libraryBookSlug = arg.slice(5);
    else if (view === 'library' && arg?.startsWith('quote:')) next.libraryQuoteId = arg.slice(6);
    else if (view === 'roundtable' && arg) next.roundtableId = Number(arg);
    else if (view === 'repo' && arg) next.repoSlug = arg;
    else if (view === 'blog_post' && arg) next.blogPostId = Number(arg);
    else if (view === 'tweet_thread' && arg) next.tweetThreadId = Number(arg);
    else if (view === 'learning_goal' && arg) next.goalId = Number(arg);
    else if (view === 'learning_lesson' && arg) next.lessonId = Number(arg);
    return next;
  };

  const navigate = useCallback((view: View, arg?: string) => {
    const path = routeToPath(view, arg);
    if (path !== window.location.pathname + window.location.search) {
      window.history.pushState(null, '', path);
    }
    setRoute(buildRoute(view, arg), true);
  }, [setRoute]);

  const replace = useCallback((view: View, arg?: string) => {
    const path = routeToPath(view, arg);
    window.history.replaceState(null, '', path);
    setRoute(buildRoute(view, arg));
  }, [setRoute]);

  return { route, navigate, replace };
}
