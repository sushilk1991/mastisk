import type {
  Article, ArticlePreview, Artifact, ArtifactKind, AskConversation, AskConversationSummary, AskResponse, BlogPostDetail,
  AgentDetail, AgentProfileUpdate, AgentSkill,
  BlogPostSummary, BookDetail, BookHighlight, BookStatus, BookSummary, CaptureTriageItem, CaptureTriageTarget, ContentDetail, ContentKind, ContentList, ContentStatus, Digest, DigestAudit, Domain, Feed,
  FeedTick, AgentInfo, CalendarStatus, CalendarToday, ChecklistTemplate, GraphData, IntegrationHealth, InventoryDetail, InventoryList, InventoryStatus, Job, Note, OpenQuestionsResponse, PendingSynthesisResponse,
  KindleImportResult, KindleReviewItem, PersonDetail, PersonSummary, PinnedItem, PodcastListItem, PodcastView, ProjectDetail, ProjectSummary, QuoteDetail, QuoteSourceType, QuoteSummary, ReminderRow,
  QuickCaptureResponse, RepoDetail, RepoIdeasResponse, RepoSummary, ResurfaceItem, RoutineGroups, RoutineProgress, RoutineRow,
  Roundtable, RoundtableSummary, SearchResult,
  SettingsBundle, SettingsPatch, SlippingItem, TaskRow, TimeOfDay, TranscriptAnchor, JournalDay, JournalDaySummary,
  Automation, AutomationDetail, AutomationRun, AutomationTriggers,
  SynthesisRunResponse, TopicSuggestion, TweetThread, UserInfo, VaultItem, WikiSuggestion,
  NeedsReviewItem,
} from './types';

const BASE = '/api';

export class ApiError extends Error {
  status: number;
  detail: string;
  constructor(message: string, status: number, detail: string = message) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

export class FocusFullError extends Error {
  focus: TaskRow[];
  constructor(focus: TaskRow[]) {
    super('daily focus already has three tasks');
    this.name = 'FocusFullError';
    this.focus = focus;
  }
}

async function throwApiError(r: Response): Promise<never> {
  let detail = `${r.status} ${r.statusText}`.trim();
  try {
    const body = await r.json() as { detail?: unknown };
    if (body && typeof body.detail === 'string' && body.detail) detail = body.detail;
    else if (body && body.detail) detail = JSON.stringify(body.detail);
  } catch { /* fall back to status */ }
  throw new ApiError(detail, r.status, detail);
}

async function focusResponse(r: Response): Promise<TaskRow[]> {
  if (r.status === 409) {
    try {
      const body = await r.json() as { detail?: { error?: string; focus?: TaskRow[] } };
      if (body.detail?.error === 'focus_full' && Array.isArray(body.detail.focus)) {
        throw new FocusFullError(body.detail.focus);
      }
    } catch (e) {
      if (e instanceof FocusFullError) throw e;
    }
  }
  if (!r.ok) await throwApiError(r);
  return r.json() as Promise<TaskRow[]>;
}

async function j<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, init);
  if (!r.ok) await throwApiError(r);
  return r.json() as Promise<T>;
}

type CompactGraphNode = [id: string, title: string, kindIndex: number, size: number, degree: number];
type CompactGraphEdge = [sourceIndex: number, targetIndex: number, weight: number];
interface CompactGraphData {
  v: 1;
  kinds: GraphData['clusters'];
  nodes: CompactGraphNode[];
  edges: CompactGraphEdge[];
  stats: GraphData['stats'];
}

function normalizeGraph(raw: GraphData | CompactGraphData): GraphData {
  if (!('v' in raw)) return raw;
  const nodes = raw.nodes.map(([id, title, kindIndex, size, degree]) => {
    const cluster = raw.kinds[kindIndex];
    return {
      id,
      title,
      kind: cluster?.kind ?? 'System',
      color: cluster?.color ?? 'var(--kind-system)',
      size,
      degree,
    };
  });
  const edges = raw.edges.flatMap(([sourceIndex, targetIndex, weight]) => {
    const source = nodes[sourceIndex];
    const target = nodes[targetIndex];
    if (!source || !target) return [];
    return [{ from_article: source.id, to_article: target.id, weight }];
  });
  return {
    nodes,
    edges,
    clusters: raw.kinds,
    stats: raw.stats,
  };
}

