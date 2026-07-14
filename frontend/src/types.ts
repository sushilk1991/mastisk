export type ArticleKind = 'Concept' | 'Entity' | 'Source' | 'Synthesis';

export interface ArticleSection {
  idx: number;
  h: string;
  body: string;
  kind: 'section' | 'callout' | 'open' | 'diagram';
}

export interface RelatedLink {
  id: string;
  label: string;
  weight: number;
}

export interface BacklinkRow {
  id: string;
  title: string;
  snippet: string;
  weight: number;
}

export interface SourceRow {
  kind: string;
  title: string;
  url: string | null;
  date: string;
}

export type ArticlePreview =
  | { id: string; exists: true; kind: ArticleKind; title: string; summary: string }
  | { id: string; exists: false };

export interface ArticleMedia {
  src: string;
  alt?: string;
  caption?: string;
}

export interface Article {
  id: string;
  kind: ArticleKind;
  title: string;
  slug: string;
  aka: string[];
  summary: string;
  body_md: string;
  confidence: number;
  readingTime: string;
  sources: number;
  backlinks: number;
  forwardlinks: number;
  sections: ArticleSection[];
  related: RelatedLink[];
  backlinkList: BacklinkRow[];
  sourceList: SourceRow[];
  updated_by: string;
  updated_at: string;
  vault_path?: string;
  heroImageUrl?: string | null;
  media?: ArticleMedia[];
}

export interface Note {
  id: number;
  slug: string;
  path: string;
  source: 'pwa' | 'cli' | 'file' | 'watch' | 'phone';
  created_at: string;
  classified_at: string | null;
  classification: string | null;
  summary: string | null;
  tags: string[];
  escalation_state: string;
  body?: string;
  body_sha256?: string;
  confidence?: number | null;
  escalation_trigger?: string | null;
  escalation_article_id?: string | null;
  escalation_retry_count?: number;
  deleted_at?: string | null;
  related_articles?: { article_id: string; title: string; rank: number }[];
}

export interface QuickCaptureResponse {
  id: string | number;
  type:
    | 'task'
    | 'note'
    | 'journal'
    | 'project_update'
    | 'routine_done'
    | 'person'
    | 'quote'
    | 'inventory'
    | 'content'
    | 'inbox'
    | string;
  destination: string;
  needs_triage: boolean;
}

export interface VaultPage {
  kind: 'page';
  id: string;
  label: string;
  glyph: string;
  badge?: string;
  hot?: boolean;
}

export interface VaultSection {
  kind: 'section';
  label: string;
}

export interface VaultFolder {
  kind: 'folder';
  label: string;
  count: number;
  children: VaultPage[];
}

export type VaultItem = VaultPage | VaultSection | VaultFolder;

export interface PinnedItem { id: string; label: string; }

export interface UserInfo {
  name: string;
  initials: string;
  stats: { pages: number; sources: number; feeds: number };
}

export interface Feed {
  url: string;
  title: string;
  last_fetched: string | null;
  last_etag: string | null;
  last_modified: string | null;
  enabled: boolean;
  added_at: string;
}

export interface JobDetail {
  title: string | null;
  subtitle: string | null;
  url: string | null;
  source_kind: string | null;
  article_id: string | null;
}

export interface Job {
  id: number;
  agent: string;
  kind: string;
  status: 'queued' | 'running' | 'done' | 'failed';
  attempts: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  detail: JobDetail;
}

export interface GraphNode {
  id: string;
  title: string;
  kind: string;
  color: string;
  size: number;
  degree: number;
}

export interface GraphEdge {
  from_article: string;
  to_article: string;
  weight: number;
}

export interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
  clusters: { kind: string; color: string; count: number }[];
  stats: { pages: number; connections: number };
}

export interface FeedTick {
  id?: number;
  t: string;
  agent: string;
  verb: string;
  obj: string;
  touched: number;
  kind?: string;
  payload_json?: string | null;
}

export interface AgentInfo {
  id: string;
  name: string;
  role: string;
  status: 'active' | 'idle' | 'disabled';
  load: number;
  color: 'amber' | 'violet' | 'emerald' | 'blue' | 'rose';
  implemented: boolean;
  queued: number;
  running: number;
  profile?: {
    enabled: boolean;
    skills: string[];
    invalid: boolean;
    invalid_reason: string | null;
  };
}

