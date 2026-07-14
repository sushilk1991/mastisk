import { useCallback, useEffect, useState } from 'react';
import type { View } from './types';

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
  automations: '/automations',
};

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

function emptyRoute(view: View): Route {
  return {
    view, articleId: null, agentId: null, noteId: null, roundtableId: null, repoSlug: null,
    blogPostId: null, tweetThreadId: null, libraryBookSlug: null, libraryQuoteId: null,
    date: null,
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
  if (pathname.startsWith('/p/')) {
    const raw = pathname.slice(3).split('/')[0];
    if (raw) return { ...emptyRoute('podcast'), articleId: decodeURIComponent(raw) };
  }
  const view = VIEW_PATHS[pathname];
  if (view) return emptyRoute(view);
  return emptyRoute('digest');
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
  return PATH_FOR_VIEW[view] ?? '/';
}

export function useRoute() {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.pathname));

  useEffect(() => {
    const onPop = () => setRoute(parseRoute(window.location.pathname));
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);

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
    return next;
  };

  const navigate = useCallback((view: View, arg?: string) => {
    const path = routeToPath(view, arg);
    if (path !== window.location.pathname + window.location.search) {
      window.history.pushState(null, '', path);
    }
    setRoute(buildRoute(view, arg));
  }, []);

  const replace = useCallback((view: View, arg?: string) => {
    const path = routeToPath(view, arg);
    window.history.replaceState(null, '', path);
    setRoute(buildRoute(view, arg));
  }, []);

  return { route, navigate, replace };
}