export const api = {
  sidebar: () => j<{ vault: VaultItem[]; pinned: PinnedItem[]; user: UserInfo }>(`${BASE}/sidebar`),

  quickCapture: (text: string, ts?: string): Promise<QuickCaptureResponse> =>
    j<QuickCaptureResponse>(`${BASE}/quick-capture`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ text, ...(ts ? { ts } : {}) }),
    }),

  graph: async () => normalizeGraph(await j<GraphData | CompactGraphData>(`${BASE}/graph/compact`)),

  feeds: () => j<{ feeds: Feed[] }>(`${BASE}/feeds`),
  addFeed: (url: string, title?: string) =>
    j<{ ok: boolean; feed: Feed }>(`${BASE}/feeds`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ url, title }),
    }),
  removeFeed: (url: string) =>
    fetch(`${BASE}/feeds?url=${encodeURIComponent(url)}`, { method: 'DELETE' }),
  fetchFeedNow: (url: string) =>
    fetch(`${BASE}/feeds/fetch`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ url }),
    }),

  listen: async (url: string): Promise<{ job_id: number; kind: string; message: string }> => {
    const r = await fetch(`${BASE}/listen`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    if (!r.ok) {
      let detail = `${r.status}`;
      try {
        const body = await r.json() as { detail?: string };
        if (body && typeof body.detail === 'string' && body.detail) detail = body.detail;
      } catch { /* ignore parse errors, fall back to status code */ }
      throw new Error(detail);
    }
    return r.json() as Promise<{ job_id: number; kind: string; message: string }>;
  },

  jobs: () => j<{ jobs: Job[] }>(`${BASE}/jobs`),

  job: (id: number) =>
    j<{ job: {
      id: number;
      agent: string;
      kind: string;
      status: 'queued' | 'running' | 'done' | 'failed';
      attempts: number;
      error: string | null;
      created_at: string;
      started_at: string | null;
      finished_at: string | null;
    } }>(`${BASE}/jobs/${id}`),

  stats: () => j<{
    counts: Record<string, number>;
    feeds_enabled: number;
    jobs: Record<string, number>;
    last_feed_fetch: string | null;
    last_agent_activity: string | null;
    self_files: Record<string, boolean>;
    vault: { path: string; icloud: boolean };
    llm: Record<string, unknown>;
  }>(`${BASE}/stats`),

  pingBridges: () => j<{
    claude: { ok: boolean; error?: string; sample?: string };
    ollama_chat: { ok: boolean; error?: string; sample?: string };
    ollama_embed: { ok: boolean; error?: string; dim?: number };
  }>(`${BASE}/stats/ping-bridges`, { method: 'POST' }),

  article: (id: string) => j<Article>(`${BASE}/articles/${id}`),

  articlePreview: (id: string) => j<ArticlePreview>(`${BASE}/articles/${id}/preview`),

  podcasts: (limit: number = 100) =>
    j<{ items: PodcastListItem[] }>(`${BASE}/podcasts?limit=${limit}`),

  podcast: (article_id: string) =>
    j<PodcastView>(`${BASE}/podcasts/${encodeURIComponent(article_id)}`),

  digest: (date?: string) =>
    j<Digest>(date ? `${BASE}/digest?date=${encodeURIComponent(date)}` : `${BASE}/digest`),

  digestCalendar: (year: number, month: number) =>
    j<{ active_dates: string[] }>(`${BASE}/digest/calendar?year=${year}&month=${month}`),

  digestFeedback: (article_id: string, verdict: 'yes' | 'no' | 'clear', digest_date: string) =>
    fetch(`${BASE}/digest/feedback`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ article_id, verdict, digest_date }),
    }).then(async (r) => {
      if (!r.ok) await throwApiError(r);
      return r.json() as Promise<{ ok: boolean; verdict: 'yes' | 'no' | null }>;
    }),

  digestAudit: (date?: string, window_days: number = 7) => {
    const params = new URLSearchParams({ window_days: String(window_days) });
    if (date) params.set('date', date);
    return j<DigestAudit>(`${BASE}/digest/audit?${params.toString()}`);
  },

  openQuestions: () => j<OpenQuestionsResponse>(`${BASE}/open-questions`),

  feed: () => j<{ feed: FeedTick[]; agents: AgentInfo[] }>(`${BASE}/feed`),

  agents: () => j<{ agents: AgentInfo[] }>(`${BASE}/agents`),

  agentDetail: (id: string) =>
    j<AgentDetail>(`${BASE}/agents/${encodeURIComponent(id)}`),

  updateAgentProfile: (id: string, patch: AgentProfileUpdate) =>
    j<{ profile: AgentDetail['profile'] }>(`${BASE}/agents/${encodeURIComponent(id)}/profile`, {
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(patch),
    }),

  agentSkills: () => j<{ skills: AgentSkill[] }>(`${BASE}/agent-skills`),

  createAgentSkill: (skill: { slug?: string; name: string; description?: string | null; tags?: string[]; body: string }) =>
    j<AgentSkill>(`${BASE}/agent-skills`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(skill),
    }),

  updateAgentSkill: (slug: string, skill: { name: string; description?: string | null; tags?: string[]; body: string }) =>
    j<AgentSkill>(`${BASE}/agent-skills/${encodeURIComponent(slug)}`, {
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(skill),
    }),

  deleteAgentSkill: (slug: string) =>
    j<{ ok: boolean }>(`${BASE}/agent-skills/${encodeURIComponent(slug)}`, {
      method: 'DELETE',
    }),

  ask: (question: string, opts?: {
    selection?: string;
    article_id?: string;
    messages?: { role: 'user' | 'assistant'; content: string }[];
    mode?: 'wiki' | 'research';
    conversation_id?: string;
  }) =>
    j<AskResponse>(`${BASE}/ask`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ question, ...opts }),
    }),

  askHistory: {
    list: (limit = 30) =>
      j<{ conversations: AskConversationSummary[] }>(`${BASE}/ask/conversations?limit=${limit}`),
    get: (conversationId: string) =>
      j<AskConversation>(`${BASE}/ask/conversations/${encodeURIComponent(conversationId)}`),
    delete: (conversationId: string) =>
      j<{ ok: boolean }>(`${BASE}/ask/conversations/${encodeURIComponent(conversationId)}`, {
        method: 'DELETE',
      }),
  },

  /** Unified keyword search across Mastisk mirrors. */
  search: (q: string, opts?: { limit?: number; signal?: AbortSignal }) =>
    fetch(
      `${BASE}/search?q_param=${encodeURIComponent(q)}` +
        (opts?.limit ? `&limit=${opts.limit}` : ''),
      { signal: opts?.signal },
    ).then(async (r) => {
      if (!r.ok) throw new Error(`${r.status}`);
      return r.json() as Promise<{ results: SearchResult[] }>;
    }),

  signalVerdict: (articleId: string) =>
    j<{ verdict: 'liked' | 'disliked' | null }>(
      `${BASE}/signals/verdict?article_id=${encodeURIComponent(articleId)}`,
    ),

  signalDislikedReason: (articleId: string, reason: string): Promise<void> =>
    fetch(`${BASE}/signals/disliked-reason`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ article_id: articleId, reason }),
    }).then((r) => { if (!r.ok) throw new Error(`reason → ${r.status}`); }),

  signal: (kind: string, article_id?: string | null, value?: unknown) =>
    fetch(`${BASE}/signals`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ kind, article_id, value }),
    }).catch(() => void 0),

  pin: (id: string) => fetch(`${BASE}/articles/${id}/pin`, { method: 'POST' }),
  unpin: (id: string) => fetch(`${BASE}/articles/${id}/pin`, { method: 'DELETE' }),

  vaultInfo: () => j<{ vault_path: string; icloud: boolean; self_files: string[] }>(`${BASE}/vault/info`),

  readSelf: (name: string) => j<{ name: string; content: string }>(`${BASE}/vault/self/${name}`),
  writeSelf: (name: string, content: string) =>
    fetch(`${BASE}/vault/self/${name}`, {
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ content }),
    }),

  vaultFile: {
    read: (path: string): Promise<{ path: string; content: string; content_sha256: string }> =>
      j<{ path: string; content: string; content_sha256: string }>(
        `${BASE}/vault/file?path=${encodeURIComponent(path)}`,
      ),
    write: (
      path: string,
      content: string,
      base_sha256: string,
    ): Promise<{ ok: boolean; path: string; content_sha256: string; rescan_failed?: boolean; rescan_error?: string }> =>
      j<{ ok: boolean; path: string; content_sha256: string; rescan_failed?: boolean; rescan_error?: string }>(`${BASE}/vault/file`, {
        method: 'PUT',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ path, content, base_sha256 }),
      }),
  },

  editing: {
    lock: (path: string): Promise<{ path: string; token: string; status: string }> =>
      j<{ path: string; token: string; status: string }>(`${BASE}/editing/lock`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ path }),
      }),
    unlock: (path: string, token: string): Promise<{ path: string; token: string; status: string }> =>
      j<{ path: string; token: string; status: string }>(`${BASE}/editing/unlock`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ path, token }),
      }),
    heartbeat: (path: string, token: string): Promise<{ path: string; token: string; status: string }> =>
      j<{ path: string; token: string; status: string }>(`${BASE}/editing/heartbeat`, {
        method: 'PUT',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ path, token }),
      }),
  },

  attachments: {
    upload: async (file: File): Promise<{ path: string; markdown: string }> => {
      const form = new FormData();
      form.append('file', file);
      const r = await fetch(`${BASE}/attachments`, { method: 'POST', body: form });
      if (!r.ok) await throwApiError(r);
      return r.json() as Promise<{ path: string; markdown: string }>;
    },
  },

  ingest: {
    uploadDocument: async (file: File): Promise<{ queued: boolean; job_id: number; status: string; source_path: string; result?: unknown }> => {
      const form = new FormData();
      form.append('file', file);
      const r = await fetch(`${BASE}/ingest/document`, { method: 'POST', body: form });
      if (!r.ok) await throwApiError(r);
      return r.json() as Promise<{ queued: boolean; job_id: number; status: string; source_path: string; result?: unknown }>;
    },
    job: (id: number): Promise<{ job: {
      id: number;
      agent: string;
      kind: string;
      status: 'queued' | 'running' | 'done' | 'failed';
      attempts: number;
      error: string | null;
      created_at: string;
      started_at: string | null;
      finished_at: string | null;
      result: unknown;
      detail: { title: string | null; source_path: string | null };
    } }> => j<{ job: {
      id: number;
      agent: string;
      kind: string;
      status: 'queued' | 'running' | 'done' | 'failed';
      attempts: number;
      error: string | null;
      created_at: string;
      started_at: string | null;
      finished_at: string | null;
      result: unknown;
      detail: { title: string | null; source_path: string | null };
    } }>(`${BASE}/ingest/jobs/${id}`),
    uploadJournalPhoto: async (file: File, day: string): Promise<{
      status: string;
      ocr_status: 'done' | 'unavailable';
      status_code?: number;
      reason?: string;
      text?: string;
      attachment: { path: string; markdown: string };
      journal: { date: string; path: string; line: string };
    }> => {
      const form = new FormData();
      form.append('photo', file);
      form.append('date', day);
      const r = await fetch(`${BASE}/ingest/journal-photo`, { method: 'POST', body: form });
      if (!r.ok) await throwApiError(r);
      return r.json() as Promise<{
        status: string;
        ocr_status: 'done' | 'unavailable';
        status_code?: number;
        reason?: string;
        text?: string;
        attachment: { path: string; markdown: string };
        journal: { date: string; path: string; line: string };
      }>;
    },
  },

  artifacts: (articleId: string) =>
    j<{ artifacts: Artifact[] }>(`${BASE}/articles/${articleId}/artifacts`),

  createArtifact: (
    articleId: string,
    body: { kind: ArtifactKind; title: string; description?: string | null; spec: Record<string, unknown> },
  ) =>
    j<Artifact>(`${BASE}/articles/${articleId}/artifacts`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    }),

  updateArtifact: (
    id: number,
    patch: { title?: string; description?: string | null; spec?: Record<string, unknown> },
  ) =>
    j<Artifact>(`${BASE}/artifacts/${id}`, {
      method: 'PATCH',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(patch),
    }),

  deleteArtifact: (id: number) =>
    fetch(`${BASE}/artifacts/${id}`, { method: 'DELETE' }),

  regenerateArtifacts: (articleId: string) =>
    j<{ queued: boolean; job_id?: number | string }>(
      `${BASE}/articles/${articleId}/artifacts/regenerate`,
      { method: 'POST' },
    ),

  settings: () => j<SettingsBundle>(`${BASE}/settings`),
  saveSettings: (patch: SettingsPatch) =>
    j<{ ok: boolean }>(`${BASE}/settings`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(patch),
    }),

  synthesisForArticle: (articleId: string) =>
    j<SynthesisRunResponse>(`${BASE}/synthesis/by-article/${encodeURIComponent(articleId)}`),

  pendingSynthesis: () => j<PendingSynthesisResponse>(`${BASE}/synthesis/pending`),

  acceptSynthesis: (runId: number) =>
    j<{ ok: boolean }>(`${BASE}/synthesis/${runId}/accept`, { method: 'POST' }),

  rejectSynthesis: (runId: number, feedback?: string) =>
    j<{ ok: boolean }>(`${BASE}/synthesis/${runId}/reject`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ feedback: feedback ?? null }),
    }),

  notes: {
    create: (
      text: string,
      context?: { article_id: string; section_heading?: string; question_html?: string },
      transcript_anchor?: TranscriptAnchor | null,
    ): Promise<{ id: number; slug: string; path: string; linked_article_id: string | null }> =>
      fetch('/api/notes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          text, source: 'pwa',
          ...(context ? { context } : {}),
          ...(transcript_anchor ? { transcript_anchor } : {}),
        }),
      }).then(r => { if (!r.ok) throw new Error(`${r.status}`); return r.json(); }),

    list: (limit = 50): Promise<Note[]> =>
      fetch(`/api/notes?limit=${limit}`).then(r => r.json()),

    get: (id: number): Promise<Note> =>
      fetch(`/api/notes/${id}`).then(r => {
        if (r.status === 404) throw new Error('not found');
        return r.json();
      }),

    delete: (id: number): Promise<void> =>
      fetch(`/api/notes/${id}`, { method: 'DELETE' }).then(r => {
        if (!r.ok) throw new Error(`${r.status}`);
      }),

    forArticle: (articleId: string, limit = 50): Promise<Note[]> =>
      fetch(`/api/articles/${encodeURIComponent(articleId)}/notes?limit=${limit}`).then(r => r.json()),

    escalate: (id: number): Promise<{ note_id: number; escalation_state: string }> =>
      fetch(`/api/notes/${id}/escalate`, { method: 'POST' }).then(r => {
        if (!r.ok) throw new Error(`${r.status}`);
        return r.json();
      }),
  },

  tasks: {
    list: (opts: { status?: string; domain?: string; project?: string; due_before?: string } = {}):
      Promise<TaskRow[]> => {
      const params = new URLSearchParams();
      if (opts.status) params.set('status', opts.status);
      if (opts.domain) params.set('domain', opts.domain);
      if (opts.project) params.set('project', opts.project);
      if (opts.due_before) params.set('due_before', opts.due_before);
      const qs = params.toString();
      return j<TaskRow[]>(qs ? `${BASE}/tasks?${qs}` : `${BASE}/tasks`);
    },
    create: (body: { text: string; due?: string | null; priority?: 'high' | 'medium' | 'low' | null; project?: string | null; host_path?: string | null }):
      Promise<TaskRow> =>
      j<TaskRow>(`${BASE}/tasks`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      }),
    toggle: (uid: string): Promise<TaskRow> =>
      j<TaskRow>(`${BASE}/tasks/${encodeURIComponent(uid)}/toggle`, { method: 'PATCH' }),
    patch: (uid: string, body: { due?: string | null; scheduled?: string | null; recurrence?: string | null; priority?: 'high' | 'medium' | 'low' | null; staleness_days?: number | null }):
      Promise<TaskRow> =>
      j<TaskRow>(`${BASE}/tasks/${encodeURIComponent(uid)}`, {
        method: 'PATCH',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      }),
  },

  focus: {
    list: (day: string): Promise<TaskRow[]> =>
      j<TaskRow[]>(`${BASE}/focus/${encodeURIComponent(day)}`),
    add: (day: string, taskUid: string, replaceUid?: string): Promise<TaskRow[]> =>
      fetch(`${BASE}/focus/${encodeURIComponent(day)}`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ task_uid: taskUid, replace_uid: replaceUid ?? null }),
      }).then(focusResponse),
    remove: (day: string, taskUid: string): Promise<TaskRow[]> =>
      j<TaskRow[]>(`${BASE}/focus/${encodeURIComponent(day)}/${encodeURIComponent(taskUid)}`, {
        method: 'DELETE',
      }),
  },

  slipping: {
    list: (): Promise<SlippingItem[]> => j<SlippingItem[]>(`${BASE}/slipping`),
    snooze: (entityType: string, entityId: string, days = 7): Promise<{ ok: boolean }> =>
      j<{ ok: boolean }>(
        `${BASE}/slipping/${encodeURIComponent(entityType)}/${encodeURIComponent(entityId)}/snooze`,
        {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ days }),
        },
      ),
    mute: (entityType: string, entityId: string): Promise<{ ok: boolean }> =>
      j<{ ok: boolean }>(
        `${BASE}/slipping/${encodeURIComponent(entityType)}/${encodeURIComponent(entityId)}/mute`,
        { method: 'POST' },
      ),
    unmute: (entityType: string, entityId: string): Promise<{ ok: boolean }> =>
      j<{ ok: boolean }>(
        `${BASE}/slipping/${encodeURIComponent(entityType)}/${encodeURIComponent(entityId)}/unmute`,
        { method: 'POST' },
      ),
  },

  resurface: {
    get: async (day: string): Promise<ResurfaceItem | null> => {
      const r = await fetch(`${BASE}/resurface/${encodeURIComponent(day)}`);
      if (r.status === 204) return null;
      if (!r.ok) await throwApiError(r);
      return r.json() as Promise<ResurfaceItem>;
    },
  },

  needsReview: {
    list: (): Promise<NeedsReviewItem[]> => j<NeedsReviewItem[]>(`${BASE}/needs-review`),
    dismiss: (id: number): Promise<NeedsReviewItem> =>
      j<NeedsReviewItem>(`${BASE}/needs-review/${id}/dismiss`, { method: 'POST' }),
  },

  projectsApi: {
    list: (): Promise<ProjectSummary[]> => j<ProjectSummary[]>(`${BASE}/projects`),
    get: (slug: string): Promise<ProjectDetail> => j<ProjectDetail>(`${BASE}/projects/${encodeURIComponent(slug)}`),
    create: (body: { name: string; type?: string; domain?: string | null; due?: string | null; template?: string | null; recurring_items?: string[] }):
      Promise<ProjectDetail> =>
      j<ProjectDetail>(`${BASE}/projects`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      }),
    patch: (slug: string, body: { status?: string | null; domain?: string | null; due?: string | null; recurring_items?: string[] | null }):
      Promise<ProjectSummary> =>
      j<ProjectSummary>(`${BASE}/projects/${encodeURIComponent(slug)}`, {
        method: 'PATCH',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      }),
    addMilestone: (slug: string, text: string): Promise<ProjectDetail> =>
      j<ProjectDetail>(`${BASE}/projects/${encodeURIComponent(slug)}/milestones`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ text }),
      }),
    setMilestoneDone: (slug: string, position: number, done: boolean, expectedText: string): Promise<ProjectDetail> =>
      j<ProjectDetail>(`${BASE}/projects/${encodeURIComponent(slug)}/milestones/${position}`, {
        method: 'PATCH',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ done, expected_text: expectedText }),
      }),
    addTime: (slug: string, body: { date?: string | null; hours: number; text: string }): Promise<ProjectDetail> =>
      j<ProjectDetail>(`${BASE}/projects/${encodeURIComponent(slug)}/time`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      }),
    checklistTemplates: (): Promise<ChecklistTemplate[]> =>
      j<ChecklistTemplate[]>(`${BASE}/templates/checklists`),
  },

  domainsApi: {
    list: (): Promise<Domain[]> => j<Domain[]>(`${BASE}/domains`),
  },

  routinesApi: {
    list: (archived = false): Promise<RoutineGroups> =>
      j<RoutineGroups>(archived ? `${BASE}/routines?archived=true` : `${BASE}/routines`),
    create: (body: { name: string; time_of_day?: TimeOfDay; streak_type?: 'ongoing' | 'fixed'; target_days?: number | null; start_date?: string | null }):
      Promise<RoutineRow> =>
      j<RoutineRow>(`${BASE}/routines`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      }),
    toggle: (slug: string): Promise<RoutineRow> =>
      j<RoutineRow>(`${BASE}/routines/${encodeURIComponent(slug)}/toggle`, { method: 'POST' }),
    archive: (slug: string): Promise<RoutineRow> =>
      j<RoutineRow>(`${BASE}/routines/${encodeURIComponent(slug)}/archive`, { method: 'POST' }),
    progress: (slug: string, days = 30): Promise<RoutineProgress> =>
      j<RoutineProgress>(`${BASE}/routines/${encodeURIComponent(slug)}/progress?days=${days}`),
  },

  journalApi: {
    list: (limit = 30): Promise<JournalDaySummary[]> =>
      j<JournalDaySummary[]>(`${BASE}/journal?limit=${limit}`),
    get: (day: string): Promise<JournalDay> => j<JournalDay>(`${BASE}/journal/${encodeURIComponent(day)}`),
    appendLog: (day: string, text: string): Promise<{ date: string; path: string; line: string }> =>
      j<{ date: string; path: string; line: string }>(`${BASE}/journal/${encodeURIComponent(day)}/log`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ text }),
      }),
    patch: (day: string, body: { mood?: number | null; energy?: number | null; reflections?: string | null }):
      Promise<JournalDay> =>
      j<JournalDay>(`${BASE}/journal/${encodeURIComponent(day)}`, {
        method: 'PATCH',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      }),
  },

  peopleApi: {
    list: (): Promise<PersonSummary[]> => j<PersonSummary[]>(`${BASE}/people`),
    get: (slug: string): Promise<PersonDetail> => j<PersonDetail>(`${BASE}/people/${encodeURIComponent(slug)}`),
    create: (body: { name: string; birthday?: string | null; anniversary?: string | null; facts?: Record<string, unknown>; body?: string | null }):
      Promise<PersonDetail> =>
      j<PersonDetail>(`${BASE}/people`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      }),
    patch: (slug: string, body: { birthday?: string | null; anniversary?: string | null; facts?: Record<string, unknown> | null; follow_up_at?: string | null; body?: string | null }):
      Promise<PersonDetail> =>
      j<PersonDetail>(`${BASE}/people/${encodeURIComponent(slug)}`, {
        method: 'PATCH',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      }),
    addInteraction: (slug: string, text: string, ts?: string | null): Promise<PersonDetail> =>
      j<PersonDetail>(`${BASE}/people/${encodeURIComponent(slug)}/interactions`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ text, ts: ts || null }),
      }),
    delete: (slug: string): Promise<PersonDetail> =>
      j<PersonDetail>(`${BASE}/people/${encodeURIComponent(slug)}`, { method: 'DELETE' }),
  },

  inventoryApi: {
    list: (filters: { status?: string; location?: string } = {}): Promise<InventoryList> => {
      const params = new URLSearchParams();
      if (filters.status) params.set('status', filters.status);
      if (filters.location) params.set('location', filters.location);
      const qs = params.toString();
      return j<InventoryList>(qs ? `${BASE}/inventory?${qs}` : `${BASE}/inventory`);
    },
    get: (id: string): Promise<InventoryDetail> =>
      j<InventoryDetail>(`${BASE}/inventory/${encodeURIComponent(id)}`),
    create: (body: { name: string; acquired?: string | null; value?: number | null; status?: InventoryStatus | string; location?: string | null; photo?: string | null; notes?: string | null }):
      Promise<InventoryDetail> =>
      j<InventoryDetail>(`${BASE}/inventory`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      }),
    patch: (id: string, body: { name?: string | null; acquired?: string | null; value?: number | null; status?: InventoryStatus | string | null; location?: string | null; photo?: string | null; notes?: string | null }):
      Promise<InventoryDetail> =>
      j<InventoryDetail>(`${BASE}/inventory/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      }),
    delete: (id: string): Promise<InventoryDetail> =>
      j<InventoryDetail>(`${BASE}/inventory/${encodeURIComponent(id)}`, { method: 'DELETE' }),
    exportUrl: `${BASE}/inventory/export`,
  },

  contentApi: {
    list: (filters: { kind?: string; status?: string; domain?: string } = {}): Promise<ContentList> => {
      const params = new URLSearchParams();
      if (filters.kind) params.set('kind', filters.kind);
      if (filters.status) params.set('status', filters.status);
      if (filters.domain) params.set('domain', filters.domain);
      const qs = params.toString();
      return j<ContentList>(qs ? `${BASE}/content?${qs}` : `${BASE}/content`);
    },
    get: (slug: string): Promise<ContentDetail> =>
      j<ContentDetail>(`${BASE}/content/${encodeURIComponent(slug)}`),
    create: (body: { title: string; kind: ContentKind | string; status?: ContentStatus | string; domain?: string | null; channel?: string | null; url?: string | null; publish_date?: string | null; outline?: string | null; checklist_template?: string | null }):
      Promise<ContentDetail> =>
      j<ContentDetail>(`${BASE}/content`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      }),
    patch: (slug: string, body: { status?: ContentStatus | string | null; domain?: string | null; channel?: string | null; url?: string | null; publish_date?: string | null }):
      Promise<ContentDetail> =>
      j<ContentDetail>(`${BASE}/content/${encodeURIComponent(slug)}`, {
        method: 'PATCH',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      }),
    draft: (slug: string): Promise<{ blog_post_id: number; status: string }> =>
      j<{ blog_post_id: number; status: string }>(
        `${BASE}/content/${encodeURIComponent(slug)}/draft`,
        { method: 'POST' },
      ),
    delete: (slug: string): Promise<void> =>
      fetch(`${BASE}/content/${encodeURIComponent(slug)}`, { method: 'DELETE' }).then(async (r) => {
        if (!r.ok) await throwApiError(r);
      }),
  },

  libraryApi: {
    books: {
      list: (status?: string): Promise<BookSummary[]> =>
        j<BookSummary[]>(status ? `${BASE}/books?status=${encodeURIComponent(status)}` : `${BASE}/books`),
      get: (slug: string): Promise<BookDetail> =>
        j<BookDetail>(`${BASE}/books/${encodeURIComponent(slug)}`),
      create: (body: { title: string; author?: string | null; isbn?: string | null; lookup?: boolean }):
        Promise<BookDetail> =>
        j<BookDetail>(`${BASE}/books`, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(body),
        }),
      patch: (slug: string, body: { status?: BookStatus | string | null; rating?: number | null; started?: string | null; finished?: string | null; format?: string | null; summary?: string | null; cover_url?: string | null; isbn?: string | null; author?: string | null }):
        Promise<BookDetail> =>
        j<BookDetail>(`${BASE}/books/${encodeURIComponent(slug)}`, {
          method: 'PATCH',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(body),
        }),
      refresh: (slug: string): Promise<BookDetail> =>
        j<BookDetail>(`${BASE}/books/${encodeURIComponent(slug)}/refresh-metadata`, { method: 'POST' }),
      addHighlight: (slug: string, text: string): Promise<BookHighlight> =>
        j<BookHighlight>(`${BASE}/books/${encodeURIComponent(slug)}/highlights`, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ text }),
        }),
    },
    quotes: {
      list: (filters?: { source_type?: string; tag?: string }): Promise<QuoteSummary[]> => {
        const params = new URLSearchParams();
        if (filters?.source_type) params.set('source_type', filters.source_type);
        if (filters?.tag) params.set('tag', filters.tag);
        const qs = params.toString();
        return j<QuoteSummary[]>(qs ? `${BASE}/quotes?${qs}` : `${BASE}/quotes`);
      },
      get: (id: string): Promise<QuoteDetail> =>
        j<QuoteDetail>(`${BASE}/quotes/${encodeURIComponent(id)}`),
      create: (body: { text: string; source_type?: QuoteSourceType | string; source_ref?: string | null; tags?: string[] }):
        Promise<QuoteDetail> =>
        j<QuoteDetail>(`${BASE}/quotes`, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(body),
        }),
      addThought: (id: string, text: string, ts?: string | null): Promise<QuoteDetail> =>
        j<QuoteDetail>(`${BASE}/quotes/${encodeURIComponent(id)}/thoughts`, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ text, ts: ts || null }),
        }),
    },
    kindle: {
      importFile: async (file: File): Promise<KindleImportResult> => {
        const form = new FormData();
        form.append('file', file);
        const r = await fetch(`${BASE}/import/kindle`, { method: 'POST', body: form });
        if (!r.ok) await throwApiError(r);
        return r.json() as Promise<KindleImportResult>;
      },
      review: (): Promise<KindleReviewItem[]> =>
        j<KindleReviewItem[]>(`${BASE}/import/kindle/review`),
      dismiss: (id: number): Promise<KindleReviewItem> =>
        j<KindleReviewItem>(`${BASE}/import/kindle/review/${id}/dismiss`, { method: 'POST' }),
      retryAsQuote: (id: number, body: { text?: string | null; source_type?: QuoteSourceType | string; source_ref?: string | null; tags?: string[] } = {}):
        Promise<KindleReviewItem> =>
        j<KindleReviewItem>(`${BASE}/import/kindle/review/${id}/retry-as-quote`, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(body),
        }),
    },
  },

  remindersApi: {
    list: (status?: string): Promise<ReminderRow[]> =>
      j<ReminderRow[]>(status ? `${BASE}/reminders?status=${encodeURIComponent(status)}` : `${BASE}/reminders`),
  },

  calendar: {
    today: (day?: string): Promise<CalendarToday> =>
      j<CalendarToday>(
        day ? `${BASE}/calendar/today?date=${encodeURIComponent(day)}` : `${BASE}/calendar/today`,
      ),
    status: (): Promise<CalendarStatus> => j<CalendarStatus>(`${BASE}/calendar/status`),
    saveConfig: (clientId: string, clientSecret: string): Promise<{ ok: boolean; status: CalendarStatus }> =>
      j<{ ok: boolean; status: CalendarStatus }>(`${BASE}/calendar/config`, {
        method: 'PUT',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ client_id: clientId, client_secret: clientSecret }),
      }),
    startConnection: (): Promise<{ authorization_url: string; redirect_uri: string }> =>
      j<{ authorization_url: string; redirect_uri: string }>(`${BASE}/calendar/connection/start`, {
        method: 'POST',
      }),
    sync: (): Promise<{ ok: boolean; event_count: number; calendar_count: number; status: CalendarStatus }> =>
      j<{ ok: boolean; event_count: number; calendar_count: number; status: CalendarStatus }>(
        `${BASE}/calendar/sync`,
        { method: 'POST' },
      ),
    disconnect: (): Promise<{ ok: boolean }> =>
      j<{ ok: boolean }>(`${BASE}/calendar/connection`, { method: 'DELETE' }),
  },

  integrations: {
    health: (): Promise<IntegrationHealth> => j<IntegrationHealth>(`${BASE}/health/integrations`),
  },

  captureTriage: {
    list: (limit = 100): Promise<CaptureTriageItem[]> =>
      j<CaptureTriageItem[]>(`${BASE}/triage?limit=${limit}`),
    reclassify: (id: string, type: CaptureTriageTarget): Promise<{ ok: boolean; id: string; type: CaptureTriageTarget }> =>
      j<{ ok: boolean; id: string; type: CaptureTriageTarget }>(
        `${BASE}/triage/${encodeURIComponent(id)}/reclassify`,
        {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ type }),
        },
      ),
  },

  roundtables: {
    create: (input_type: 'note' | 'article' | 'prompt', input_ref: string, prompt: string):
      Promise<{ id: number; status: string }> =>
      fetch('/api/roundtables', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ input_type, input_ref, prompt }),
      }).then(r => { if (!r.ok) throw new Error(`${r.status}`); return r.json(); }),

    list: (limit = 50): Promise<RoundtableSummary[]> =>
      fetch(`/api/roundtables?limit=${limit}`).then(r => r.json()),

    get: (id: number): Promise<Roundtable> =>
      fetch(`/api/roundtables/${id}`).then(async r => {
        if (!r.ok) await throwApiError(r);
        return r.json();
      }),

    saveAsNote: (id: number): Promise<{ note_id: number; slug: string; reused: boolean }> =>
      fetch(`/api/roundtables/${id}/save-as-note`, { method: 'POST' }).then(r => {
        if (!r.ok) throw new Error(`${r.status}`);
        return r.json();
      }),
  },

  blog: {
    create: (body: { theme?: string; window_days: number }, signal?: AbortSignal):
      Promise<{ id: number; status: string }> =>
      fetch('/api/blog-posts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          theme: body.theme ?? '',
          window_days: body.window_days,
        }),
        signal,
      }).then(async r => {
        if (!r.ok) await throwApiError(r);
        return r.json();
      }),

    list: (opts: { limit?: number; before?: number } = {}): Promise<BlogPostSummary[]> => {
      const params = new URLSearchParams();
      if (opts.limit) params.set('limit', String(opts.limit));
      if (opts.before !== undefined) params.set('before', String(opts.before));
      const qs = params.toString();
      const url = qs ? `/api/blog-posts?${qs}` : '/api/blog-posts';
      return fetch(url).then(async r => {
        if (!r.ok) await throwApiError(r);
        return r.json();
      });
    },

    get: (id: number): Promise<BlogPostDetail> =>
      fetch(`/api/blog-posts/${id}`).then(async r => {
        if (!r.ok) await throwApiError(r);
        return r.json();
      }),

    regenerate: (id: number): Promise<{ id: number; status: string }> =>
      fetch(`/api/blog-posts/${id}/regenerate`, { method: 'POST' }).then(async r => {
        if (!r.ok) await throwApiError(r);
        return r.json();
      }),

    saveAsNote: (id: number): Promise<{ note_id: number; slug: string; reused: boolean }> =>
      fetch(`/api/blog-posts/${id}/save-as-note`, { method: 'POST' }).then(async r => {
        if (!r.ok) await throwApiError(r);
        return r.json();
      }),

    delete: (id: number): Promise<void> =>
      fetch(`/api/blog-posts/${id}`, { method: 'DELETE' }).then(async r => {
        if (!r.ok) await throwApiError(r);
      }),
  },

  tweets: {
    create: (body: {
      theme?: string;
      url?: string;
      window_days: number;
      include_web: boolean;
      use_browser_context: boolean;
    }, signal?: AbortSignal): Promise<{ id: number; status: string }> =>
      fetch('/api/tweet-threads', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          theme: body.theme ?? '',
          url: body.url?.trim() || null,
          window_days: body.window_days,
          include_web: body.include_web,
          use_browser_context: body.use_browser_context,
        }),
        signal,
      }).then(async r => {
        if (!r.ok) await throwApiError(r);
        return r.json();
      }),

    list: (opts: { limit?: number; before?: number } = {}): Promise<TweetThread[]> => {
      const params = new URLSearchParams();
      if (opts.limit) params.set('limit', String(opts.limit));
      if (opts.before !== undefined) params.set('before', String(opts.before));
      const qs = params.toString();
      const url = qs ? `/api/tweet-threads?${qs}` : '/api/tweet-threads';
      return fetch(url).then(async r => {
        if (!r.ok) await throwApiError(r);
        return r.json();
      });
    },

    get: (id: number): Promise<TweetThread> =>
      fetch(`/api/tweet-threads/${id}`).then(async r => {
        if (!r.ok) await throwApiError(r);
        return r.json();
      }),

    regenerate: (id: number): Promise<{ id: number; status: string }> =>
      fetch(`/api/tweet-threads/${id}/regenerate`, { method: 'POST' }).then(async r => {
        if (!r.ok) await throwApiError(r);
        return r.json();
      }),

    feedback: (
      id: number,
      body: { body: string; target_tweet_index?: number | null; rework?: boolean },
    ): Promise<{ id: number; status: string; feedback_id: number; rework_queued: boolean }> =>
      fetch(`/api/tweet-threads/${id}/feedback`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          body: body.body,
          target_tweet_index: body.target_tweet_index ?? null,
          rework: body.rework ?? true,
        }),
      }).then(async r => {
        if (!r.ok) await throwApiError(r);
        return r.json();
      }),

    delete: (id: number): Promise<void> =>
      fetch(`/api/tweet-threads/${id}`, { method: 'DELETE' }).then(async r => {
        if (!r.ok) await throwApiError(r);
      }),
  },

  topicSuggestions: {
    list: () => j<{ suggestions: TopicSuggestion[] }>(`${BASE}/topic-suggestions`),
    dismiss: (id: number): Promise<void> =>
      fetch(`${BASE}/topic-suggestions/${id}/dismiss`, { method: 'POST' })
        .then((r) => { if (!r.ok) throw new Error(`dismiss → ${r.status}`); }),
  },

  automations: {
    list: () => j<{ automations: Automation[] }>(`${BASE}/automations`),
    get: (slug: string) => j<AutomationDetail>(`${BASE}/automations/${encodeURIComponent(slug)}`),
    create: (body: {
      name: string; instructions: string;
      triggers?: AutomationTriggers; model?: string; active?: boolean;
    }): Promise<Automation> =>
      fetch(`${BASE}/automations`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }).then(async (r) => {
        if (!r.ok) await throwApiError(r);
        return r.json();
      }),
    patch: (slug: string, body: Partial<{
      name: string; instructions: string;
      triggers: AutomationTriggers; model: string | null; active: boolean;
    }>): Promise<Automation> =>
      fetch(`${BASE}/automations/${encodeURIComponent(slug)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }).then(async (r) => {
        if (!r.ok) await throwApiError(r);
        return r.json();
      }),
    run: (slug: string): Promise<AutomationRun> =>
      fetch(`${BASE}/automations/${encodeURIComponent(slug)}/run`, { method: 'POST' })
        .then(async (r) => {
          if (!r.ok) await throwApiError(r);
          return r.json();
        }),
  },

  wikiSuggestions: {
    list: (status: 'pending' | 'promoted' | 'dismissed' = 'pending') =>
      j<{ suggestions: WikiSuggestion[] }>(`${BASE}/suggestions?status=${status}`),
    decide: (slug: string, action: 'promote' | 'dismiss' | 'restore'): Promise<WikiSuggestion> =>
      fetch(`${BASE}/suggestions/${encodeURIComponent(slug)}/${action}`, { method: 'POST' })
        .then((r) => {
          if (!r.ok) throw new Error(`${action} → ${r.status}`);
          return r.json();
        }),
  },

  github: {
    get: (): Promise<{
      pat_set: boolean; pat_last4: string | null;
      poll_interval_minutes: number; ideate_min_interval_hours: number; ideas_per_run: number;
    }> =>
      fetch(`${BASE}/settings/github`).then(r => r.json()),

    setPat: (pat: string): Promise<{ pat_set: boolean; pat_last4: string | null }> =>
      fetch(`${BASE}/settings/github/pat`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pat }),
      }).then(async r => {
        if (!r.ok) {
          const msg = await r.text();
          throw new Error(msg);
        }
        return r.json();
      }),

    testPat: (): Promise<{ ok: boolean; username: string; name: string | null; public_repos: number }> =>
      fetch(`${BASE}/settings/github/test`).then(async r => {
        if (!r.ok) {
          let detail = String(r.status);
          try {
            const body = await r.json() as { detail?: string };
            if (body && typeof body.detail === 'string' && body.detail) detail = body.detail;
          } catch { /* fall back to status code */ }
          throw new Error(detail);
        }
        return r.json();
      }),
  },

  repos: {
    add: (slug: string): Promise<{ slug: string; display_name: string; description: string | null }> =>
      fetch('/api/repos', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ slug }),
      }).then(async r => {
        if (!r.ok) {
          const msg = await r.text();
          throw new Error(`${r.status}: ${msg.slice(0, 200)}`);
        }
        return r.json();
      }),

    addLocal: (path: string): Promise<{ slug: string; local_path: string; display_name: string }> =>
      fetch('/api/repos/local', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path }),
      }).then(async r => {
        if (!r.ok) {
          const msg = await r.text();
          throw new Error(`${r.status}: ${msg.slice(0, 200)}`);
        }
        return r.json();
      }),

    list: (): Promise<RepoSummary[]> =>
      fetch('/api/repos').then(r => r.json()),

    get: (slug: string): Promise<RepoDetail> =>
      // Slugs can be `owner/name` or `local:/Users/...`; the `{slug:path}`
      // backend route captures the raw remainder. Leave slashes and colons
      // intact — fetch already percent-encodes unsafe chars in the rest.
      fetch(`/api/repos/by-slug/${slug}`).then(r => {
        if (!r.ok) throw new Error(`${r.status}`);
        return r.json();
      }),

    delete: (slug: string): Promise<void> =>
      fetch(`/api/repos/by-slug/${slug}`, { method: 'DELETE' }).then(r => {
        if (!r.ok) throw new Error(`${r.status}`);
      }),

    pollNow: (slug: string): Promise<void> =>
      fetch(`/api/repos/poll/${slug}`, { method: 'POST' }).then(r => {
        if (!r.ok) throw new Error(`${r.status}`);
      }),

    ideateNow: (slug: string): Promise<void> =>
      fetch(`/api/repos/ideate/${slug}`, { method: 'POST' }).then(r => {
        if (!r.ok) throw new Error(`${r.status}`);
      }),

    ideas: (slug: string): Promise<RepoIdeasResponse> =>
      fetch(`/api/repos/ideas/${slug}`).then(r => {
        if (!r.ok) throw new Error(`${r.status}`);
        return r.json();
      }),

    browse: (path?: string): Promise<{
      path: string;
      parent: string | null;
      entries: { name: string; path: string; is_dir: boolean; is_git_repo: boolean }[];
    }> => {
      const qs = path ? `?path=${encodeURIComponent(path)}` : '';
      return fetch(`/api/repos/browse${qs}`).then(async r => {
        if (!r.ok) throw new Error(await r.text());
        return r.json();
      });
    },
  },
};
