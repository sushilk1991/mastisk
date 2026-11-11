// Route-level code splitting. Every view is its own chunk so the entry bundle
// carries only the shell; `preloadView` lets the router start a chunk download
// before React asks for it (first paint, and ahead of a navigation commit).
import { lazy } from 'react';
import type { View } from './types';

const load = {
  article: () => import('./components/ArticleView'),
  rightRail: () => import('./components/RightRail'),
  askDrawer: () => import('./components/AskDrawer'),
  dashboard: () => import('./components/DashboardViews'),
  digest: () => import('./components/DigestView'),
  digestAudit: () => import('./components/DigestAuditView'),
  agents: () => import('./components/AgentsView'),
  graph: () => import('./components/GraphView'),
  ingest: () => import('./components/IngestView'),
  openQuestions: () => import('./components/OpenQuestionsView'),
  suggestions: () => import('./components/SuggestionsView'),
  learning: () => import('./components/LearningView'),
  automations: () => import('./components/AutomationsView'),
  queue: () => import('./components/QueueView'),
  settings: () => import('./components/SettingsView'),
  systemCheck: () => import('./components/SystemCheckView'),
  notes: () => import('./components/NotesView'),
  note: () => import('./components/NoteView'),
  library: () => import('./components/LibraryView'),
  roundtables: () => import('./components/RoundtablesListView'),
  roundtable: () => import('./components/RoundtableView'),
  repos: () => import('./components/ReposView'),
  repo: () => import('./components/RepoDetailView'),
  blogList: () => import('./components/BlogListView'),
  blog: () => import('./components/BlogView'),
  tweets: () => import('./components/TweetThreadsView'),
  tweet: () => import('./components/TweetThreadView'),
  podcasts: () => import('./components/PodcastsListView'),
  podcast: () => import('./components/PodcastView'),
};

export const ArticleView = lazy(() => load.article().then((m) => ({ default: m.ArticleView })));
export const RightRail = lazy(() => load.rightRail().then((m) => ({ default: m.RightRail })));
export const AskDrawer = lazy(() => load.askDrawer().then((m) => ({ default: m.AskDrawer })));

export const TodayView = lazy(() => load.dashboard().then((m) => ({ default: m.TodayView })));
export const TasksView = lazy(() => load.dashboard().then((m) => ({ default: m.TasksView })));
export const ProjectsView = lazy(() => load.dashboard().then((m) => ({ default: m.ProjectsView })));
export const RoutinesView = lazy(() => load.dashboard().then((m) => ({ default: m.RoutinesView })));
export const JournalView = lazy(() => load.dashboard().then((m) => ({ default: m.JournalView })));
export const PeopleView = lazy(() => load.dashboard().then((m) => ({ default: m.PeopleView })));
export const InventoryView = lazy(() => load.dashboard().then((m) => ({ default: m.InventoryView })));
export const ContentView = lazy(() => load.dashboard().then((m) => ({ default: m.ContentView })));
export const InboxTriageView = lazy(() => load.dashboard().then((m) => ({ default: m.InboxTriageView })));