export interface AgentSkill {
  slug: string;
  path: string;
  name: string;
  description: string | null;
  tags: string[];
  body: string;
}

export interface AgentPromptSlot {
  slot_id: string;
  label: string;
  module: string;
  module_attr: string;
  primary: boolean;
  editable: boolean;
  default: string;
  override: string;
  required_placeholders: string[];
  invalid: boolean;
  invalid_reason: string | null;
}

export interface AgentProfile {
  agent_id: string;
  path: string;
  enabled: boolean;
  model: string | null;
  skills: string[];
  prompt_override: string;
  slot_overrides: Record<string, string>;
  invalid: boolean;
  invalid_reason: string | null;
  invalid_slots: Record<string, string[]>;
  model_supported: boolean;
  model_note: string;
}

export interface AgentDetail {
  agent: AgentInfo;
  definition: {
    id: string;
    name: string;
    role: string;
    module: string;
    model_supported: boolean;
    model_note: string;
  };
  profile: AgentProfile;
  slots: AgentPromptSlot[];
  skills: AgentSkill[];
  recent_feed: FeedTick[];
  queue: { queued: number; running: number; failed: number };
}

export interface AgentProfileUpdate {
  enabled?: boolean;
  model?: string | null;
  skills?: string[];
  prompt_override?: string | null;
  slot_overrides?: Record<string, string | null>;
}

export interface DigestThreadScores {
  quality: number;
  interest: number;
  final: number;
}

export interface DigestThread {
  title: string;
  body: string;
  sources: number;
  links: string[];
  article_id?: string;
  scores?: DigestThreadScores;
  feedback?: 'yes' | 'no' | null;
}

export interface Digest {
  date: string;
  iso_date: string;
  prev_date: string | null;
  next_date: string | null;
  summary: string;
  counters: { label: string; value: number }[];
  threads: DigestThread[];
  queue: string[];
  meta?: { considered: number; selected: number; unshown: number };
}

export interface DigestAuditCandidate {
  article_id: string;
  title: string;
  kind: string;
  summary: string;
  quality: number;
  interest: number;
  final: number;
  selected: boolean;
  rank: number | null;
  feedback: 'yes' | 'no' | null;
}

export interface DigestAuditDay {
  date: string;
  candidates: DigestAuditCandidate[];
}

export interface DigestAuditRollup {
  total_considered: number;
  total_shown: number;
  liked: number;
  disliked: number;
  unrated: number;
  hit_rate: number;
}

export interface DigestAudit {
  start: string;
  end: string;
  window_days: number;
  days: DigestAuditDay[];
  rollup: DigestAuditRollup;
}

export interface OpenQuestion {
  article_id: string;
  article_title: string;
  article_kind: ArticleKind;
  heading: string;
  body: string;
  updated_at: string;
}

export interface OpenQuestionsResponse {
  questions: OpenQuestion[];
}

export interface AskResponse {
  answer: string;
  cites: string[];
  hits: { id: string; title: string; snippet?: string }[];
}

export type SearchResultKind =
  | 'article' | 'note' | 'blog'
  | 'task' | 'project' | 'routine' | 'journal' | 'person'
  | 'book' | 'quote' | 'inventory' | 'content';

export interface SearchResult {
  kind: SearchResultKind;
  /** Article id (string) for articles; numeric rowid (as string) for notes/blog. */
  id: string;
  title: string;
  /** Short label under the title — article kind, "Note · Observation", "Blog post". */
  subtitle: string;
  /** FTS5 snippet wrapping match terms with STX (\x02) / ETX (\x03) so the
   * marker chars can never collide with literal `<mark>` text the user wrote
   * in a note about HTML. The palette renders these as React `<mark>` nodes. */
  snippet: string;
  excerpt?: string;
  link_target?: string;
  slug?: string;
  /** Lower = better. Frontend doesn't sort (server already did) but exposes for tests. */
  score: number;
}

