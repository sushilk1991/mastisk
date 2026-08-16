import { useCallback, useEffect, useState } from 'react';
import { api } from './api';
import { useRoute } from './router';
import { useFeedStream } from './stream';
import type {
  Article, AgentInfo, BlogPostDetail, Digest, FeedTick, PinnedItem, VaultItem, View,
  TweetThread,
} from './types';

import { Titlebar } from './components/Titlebar';
import { Sidebar } from './components/Sidebar';
import { ArticleView } from './components/ArticleView';
import { RightRail } from './components/RightRail';
import { SystemRail } from './components/SystemRail';
import { DigestView } from './components/DigestView';
import { DigestAuditView } from './components/DigestAuditView';
import { AgentsView } from './components/AgentsView';
import { GraphView } from './components/GraphView';
import { AskDrawer } from './components/AskDrawer';
import { MobileNav } from './components/MobileNav';
import { CommandPalette } from './components/CommandPalette';
import { IngestView } from './components/IngestView';
import { OpenQuestionsView } from './components/OpenQuestionsView';
import { SuggestionsView } from './components/SuggestionsView';
import { LearningView, LessonView } from './components/LearningView';
import { AutomationsView } from './components/AutomationsView';
import { QueueView } from './components/QueueView';
import { SettingsView } from './components/SettingsView';
import { SystemCheckView } from './components/SystemCheckView';
import { WikiLinkHoverProvider } from './components/WikiLinkHover';
import { NotesView } from './components/NotesView';
import { NoteView } from './components/NoteView';
import { LibraryView } from './components/LibraryView';
import { QuickCaptureSheet } from './components/QuickCaptureSheet';
import { RoundtablesListView } from './components/RoundtablesListView';
import { RoundtableView } from './components/RoundtableView';
import { ReposView } from './components/ReposView';
import { RepoDetailView } from './components/RepoDetailView';
import { AddRepoModal } from './components/AddRepoModal';
import { BlogListView } from './components/BlogListView';
import { BlogView } from './components/BlogView';
import { BlogCreationModal } from './components/BlogCreationModal';
import { TweetThreadsView } from './components/TweetThreadsView';
import { TweetThreadView } from './components/TweetThreadView';
import { PodcastsListView } from './components/PodcastsListView';
import { PodcastView } from './components/PodcastView';
import {
  ContentView, InboxTriageView, InventoryView, JournalView, PeopleView, ProjectsView, RoutinesView, TasksView, TodayView,
} from './components/DashboardViews';

export function App() {
  const [theme, setTheme] = useState<'light' | 'dark'>(
    (localStorage.getItem('mk-theme') as 'light' | 'dark') || 'light',
  );

  const { route, navigate: routeNavigate, replace } = useRoute();
  const { view, articleId: currentArticle, agentId: currentAgent, noteId: currentNote, date: currentDate } = route;
  const currentRoundtable = route.roundtableId;
  const currentBlogPost = route.blogPostId;
  const currentTweetThread = route.tweetThreadId;

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
  const mergedFeed: FeedTick[] = [...liveRows, ...feed];

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
    localStorage.setItem('mk-theme', theme);
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

  useEffect(() => {
    void api.sidebar().then(setSidebar).catch(console.error);
    void api.feed().then((d) => { setFeed(d.feed); setAgents(d.agents); }).catch(console.error);
  }, []);

  // Re-fetch the digest whenever the requested date changes (null = today).
  useEffect(() => {
    setDigest(null);
    void api.digest(currentDate ?? undefined).then(setDigest).catch(console.error);
  }, [currentDate]);

  // Refresh sidebar counts + digest whenever agents emit a new tick, so the
  // "Concepts 1 → 2" counter updates as articles are compiled without a reload.
  // Also polls on a 30s floor as a safety net in case the SSE stream drops.
  const tickKey = liveRows[0] ? `${(liveRows[0] as FeedTick).t}-${liveRows.length}` : '';
  useEffect(() => {
    if (!tickKey) return;
    void api.sidebar().then(setSidebar).catch(() => {});
    void api.digest(currentDate ?? undefined).then(setDigest).catch(() => {});
  }, [tickKey, currentDate]);
  useEffect(() => {
    const id = setInterval(() => {
      void api.sidebar().then(setSidebar).catch(() => {});
      void api.digest(currentDate ?? undefined).then(setDigest).catch(() => {});
    }, 30000);
    return () => clearInterval(id);
  }, [currentDate]);

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
        onTheme={() => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))}
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

      <main className="main">
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
            liveKey={tickKey}
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
        {view === 'learning_lesson' && <LessonView lessonId={route.lessonId} onNavigate={navigate}/>}
        {view === 'automations' && <AutomationsView liveKey={tickKey} onNavigate={navigate}/>}
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
            liveKey={tickKey}
            initialBookSlug={route.libraryBookSlug}
            initialQuoteId={route.libraryQuoteId}
            onNavigate={navigate}
          />
        )}
        {view === 'tasks' && <TasksView liveKey={tickKey}/>}
        {view === 'projects' && <ProjectsView liveKey={tickKey}/>}
        {view === 'routines' && <RoutinesView liveKey={tickKey}/>}
        {view === 'journal' && <JournalView liveKey={tickKey}/>}
        {view === 'people' && <PeopleView liveKey={tickKey}/>}
        {view === 'inventory' && <InventoryView liveKey={tickKey}/>}
        {view === 'content' && <ContentView liveKey={tickKey} onNavigate={navigate}/>}
        {view === 'inbox_triage' && <InboxTriageView liveKey={tickKey}/>}
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
      </main>

      {railOpen && view === 'article' && article && (
        <RightRail
          article={article}
          feed={mergedFeed}
          agents={agents}
          onAsk={openAsk}
          onNavigate={navigate}
          onClose={() => setRailOpen(false)}
        />
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

      <AskDrawer
        open={askOpen}
        ctx={askCtx}
        onClose={() => setAskOpen(false)}
        onNavigate={navigate}
      />
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