export const DigestView = lazy(() => load.digest().then((m) => ({ default: m.DigestView })));
export const DigestAuditView = lazy(() => load.digestAudit().then((m) => ({ default: m.DigestAuditView })));
export const AgentsView = lazy(() => load.agents().then((m) => ({ default: m.AgentsView })));
export const GraphView = lazy(() => load.graph().then((m) => ({ default: m.GraphView })));
export const IngestView = lazy(() => load.ingest().then((m) => ({ default: m.IngestView })));
export const OpenQuestionsView = lazy(() => load.openQuestions().then((m) => ({ default: m.OpenQuestionsView })));
export const SuggestionsView = lazy(() => load.suggestions().then((m) => ({ default: m.SuggestionsView })));
export const LearningView = lazy(() => load.learning().then((m) => ({ default: m.LearningView })));
export const GoalView = lazy(() => load.learning().then((m) => ({ default: m.GoalView })));
export const LessonView = lazy(() => load.learning().then((m) => ({ default: m.LessonView })));
export const AutomationsView = lazy(() => load.automations().then((m) => ({ default: m.AutomationsView })));
export const QueueView = lazy(() => load.queue().then((m) => ({ default: m.QueueView })));
export const SettingsView = lazy(() => load.settings().then((m) => ({ default: m.SettingsView })));
export const SystemCheckView = lazy(() => load.systemCheck().then((m) => ({ default: m.SystemCheckView })));
export const NotesView = lazy(() => load.notes().then((m) => ({ default: m.NotesView })));
export const NoteView = lazy(() => load.note().then((m) => ({ default: m.NoteView })));
export const LibraryView = lazy(() => load.library().then((m) => ({ default: m.LibraryView })));
export const RoundtablesListView = lazy(() => load.roundtables().then((m) => ({ default: m.RoundtablesListView })));
export const RoundtableView = lazy(() => load.roundtable().then((m) => ({ default: m.RoundtableView })));
export const ReposView = lazy(() => load.repos().then((m) => ({ default: m.ReposView })));
export const RepoDetailView = lazy(() => load.repo().then((m) => ({ default: m.RepoDetailView })));
export const BlogListView = lazy(() => load.blogList().then((m) => ({ default: m.BlogListView })));
export const BlogView = lazy(() => load.blog().then((m) => ({ default: m.BlogView })));
export const TweetThreadsView = lazy(() => load.tweets().then((m) => ({ default: m.TweetThreadsView })));
export const TweetThreadView = lazy(() => load.tweet().then((m) => ({ default: m.TweetThreadView })));
export const PodcastsListView = lazy(() => load.podcasts().then((m) => ({ default: m.PodcastsListView })));
export const PodcastView = lazy(() => load.podcast().then((m) => ({ default: m.PodcastView })));

type Loader = () => Promise<unknown>;

// `mobile` is inline markup in App and has no chunk.
const VIEW_LOADERS: Partial<Record<View, Loader[]>> = {
  article: [load.article, load.rightRail],
  today: [load.dashboard],
  tasks: [load.dashboard],
  projects: [load.dashboard],
  routines: [load.dashboard],
  journal: [load.dashboard],
  people: [load.dashboard],
  inventory: [load.dashboard],
  content: [load.dashboard],
  inbox_triage: [load.dashboard],
  digest: [load.digest],
  digest_audit: [load.digestAudit],
  feed: [load.agents],
  agents: [load.agents],
  agent_detail: [load.agents],
  graph: [load.graph],
  ingest: [load.ingest],
  open_questions: [load.openQuestions],
  suggestions: [load.suggestions],
  learning: [load.learning],
  learning_goal: [load.learning],
  learning_lesson: [load.learning],
  automations: [load.automations],
  queue: [load.queue],
  lint: [load.systemCheck],
  settings: [load.settings],
  notes: [load.notes],
  note: [load.note],
  library: [load.library],
  roundtables: [load.roundtables],
  roundtable: [load.roundtable],
  repos: [load.repos],
  repo: [load.repo],
  blog: [load.blogList],
  blog_post: [load.blog],
  tweets: [load.tweets],
  tweet_thread: [load.tweet],
  podcasts: [load.podcasts],
  podcast: [load.podcast],
};

/** Start downloading a view's chunk. Failures are left for React.lazy to
 *  surface through the error boundary when the view actually renders. */
export function preloadView(view: View): void {
  for (const loader of VIEW_LOADERS[view] ?? []) {
    loader().catch(() => {});
  }
}

/** Resolves once a view's chunks are in the module cache (or have failed; the
 *  error boundary reports that when the view renders). */
export function viewReady(view: View): Promise<void> {
  return Promise.all((VIEW_LOADERS[view] ?? []).map((loader) => loader().catch(() => {}))).then(() => {});
}
