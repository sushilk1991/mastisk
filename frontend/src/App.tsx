import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from './api';
import { useRoute } from './router';
import { useFeedStream } from './stream';
import type {
  Article, AgentInfo, BlogPostDetail, Digest, FeedTick, PinnedItem, VaultItem, View,
  TweetThread,
} from './types';

import { Titlebar } from './components/Titlebar';
import { Sidebar } from './components/Sidebar';
import { SystemRail } from './components/SystemRail';
import { MobileNav } from './components/MobileNav';
import { CommandPalette } from './components/CommandPalette';
import { WikiLinkHoverProvider } from './components/WikiLinkHover';
import { QuickCaptureSheet } from './components/QuickCaptureSheet';
import { AddRepoModal } from './components/AddRepoModal';
import { BlogCreationModal } from './components/BlogCreationModal';
import { ViewErrorBoundary } from './components/ViewErrorBoundary';
import { useTrailingThrottle } from './hooks/useTrailingThrottle';
// Everything below is a lazy chunk: the entry bundle carries only the shell.
import {
  AgentsView, ArticleView, AskDrawer, AutomationsView, BlogListView, BlogView, ContentView, DigestAuditView,
  DigestView, GoalView, GraphView, InboxTriageView, IngestView, InventoryView, JournalView, LearningView,
  LessonView, LibraryView, NoteView, NotesView, OpenQuestionsView, PeopleView, PodcastView, PodcastsListView,
  ProjectsView, QueueView, RepoDetailView, ReposView, RightRail, RoundtableView, RoundtablesListView,
  RoutinesView, SettingsView, SuggestionsView, SystemCheckView, TasksView, TodayView, TweetThreadView,
  TweetThreadsView, preloadView,
} from './views';

// Live feed rows can arrive many per second while agents work. Views get a
// tick key that changes at most once per LIVE_TICK_MS (trailing), and the
// sidebar counts refresh on a slower beat.
const LIVE_TICK_MS = 2500;
const SIDEBAR_TICK_MS = 10000;
const POLL_MS = 30000;