export interface RoundtablePerspective {
  backend: string;
  model: string | null;
  content: string | null;
  error: string | null;
  latency_ms: number | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface RoundtableSummary {
  id: number;
  input_type: 'note' | 'article' | 'prompt';
  input_ref: string;
  status: 'pending' | 'running' | 'done' | 'failed';
  synthesis_preview: string | null;
  synthesis_model: string | null;
  created_at: string;
  finished_at: string | null;
  saved_as_note_id: number | null;
}

export interface Roundtable extends RoundtableSummary {
  prompt: string;
  synthesis: string | null;
  error: string | null;
  perspectives: RoundtablePerspective[];
}

export type View =
  | 'article' | 'today' | 'digest' | 'digest_audit' | 'feed' | 'agents' | 'agent_detail'
  | 'graph' | 'mobile' | 'queue' | 'ingest' | 'lint' | 'settings'
  | 'open_questions'
  | 'notes' | 'note' | 'library'
  | 'tasks' | 'projects' | 'routines' | 'journal' | 'people' | 'inventory' | 'content' | 'inbox_triage'
  | 'roundtables' | 'roundtable'
  | 'repos' | 'repo'
  | 'blog' | 'blog_post'
  | 'tweets' | 'tweet_thread'
  | 'podcasts' | 'podcast'
  | 'suggestions';

export interface WikiSuggestion {
  slug: string;
  title: string;
  kind: string;
  occurrences: number;
  referrers: string[];
  status: 'pending' | 'promoted' | 'dismissed';
  created_at: string;
  last_seen_at: string;
  decided_at: string | null;
}

export type TaskStatus = 'open' | 'done' | string;
export type Priority = 'high' | 'medium' | 'low' | null;
export type TimeOfDay = 'morning' | 'afternoon' | 'evening' | 'anytime';

export interface Domain {
  slug: string;
  name: string;
  created_at?: string;
  deleted_at?: string | null;
}

export interface TaskRow {
  uid: string;
  host_path: string;
  line_number: number;
  text: string;
  checked: boolean;
  status: TaskStatus;
  due: string | null;
  scheduled: string | null;
  priority: Priority;
  domain: string | null;
  project: string | null;
  recurrence: string | null;
  tags: string[];
  links: string[];
  needs_triage: boolean;
  staleness_days?: number | null;
  reminder_lead_minutes?: number | null;
  no_reminder?: boolean;
  review_at?: string | null;
  recurrence_unparsed: boolean;
  position?: number;
  focused?: boolean;
  deleted_at?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface SlippingItem {
  entity_type: 'task' | 'project' | string;
  entity_id: string;
  title: string;
  subtitle: string;
  domain: string | null;
  stale_since: string;
  computed_at: string;
  slipping_muted?: boolean;
  slipping_muted_until?: string | null;
  link: string;
}

export interface ResurfaceItem {
  kind: 'note' | string;
  id: number | string;
  title: string;
  excerpt: string;
  link: string;
}

export interface NeedsReviewItem {
  id: number;
  entity_type: string;
  entity_id: string;
  reason: string;
  surfaced_at: string;
  dismissed_at: string | null;
  title: string;
  excerpt: string;
  link: string;
}

export interface ProjectSummary {
  slug: string;
  path: string;
  name: string;
  type: 'project' | 'area' | 'retainer' | string;
  domain: string | null;
  status: 'active' | 'someday' | 'paused' | 'done' | string;
  due: string | null;
  last_activity_at: string | null;
  open_task_count: number;
  created_at?: string;
  updated_at?: string;
}

export interface ProjectMilestone {
  position: number;
  text: string;
  done: boolean;
}

export interface ProjectMilestoneProgress {
  done: number;
  total: number;
  percent: number;
}

export interface ProjectTimeEntry {
  position: number;
  date: string;
  hours: number;
  text: string;
}

export interface ProjectTimeTotals {
  total_hours: number;
  last_30_days_hours: number;
}

export interface ProjectRetainerState {
  current_month: string;
  month_end: string;
  done: number;
  open: number;
  total: number;
  tasks: Pick<TaskRow, 'uid' | 'text' | 'status' | 'due'>[];
}

export interface ProjectDetail extends ProjectSummary {
  frontmatter: Record<string, unknown>;
  body: string;
  milestones: ProjectMilestone[];
  milestone_progress: ProjectMilestoneProgress;
  time_entries: ProjectTimeEntry[];
  time_totals: ProjectTimeTotals;
  retainer: ProjectRetainerState | null;
}

export interface ChecklistTemplate {
  name: string;
  task_count: number;
}

export interface RoutineStreak {
  current: number;
  longest: number;
  rate_30d: number;
  fixed?: {
    days_done: number;
    target_days: number;
    remaining: number;
    complete: boolean;
  };
}

export interface RoutineRow {
  slug: string;
  path: string;
  name: string;
  description: string | null;
  domain: string | null;
  time_of_day: TimeOfDay;
  specific_time: string | null;
  notify: boolean;
  streak_type: 'ongoing' | 'fixed' | string;
  target_days: number | null;
  start_date: string | null;
  archived: boolean;
  completed_today: boolean;
  streak: RoutineStreak;
}

export type RoutineGroups = Record<TimeOfDay, RoutineRow[]>;

export interface RoutineProgress {
  slug: string;
  completion_dates: string[];
}

export interface PersonInteraction {
  ts: string;
  text: string;
}

export interface PersonSummary {
  slug: string;
  path: string;
  name: string;
  birthday: string | null;
  anniversary: string | null;
  facts: Record<string, unknown>;
  follow_up_at: string | null;
  last_interaction_at: string | null;
  birthday_soon: boolean;
  deleted_at?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface PersonDetail extends PersonSummary {
  frontmatter: Record<string, unknown>;
  body: string;
  interactions: PersonInteraction[];
}

export type InventoryStatus = 'owned' | 'sold' | 'discarded';

export interface InventorySummary {
  id: string;
  path: string;
  name: string;
  acquired: string | null;
  value: number | null;
  status: InventoryStatus | string;
  location: string | null;
  photo: string | null;
  archived?: boolean;
  deleted_at?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface InventoryDetail extends InventorySummary {
  frontmatter: Record<string, unknown>;
  body: string;
}

export interface InventoryList {
  items: InventorySummary[];
  total_value: number;
}

export type BookStatus = 'want' | 'reading' | 'finished' | 'abandoned';
export type QuoteSourceType = 'book' | 'article' | 'podcast' | 'conversation';

export interface BookHighlight {
  id: number;
  book_slug: string;
  position: number;
  text: string;
  content_hash: string;
  quote_id: string | null;
  created?: boolean;
}

export interface BookSummary {
  slug: string;
  path: string;
  title: string;
  author: string | null;
  cover_url: string | null;
  status: BookStatus | string;
  format: string | null;
  started: string | null;
  finished: string | null;
  rating: number | null;
  isbn: string | null;
  summary: string | null;
  highlight_count: number;
  deleted_at?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface QuoteSummary {
  id: string;
  path: string;
  text: string;
  content_hash: string;
  source_type: QuoteSourceType | string;
  source_ref: string | null;
  tags: string[];
  deleted_at?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface QuoteThought {
  ts: string;
  text: string;
}

export interface QuoteDetail extends QuoteSummary {
  frontmatter: Record<string, unknown>;
  thoughts: QuoteThought[];
}

export interface BookDetail extends BookSummary {
  frontmatter: Record<string, unknown>;
  body: string;
  highlights: BookHighlight[];
  linked_quotes: QuoteSummary[];
}

export interface KindleImportResult {
  imported: number;
  skipped_duplicates: number;
  review_count: number;
}

export interface KindleReviewItem {
  id: number;
  raw_hash: string;
  raw_block: string;
  reason: string;
  parsed_title: string | null;
  parsed_author: string | null;
  parsed_content: string | null;
  status: string;
  quote_id: string | null;
  created_at: string;
  resolved_at: string | null;
}

export interface ReminderRow {
  id: number;
  entity_type: string | null;
  entity_id: string | null;
  fire_at: string;
  lead_minutes: number | null;
  kind: string;
  status: 'pending' | 'firing' | 'sent' | 'late' | 'notify_failed' | 'cancelled' | string;
  title: string | null;
  body: string | null;
  url: string | null;
  created_at: string;
  fired_at: string | null;
  last_error?: string | null;
}

export type ContentKind = 'video' | 'article' | 'podcast' | 'newsletter';
export type ContentStatus = 'idea' | 'outline' | 'editing' | 'waiting' | 'published' | 'done';

export interface ContentSummary {
  slug: string;
  path: string;
  title: string;
  kind: ContentKind | string;
  status: ContentStatus | string;
  domain: string | null;
  channel: string | null;
  url: string | null;
  publish_date: string | null;
  needs_triage: boolean;
  archived?: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface ContentDetail extends ContentSummary {
  frontmatter: Record<string, unknown>;
  body: string;
  tasks: TaskRow[];
}

export interface ContentList {
  items: ContentSummary[];
  kanban: Record<ContentStatus | string, ContentSummary[]>;
}

export interface CalendarStatus {
  status: 'connected' | 'disconnected' | 'not_synced' | 'unconfigured' | string;
  last_synced_at: string | null;
  error: string | null;
  last_error: string | null;
  last_error_at: string | null;
}

export interface CalendarEvent {
  id: string;
  calendar_id: string;
  summary: string;
  start: string;
  end: string;
  all_day: boolean;
  location: string | null;
  status: string | null;
  updated_at: string | null;
  synced_at: string;
}

export interface CalendarToday {
  date: string;
  status: CalendarStatus;
  events: CalendarEvent[];
}

export interface IntegrationHealth {
  calendar: CalendarStatus;
  push: {
    backend: string;
    configured: boolean;
    notify_failed_last_24h: number;
  };
  bridges: {
    claude: { configured: boolean; cmd: string; available: boolean };
    ollama_chat: {
      local_url: string;
      local_only: boolean;
      cloud_configured: boolean;
      model: string;
    };
    ollama_embed: { local_url: string; model: string };
  };
  ingest: {
    markitdown: boolean;
    docling: boolean;
    mlx_whisper: boolean;
  };
}

export interface JournalDaySummary {
  date: string;
  path: string;
  mood: number | null;
  energy: number | null;
  log_count: number;
  has_reflections: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface JournalDay extends JournalDaySummary {
  frontmatter: Record<string, unknown>;
  body_md: string;
  sections: Record<string, string>;
  tasks: TaskRow[];
  routine_completions: {
    routine_id: string;
    date: string;
    created_at: string;
    name: string;
    time_of_day: TimeOfDay;
  }[];
  fired_reminders: ReminderRow[];
}

export type CaptureTriageTarget =
  | 'task' | 'note' | 'journal' | 'project_update' | 'routine_done'
  | 'person' | 'quote' | 'inventory' | 'content' | 'inbox' | 'dismiss';

export interface CaptureTriageItem {
  id: string;
  kind: string;
  detected_type: CaptureTriageTarget | string;
  original_text: string;
  confidence: number | null;
  capture: Record<string, unknown>;
  source: Record<string, unknown>;
}

export interface PodcastListItem {
  article_id: string;
  article_title: string;
  article_summary: string;
  article_kind: ArticleKind;
  article_confidence: number;
  article_updated_at: string;
  article_hero: string | null;
  source_id: string;
  source_kind: 'podcast' | 'youtube';
  source_title: string;
  source_url: string;
  source_author: string | null;
  source_published_at: string | null;
  source_duration_sec: number | null;
  source_feed_url: string | null;
  source_hero: string | null;
}

export interface TranscriptSegment {
  idx: number;
  start_sec: number;
  end_sec: number;
  text: string;
}

export interface TranscriptAnchor {
  source_id: string;
  segment_idx: number;
  start_sec: number;
}

export interface AnchoredNote {
  id: number;
  body: string;
  classification: string | null;
  summary: string | null;
  created_at: string;
  transcript_anchor: TranscriptAnchor;
}

export interface PodcastView {
  article: Article;
  source: {
    id: string;
    kind: 'podcast' | 'youtube';
    title: string;
    url: string;
    author: string;
    published_at: string | null;
    duration_sec: number | null;
    feed_url: string | null;
    hero_image_url: string | null;
  };
  transcript_text: string;
  segments: TranscriptSegment[];
  anchored_notes: AnchoredNote[];
}

export type BlogSourceKind = 'note' | 'article' | 'roundtable';

export interface BlogPostSource {
  n: number;
  kind: BlogSourceKind;
  ref: string;
  used: boolean;
  origin: string | null;
  resolved: {
    title?: string;
    summary?: string;
    slug?: string;
    url?: string | null;
    deleted: boolean;
  };
}

export interface BlogPostSummary {
  id: number;
  slug: string | null;
  path: string | null;
  title: string | null;
  theme: string;
  window_days: number;
  status: 'pending' | 'running' | 'done' | 'failed';
  model: string | null;
  tags: string[];
  word_count: number | null;
  body_preview: string | null;
  created_at: string;
  finished_at: string | null;
  saved_as_note_id: number | null;
}

export interface BlogPostDetail extends BlogPostSummary {
  body_md: string | null;
  error: string | null;
  sources: BlogPostSource[];
}

export interface TweetThreadSource {
  kind?: string;
  title?: string;
  url?: string | null;
  why?: string;
}

export interface TweetThreadFeedback {
  id: number;
  target_tweet_index: number | null;
  body: string;
  created_at: string;
  applied_at: string | null;
}

export interface TweetThread {
  id: number;
  title: string | null;
  angle: string | null;
  theme: string;
  url: string | null;
  window_days: number;
  include_web: boolean;
  use_browser_context: boolean;
  status: 'pending' | 'running' | 'done' | 'failed';
  model: string | null;
  thread: string[];
  sources: TweetThreadSource[];
  warnings: string[];
  feedback: TweetThreadFeedback[];
  error: string | null;
  created_at: string;
  finished_at: string | null;
}

export interface RepoSnapshot {
  polled_at: string;
  stars_count: number | null;
  forks_count: number | null;
  open_issues_count: number | null;
  open_prs_count: number | null;
  error: string | null;
}

export interface RepoSummary {
  slug: string;
  source_type?: 'github' | 'local';
  local_path?: string | null;
  display_name: string | null;
  description: string | null;
  is_private: boolean;
  last_polled_at: string | null;
  last_ideated_at: string | null;
  snapshot: RepoSnapshot | null;
}

export interface BrowseEntry {
  name: string;
  path: string;
  is_dir: boolean;
  is_git_repo: boolean;
}

export interface RepoDetail {
  slug: string;
  source_type?: 'github' | 'local';
  local_path?: string | null;
  display_name: string | null;
  description: string | null;
  is_private: boolean;
  default_branch: string | null;
  added_at: string;
  last_polled_at: string | null;
  last_ideated_at: string | null;
  context_md: string | null;
  latest_snapshot: Record<string, unknown> | null;
}

export interface RepoIdea {
  id: number;
  slug: string;
  summary: string | null;
  classification: string | null;
  confidence: number | null;
  created_at: string;
  classified_at: string | null;
}

export interface RepoIdeaRun {
  id: number;
  ideated_at: string;
  model: string | null;
  error: string | null;
  ideas_count: number;
}

export interface RepoIdeasResponse {
  ideas: RepoIdea[];
  runs: RepoIdeaRun[];
}

export interface BudgetValues {
  scout: number;
  listener: number;
  compiler: number;
  linter: number;
  synthesizer: number;
}

export interface SettingsValues {
  claude_cmd: string;
  ollama_local_url: string;
  ollama_local_only: boolean;
  ollama_cloud_url: string;
  ollama_cloud_key_set: boolean;
  embed_model: string;
  summarize_model_cheap: string;
  summarize_model_heavy: string;
  budget: BudgetValues;
}

export interface SettingsBundle {
  values: SettingsValues;
  model_roles: Record<string, string>;
  config_path: string;
  cloud_active: boolean;
}

export type ArtifactKind = 'chart' | 'comparison' | 'timeline' | 'stat';

export interface Artifact {
  id: number;
  article_id: string;
  kind: ArtifactKind;
  title: string;
  description: string | null;
  spec: Record<string, unknown>;
  created_by: string | null;
  created_at: string;
  updated_at: string;
}

export interface SettingsPatch {
  claude_cmd?: string;
  ollama_local_url?: string;
  ollama_local_only?: boolean;
  ollama_cloud_url?: string;
  ollama_cloud_key?: string;  // "" clears, non-empty sets, omit to leave
  embed_model?: string;
  summarize_model_cheap?: string;
  summarize_model_heavy?: string;
  budget?: BudgetValues;
}

export interface SynthesisRun {
  id: number;
  cluster_hash: string;
  source_article_ids: string[];
  draft_article_id: string | null;
  eval_score: number | null;
  eval_rationale: string | null;
  user_accepted: 0 | 1 | null;
  user_feedback: string | null;
  created_at: string;
  reviewed_at: string | null;
}

export interface SynthesisRunResponse {
  run: SynthesisRun | null;
}

export interface PendingSynthesisResponse {
  runs: SynthesisRun[];
}

export type TopicSuggestionKind = 'daily' | 'opinion';

export interface TopicSourceRef {
  kind: 'note' | 'article' | 'roundtable';
  ref: number | string;
  label: string;
}

export interface TopicSuggestion {
  id: number;
  kind: TopicSuggestionKind;
  title: string;
  hook: string;
  angle: string | null;
  source_refs: TopicSourceRef[];
  created_at: string;
}