export function App() {
  const [theme, setTheme] = useState<'light' | 'dark'>(
    // index.html resolved this before first paint: saved choice, else the system setting.
    (document.documentElement.getAttribute('data-theme') as 'light' | 'dark') || 'light',
  );

  const { route, navigate: routeNavigate, replace } = useRoute();
  const { view, articleId: currentArticle, agentId: currentAgent, noteId: currentNote, date: currentDate } = route;
  const currentRoundtable = route.roundtableId;
  const currentBlogPost = route.blogPostId;
  const currentTweetThread = route.tweetThreadId;
  // A navigation clears a caught view error.
  const routeKey = useMemo(() => JSON.stringify(route), [route]);

  const [sideOpen, setSideOpen] = useState(window.innerWidth > 900);
  const [railOpen, setRailOpen] = useState(window.innerWidth > 900);
  const [askOpen, setAskOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [quickCaptureOpen, setQuickCaptureOpen] = useState(false);
  const [quickCaptureHint, setQuickCaptureHint] = useState<'note' | undefined>(undefined);
  const [addRepoOpen, setAddRepoOpen] = useState(false);
  const [captureBlogOpen, setCaptureBlogOpen] = useState(false);
  // Optional theme to pre-fill BlogCreationModal with when it opens — set by
  // the topic-suggestions panel's "Draft this →", reset to undefined on every
  // close so the next manual "+ new draft" still opens an empty modal.
  const [pendingTheme, setPendingTheme] = useState<string | undefined>(undefined);
  const openBlogModalWithTheme = useCallback((title: string) => {
    setPendingTheme(title);
    setCaptureBlogOpen(true);
  }, []);
  const openQuickCapture = useCallback((hint?: 'note') => {
    setPaletteOpen(false);
    setQuickCaptureHint(hint);
    setQuickCaptureOpen(true);
  }, []);
  const closeQuickCapture = useCallback(() => {
    setQuickCaptureOpen(false);
    setQuickCaptureHint(undefined);
  }, []);
  const closeBlogModal = useCallback(() => {
    setCaptureBlogOpen(false);
    setPendingTheme(undefined);
  }, []);
  // Bumped after a successful add-repo, so ReposView re-fetches its list when
  // we're already on /repos (navigating there is a no-op in that case).
  const [reposReloadKey, setReposReloadKey] = useState(0);
  const [askCtx, setAskCtx] = useState<{ prompt: string; selection: string | null; article_id?: string } | null>(null);

  const [sidebar, setSidebar] = useState<{ vault: VaultItem[]; pinned: PinnedItem[]; user: import('./types').UserInfo } | null>(null);
  const [article, setArticle] = useState<Article | null>(null);
  const [blogPostDetail, setBlogPostDetail] = useState<BlogPostDetail | null>(null);
  const [tweetThreadDetail, setTweetThreadDetail] = useState<TweetThread | null>(null);
  const [digest, setDigest] = useState<Digest | null>(null);
  const [feed, setFeed] = useState<FeedTick[]>([]);
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [toast, setToast] = useState<{ msg: string; noteId: number } | null>(null);

  const { rows: liveRows } = useFeedStream<FeedTick>([]);
  const mergedFeed: FeedTick[] = useMemo(() => [...liveRows, ...feed], [liveRows, feed]);

  // Watch for escalator/auto-escalated feed rows and surface a toast so the
  // user knows a background research job just kicked off on one of their notes.
  const liveEscalations = liveRows.filter(
    (r) => (r as FeedTick).agent === 'escalator' && (r as FeedTick).verb === 'auto-escalated',
  );
  const lastEscalation = liveEscalations[0];
  useEffect(() => {
    if (!lastEscalation) return;
    const tick = lastEscalation as FeedTick;
    const payload = tick.payload_json
      ? (() => { try { return JSON.parse(tick.payload_json!); } catch { return {}; } })()
      : {};
    const title = (payload as { title?: string }).title ?? 'note';
    const noteId = Number(tick.obj);
    if (!Number.isFinite(noteId)) return;
    setToast({ msg: `Auto-researching: ${title}`, noteId });
    const t = setTimeout(() => setToast(null), 5000);
    return () => clearTimeout(t);
  }, [lastEscalation]);

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
  }, [theme]);

  // The desktop shell starts with both rails open; crossing into a phone-sized
  // viewport must not preserve those panels and leave the main content buried.
  useEffect(() => {
    const syncShellToViewport = () => {
      if (window.innerWidth <= 900) {
        setSideOpen(false);
        setRailOpen(false);
      }
    };
    syncShellToViewport();
    window.addEventListener('resize', syncShellToViewport);
    return () => window.removeEventListener('resize', syncShellToViewport);
  }, []);

  // Set when a live refresh was skipped because nothing on screen needed it.
  const sidebarStaleRef = useRef(false);
  const digestStaleRef = useRef(false);

  const refreshSidebar = useCallback(() => {
    sidebarStaleRef.current = false;
    void api.sidebar().then(setSidebar).catch(() => {});
  }, []);

  // Drop a response that lands after the user moved to another date.
  const dateRef = useRef(currentDate);
  dateRef.current = currentDate;
  const refreshDigest = useCallback((onError: (e: unknown) => void = () => {}) => {
    const date = dateRef.current;
    digestStaleRef.current = false;
    void api.digest(date ?? undefined)
      .then((d) => { if (dateRef.current === date) setDigest(d); })
      .catch(onError);
  }, []);

  useEffect(() => {
    void api.sidebar().then(setSidebar).catch(console.error);
    void api.feed().then((d) => { setFeed(d.feed); setAgents(d.agents); }).catch(console.error);
  }, []);

  // Re-fetch the digest whenever the requested date changes (null = today).
  useEffect(() => {
    setDigest(null);
    refreshDigest(console.error);
  }, [currentDate, refreshDigest]);

  // Live refresh. `tickKey` changes on every feed row (keyed on the newest
  // row's id: the list is capped and `t` is a coarse relative age, so neither
  // changes reliably when a new tick arrives). Refetching on each one was a
  // request storm while agents were busy, so:
  //   - views receive `liveKey`, published at most once per LIVE_TICK_MS;
  //   - the digest is refetched only while a view that renders it is showing;
  //   - sidebar counts refresh on a slower beat, and only while it is open.
  // Whatever is skipped is marked stale and refetched when it comes back into
  // view. The 30s poll is the safety net for a dropped SSE stream.
  const newestTick = liveRows[0];
  const tickKey = newestTick ? String(newestTick.id ?? `${newestTick.t}-${liveRows.length}`) : '';
  const liveKey = useTrailingThrottle(tickKey, LIVE_TICK_MS);
  const sidebarKey = useTrailingThrottle(tickKey, SIDEBAR_TICK_MS);

  const showsDigest = view === 'today' || view === 'digest';
  const showsDigestRef = useRef(showsDigest);
  showsDigestRef.current = showsDigest;
  const sideOpenRef = useRef(sideOpen);
  sideOpenRef.current = sideOpen;

  const liveRefreshSidebar = useCallback(() => {
    if (sideOpenRef.current) refreshSidebar();
    else sidebarStaleRef.current = true;
  }, [refreshSidebar]);
  const liveRefreshDigest = useCallback(() => {
    if (showsDigestRef.current) refreshDigest();
    else digestStaleRef.current = true;
  }, [refreshDigest]);

  useEffect(() => { if (liveKey) liveRefreshDigest(); }, [liveKey, liveRefreshDigest]);
  useEffect(() => { if (sidebarKey) liveRefreshSidebar(); }, [sidebarKey, liveRefreshSidebar]);
  useEffect(() => {
    const id = setInterval(() => {
      if (document.hidden) {
        sidebarStaleRef.current = true;
        digestStaleRef.current = true;
        return;
      }
      liveRefreshSidebar();
      liveRefreshDigest();
    }, POLL_MS);
    const onVisible = () => {
      if (document.hidden) return;
      if (sidebarStaleRef.current) liveRefreshSidebar();
      if (digestStaleRef.current) liveRefreshDigest();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      clearInterval(id);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [liveRefreshSidebar, liveRefreshDigest]);
  useEffect(() => {
    if (sideOpen && sidebarStaleRef.current) refreshSidebar();
  }, [sideOpen, refreshSidebar]);
  useEffect(() => {
    if (showsDigest && digestStaleRef.current) refreshDigest();
  }, [showsDigest, refreshDigest]);

  // Load the article whenever the route points at one. On 404, bounce to the
  // Today so a dead deep-link doesn't leave the user staring at "loading…".
  useEffect(() => {
    if (view !== 'article' || !currentArticle) {
      setArticle(null);
      return;
    }
    api.article(currentArticle)
      .then(setArticle)
      .catch(() => {
        setArticle(null);
        replace('today');
      });
  }, [view, currentArticle, replace]);

  const navigate = useCallback((v: View, id?: string) => {
    // Start the chunk download now; the route change is a React transition, so
    // the current view stays on screen until the next one can render.
    preloadView(v);
    routeNavigate(v, id);
    if (window.innerWidth <= 900) {
      setSideOpen(false);
      setRailOpen(false);
    }
  }, [routeNavigate]);

  const refreshAgents = useCallback(async () => {
    const data = await api.feed();
    setFeed(data.feed);
    setAgents(data.agents);
  }, []);

  const openAsk = useCallback((prompt = '', selection: string | null = null) => {
    setAskCtx({ prompt, selection, article_id: currentArticle ?? undefined });
    setAskOpen(true);
  }, [currentArticle]);

  // Global shortcuts:
  //   ⌘K / Ctrl+K opens the command palette.
  //   ⌘⇧A / Ctrl+Shift+A opens quick capture from anywhere.
  // Both are intercepted inside form fields because they are app-level command
  // chords, not ordinary text input.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault();
        setPaletteOpen(true);
        return;
      }
      if ((e.metaKey || e.ctrlKey) && e.shiftKey && (e.key === 'a' || e.key === 'A')) {
        e.preventDefault();
        openQuickCapture();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [openQuickCapture]);

  return (
    <div className="app" data-rail={railOpen ? 'open' : 'closed'} data-side={sideOpen ? 'open' : 'closed'}>
      <Titlebar
        view={view}
        articleTitle={article?.title}
        articleKind={article?.kind}
        blogPostTitle={blogPostDetail?.title ?? null}
        blogPostStatus={blogPostDetail?.status ?? null}
        tweetThreadTitle={tweetThreadDetail?.title ?? null}
        tweetThreadStatus={tweetThreadDetail?.status ?? null}
        theme={theme}
        onTheme={() => setTheme((t) => {
          const next = t === 'dark' ? 'light' : 'dark';
          // Saved only on an explicit toggle, so an untouched install keeps following the system.
          try { localStorage.setItem('mk-theme', next); } catch { /* private mode */ }
          return next;
        })}
        onToggleSide={() => {
          setRailOpen(false);
          setSideOpen((s) => !s);
        }}
        onToggleRail={() => {
          setSideOpen(false);
          setRailOpen((s) => !s);
        }}
        onAsk={() => openAsk()}
        onSearchClick={() => setPaletteOpen(true)}
        onCapture={() => openQuickCapture()}
      />

      {sideOpen && sidebar && (
        <Sidebar
          vault={sidebar.vault}
          pinned={sidebar.pinned}
          user={sidebar.user}
          currentView={view}
          currentArticle={currentArticle ?? ''}
          onNavigate={navigate}
          onAddRepo={() => setAddRepoOpen(true)}
          onCaptureNote={() => openQuickCapture('note')}
          onCreateBlog={() => setCaptureBlogOpen(true)}
        />
      )}

      <main className="main" tabIndex={-1}>
        <ViewErrorBoundary resetKey={routeKey}>
        <Suspense fallback={<Loading/>}>
        {view === 'article' && article && (
          <ArticleView
            article={article}
            onAsk={openAsk}
            onNavigate={navigate}
            onContext={() => {
              setSideOpen(false);
              setRailOpen(true);
            }}
          />
        )}
        {view === 'article' && !article && <Loading/>}
        {view === 'today' && (
          <TodayView
            liveKey={liveKey}
            digest={digest}
            onNavigate={navigate}
            onAsk={openAsk}
          />
        )}
        {view === 'digest' && digest && <DigestView digest={digest} onNavigate={navigate} onAsk={openAsk}/>}
        {view === 'digest' && !digest && <Loading/>}
        {view === 'digest_audit' && <DigestAuditView date={currentDate ?? undefined} onNavigate={navigate}/>}
        {view === 'feed' && <AgentsView agents={agents} feed={mergedFeed} onNavigate={navigate}/>}
        {view === 'agents' && <AgentsView agents={agents} feed={mergedFeed} onNavigate={navigate}/>}
        {view === 'agent_detail' && currentAgent && (
          <AgentsView
            agents={agents}
            feed={mergedFeed}
            agentId={currentAgent}
            onNavigate={navigate}
            onAgentsChanged={refreshAgents}
          />
        )}
        {view === 'graph' && <GraphView onNavigate={navigate}/>}
        {view === 'ingest' && <IngestView/>}
        {view === 'open_questions' && <OpenQuestionsView onNavigate={navigate}/>}
        {view === 'suggestions' && <SuggestionsView onNavigate={navigate}/>}
        {view === 'learning' && <LearningView onNavigate={navigate}/>}
        {view === 'learning_goal' && <GoalView key={route.goalId} goalId={route.goalId} onNavigate={navigate}/>}
        {view === 'learning_lesson' && <LessonView key={route.lessonId} lessonId={route.lessonId} onNavigate={navigate}/>}
        {view === 'automations' && <AutomationsView liveKey={liveKey} onNavigate={navigate}/>}
        {view === 'queue' && <QueueView onNavigate={navigate}/>}
        {view === 'lint' && <SystemCheckView/>}
        {view === 'settings' && <SettingsView/>}
        {view === 'notes' && (
          <NotesView
            onNavigate={navigate}
            onCaptureNote={() => openQuickCapture('note')}
          />
        )}
        {view === 'note' && currentNote !== null && <NoteView noteId={currentNote} onNavigate={navigate}/>}
        {view === 'library' && (
          <LibraryView
            liveKey={liveKey}
            initialBookSlug={route.libraryBookSlug}
            initialQuoteId={route.libraryQuoteId}
            onNavigate={navigate}
          />
        )}
        {view === 'tasks' && <TasksView liveKey={liveKey}/>}
        {view === 'projects' && <ProjectsView liveKey={liveKey}/>}
        {view === 'routines' && <RoutinesView liveKey={liveKey}/>}
        {view === 'journal' && <JournalView liveKey={liveKey}/>}
        {view === 'people' && <PeopleView liveKey={liveKey}/>}
        {view === 'inventory' && <InventoryView liveKey={liveKey}/>}
        {view === 'content' && <ContentView liveKey={liveKey} onNavigate={navigate}/>}
        {view === 'inbox_triage' && <InboxTriageView liveKey={liveKey}/>}
        {view === 'roundtables' && <RoundtablesListView onNavigate={navigate}/>}
        {view === 'roundtable' && currentRoundtable !== null && (
          <RoundtableView roundtableId={currentRoundtable} onNavigate={navigate}/>
        )}
        {view === 'repos' && (
          <ReposView
            onNavigate={navigate}
            onAddRepo={() => setAddRepoOpen(true)}
            reloadKey={reposReloadKey}
          />
        )}
        {view === 'repo' && route.repoSlug && <RepoDetailView slug={route.repoSlug} onNavigate={navigate}/>}
        {view === 'blog' && (
          <BlogListView
            onCreateBlog={() => setCaptureBlogOpen(true)}
            onCreateBlogWithTheme={openBlogModalWithTheme}
            onNavigate={navigate}
          />
        )}
        {view === 'blog_post' && currentBlogPost !== null && (
          <BlogView
            blogPostId={currentBlogPost}
            onNavigate={navigate}
            onLoaded={setBlogPostDetail}
          />
        )}
        {view === 'tweets' && <TweetThreadsView onNavigate={navigate}/>}
        {view === 'tweet_thread' && currentTweetThread !== null && (
          <TweetThreadView
            threadId={currentTweetThread}
            onNavigate={navigate}
            onLoaded={setTweetThreadDetail}
          />
        )}
        {view === 'podcasts' && <PodcastsListView onNavigate={navigate}/>}
        {view === 'podcast' && currentArticle && (
          <PodcastView articleId={currentArticle} onAsk={openAsk} onNavigate={navigate}/>
        )}
        {view === 'mobile' && (
          <div className="view">
            <div className="view-h">System</div>
            <h1 className="view-title">Mobile companion</h1>
            <p className="view-sub">
              Open Mastisk on your phone via the Tailnet URL (run <code>mastisk url</code>)
              and tap Share → Add to Home Screen. The full reader runs as a PWA.
            </p>
          </div>
        )}
        </Suspense>
        </ViewErrorBoundary>
      </main>

      {railOpen && view === 'article' && article && (
        <ViewErrorBoundary resetKey={routeKey} silent>
          <Suspense fallback={null}>
            <RightRail
              article={article}
              feed={mergedFeed}
              agents={agents}
              onAsk={openAsk}
              onNavigate={navigate}
              onClose={() => setRailOpen(false)}
            />
          </Suspense>
        </ViewErrorBoundary>
      )}
      {railOpen && view !== 'article' && (
        <SystemRail
          view={view}
          feed={mergedFeed}
          agents={agents}
          selectedDate={digest?.iso_date ?? null}
          onNavigate={navigate}
        />
      )}

      <ViewErrorBoundary resetKey={routeKey} silent>
        <Suspense fallback={null}>
          <AskDrawer
            open={askOpen}
            ctx={askCtx}
            onClose={() => setAskOpen(false)}
            onNavigate={navigate}
          />
        </Suspense>
      </ViewErrorBoundary>
      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        onAsk={(q) => {
          // Escalating to AI: dismiss any other modal that was floating
          // (most commonly an already-open AskDrawer with a stale prompt).
          setAskOpen(false);
          openAsk(q, null);
        }}
        onNavigate={(view, id) => {
          // Same intent on result-pick navigation: don't leave AskDrawer
          // hovering with an unrelated question over the new page.
          setAskOpen(false);
          navigate(view, id);
        }}
      />
      <QuickCaptureSheet
        open={quickCaptureOpen}
        onClose={closeQuickCapture}
        onNavigate={navigate}
        initialHint={quickCaptureHint}
      />
      <AddRepoModal
        open={addRepoOpen}
        onClose={() => setAddRepoOpen(false)}
        onAdded={(slug) => {
          setAddRepoOpen(false);
          // Bump the reload key so ReposView re-fetches if it's already mounted,
          // and land on the repo's detail page so the user sees the fresh row.
          setReposReloadKey((k) => k + 1);
          navigate('repo', slug);
        }}
        onNavigate={navigate}
      />
      <BlogCreationModal
        open={captureBlogOpen}
        initialTheme={pendingTheme}
        onClose={closeBlogModal}
        onCreated={(id) => {
          closeBlogModal();
          navigate('blog_post', String(id));
        }}
      />
      {toast && (
        <div
          role="status"
          onClick={() => { navigate('note', String(toast.noteId)); setToast(null); }}
          style={{
            position: 'fixed', bottom: 20, right: 20, zIndex: 1100,
            background: 'var(--bg)', border: '1px solid var(--border)',
            padding: '8px 12px', borderRadius: 6,
            boxShadow: '0 4px 16px rgba(0,0,0,0.15)',
            cursor: 'pointer', fontSize: 13, maxWidth: 320,
          }}
        >
          <div style={{ fontSize: 11, color: 'var(--fg-faint)', fontFamily: 'var(--mono)', marginBottom: 2 }}>
            auto-escalated
          </div>
          {toast.msg}
        </div>
      )}
      <MobileNav
        currentView={view}
        menuOpen={sideOpen}
        onNavigate={navigate}
        onSearch={() => setPaletteOpen(true)}
        onChat={() => openAsk()}
        onCapture={() => openQuickCapture()}
        onMenu={() => {
          setRailOpen(false);
          setSideOpen((open) => !open);
        }}
      />
      <WikiLinkHoverProvider/>
    </div>
  );
}

function Loading() {
  return (
    <div className="view">
      <p style={{color:'var(--fg-faint)',fontFamily:'var(--mono)',fontSize:12}}>loading…</p>
    </div>
  );
}
