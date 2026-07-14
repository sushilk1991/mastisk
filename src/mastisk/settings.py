"""Runtime configuration. Load order:

1. Environment variables (MASTISK_*, OLLAMA_*, CLAUDE_CMD)
2. ~/Library/Application Support/Mastisk/config.toml
3. .env (dev only)
4. Defaults
"""
from __future__ import annotations

import os
import tempfile
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import tomli_w
from dotenv import load_dotenv
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from mastisk.paths import config_path


class AgentBudget(BaseSettings):
    scout: int = 500
    listener: int = 20
    compiler: int = 100
    linter: int = 50
    synthesizer: int = 10


class NotesSettings(BaseSettings):
    """Config for the notes subsystem. See docs/superpowers/specs/2026-04-21-notes-subsystem-design.md §8."""
    classify_stable_mtime_seconds: int = 30
    auto_escalate_cap: int = 20
    auto_escalate_min_confidence: float = 0.7
    auto_escalate_min_length: int = 80
    auto_escalate_classifications: list[str] = Field(default_factory=lambda: ["idea", "question"])
    dedup_hours: int = 24
    dedup_similarity_threshold: float = 0.85
    claude_retry_count: int = 2
    claude_retry_backoff_mins: list[int] = Field(default_factory=lambda: [30, 60])
    # When unset, the notetaker falls back to top-level summarize_model_cheap
    # (which the Settings UI actually exposes). The old "llama3.1:8b" hardcode
    # silently broke classify for anyone who hadn't pulled that specific model.
    notetaker_model: str | None = None
    # Escalator's Ollama-tier fallback model. None → top-level
    # summarize_model_heavy. The old default ``"claude-sonnet-4-6"`` was a bug
    # — that's a Claude model name being passed as an Ollama model, which made
    # every Claude+Codex-exhausted escalation 404 against /api/chat.
    escalator_model: str | None = None
    notetaker_concurrency: int = 4


class CaptureSettings(BaseSettings):
    """Config for the token-authenticated capture ingress."""
    bearer_token: str | None = None
    default_timezone: str = "America/Los_Angeles"
    router_timeout_s: int = 25


class IntelligenceSettings(BaseSettings):
    """Shared LLM fallback order for intelligence-heavy agents.

    "anthropic" is the direct Anthropic Messages API (fast, no CLI subprocess).
    It is auto-prepended at call time when an API key is configured and the
    user hasn't listed it explicitly — see intelligence.effective_order().
    """
    provider_order: list[str] = Field(
        default_factory=lambda: ["codex", "claude", "ollama"]
    )
    # Anthropic API tier. Key comes from top-level anthropic_api_key
    # (config.toml) or the ANTHROPIC_API_KEY env var. anthropic_auto=false
    # stops the tier from being auto-prepended when a key is present —
    # explicit listing in provider_order still works.
    anthropic_auto: bool = True
    anthropic_model: str = "claude-haiku-4-5-20251001"
    anthropic_max_tokens: int = 8192
    # Model pins for the CLI tiers — intelligence chain only (roundtable,
    # blog-writer, etc. keep their own model handling). The codex CLI's
    # default model runs deep reasoning and was observed timing out at
    # 180-300s per call under the daemon; gpt-5.6-luna at low effort answers
    # a full 60k-char compile prompt in under a minute on a ChatGPT
    # subscription. Set to None (or "" in TOML) to use each CLI's default.
    codex_model: str | None = "gpt-5.6-luna"
    codex_reasoning_effort: str | None = "low"
    claude_model: str | None = "haiku"
    # Circuit breaker: after this many CONSECUTIVE failures a provider is
    # skipped for cooldown_s seconds. Prevents a dead CLI tier (e.g. codex
    # hanging to its 180s timeout on every call) from taxing every request.
    breaker_failure_threshold: int = 3
    breaker_cooldown_s: int = 900

    @field_validator("provider_order")
    @classmethod
    def _validate_provider_order(cls, value: list[str]) -> list[str]:
        allowed = {"anthropic", "codex", "claude", "ollama"}
        if not value:
            raise ValueError("provider_order must include at least one provider")
        seen: set[str] = set()
        order: list[str] = []
        for raw in value:
            provider = raw.strip().lower() if isinstance(raw, str) else ""
            if provider not in allowed:
                raise ValueError(
                    "provider_order entries must be one of: anthropic, codex, claude, ollama"
                )
            if provider in seen:
                raise ValueError(
                    f"provider_order contains duplicate provider: {provider}"
                )
            seen.add(provider)
            order.append(provider)
        return order


class CompilerSettings(BaseSettings):
    """Config for the Compiler agent (raw source → wiki article)."""
    # How much of the raw source reaches the model. The old hardcoded 8k cut
    # threw away ~85% of a typical Defuddle extraction; cloud-tier models
    # handle far more context, and depth is the whole point of the wiki.
    max_source_chars: int = 60000
    # Generated hero images via the yoyo CLI (`yoyo imagegen`). "auto" enables
    # generation when the yoyo binary is on PATH; "off" disables. Only fires
    # for articles that have no hero from their source, capped per day.
    hero_images: Literal["auto", "off"] = "auto"
    hero_images_daily_cap: int = 10
    hero_image_timeout_s: int = 180
    # Write-time pollution gate: a wiki-link target with no article is minted
    # as an Entity stub only after this many distinct articles reference it;
    # until then it sits in the Suggestions queue. 1 = legacy mint-on-first-
    # reference behavior.
    stub_gate_min_sources: int = 2


class ServerSettings(BaseSettings):
    """HTTP server trust-boundary config."""
    allowed_origins: list[str] = Field(default_factory=list)
    local_hostnames: list[str] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1", "::1", "[::1]", "testserver"]
    )


class RemindersSettings(BaseSettings):
    """Config for reminder defaults and reminder engine cadence."""
    default_lead_minutes: int = 15
    tick_seconds: int = 60
    late_threshold_minutes: int = 5
    max_attempts: int = 3
    retry_backoff_seconds: list[int] = Field(default_factory=lambda: [60, 300, 900])
    daily_summary_time: str = "07:30"


class NotifySettings(BaseSettings):
    """Push notification backend config."""
    backend: str = "none"
    pushover_token: str | None = None
    pushover_user: str | None = None
    ntfy_topic: str | None = None
    ntfy_server: str = "https://ntfy.sh"


class AttachmentsSettings(BaseSettings):
    """Config for vault attachment uploads."""
    max_mb: int = 25


class DomainsSettings(BaseSettings):
    """User-defined top-level life domains seeded from config.toml."""
    names: list[str] = Field(default_factory=list)


class DashboardSettings(BaseSettings):
    """Dashboard intelligence defaults for Phase 8 derived scans."""
    slipping_project_days: int = 14
    slipping_task_days: int = 7
    triage_reminder_days: int = 3


class CalendarSettings(BaseSettings):
    """Read-only Google Calendar cache config."""
    client_id: str = ""
    client_secret: str = ""
    calendar_ids: list[str] = Field(default_factory=list)
    sync_interval_minutes: int = 15


class RoundtableSettings(BaseSettings):
    """Config for the multi-LLM roundtable subsystem.
    See docs/superpowers/specs/2026-04-22-multi-llm-roundtable-design.md §7."""
    backends: list[str] = Field(default_factory=lambda: ["claude", "codex", "gemini", "ollama"])
    timeout_seconds: int = 120
    synthesis_model: str = "claude"
    # No per-backend model pins by default — each CLI uses its own current
    # default (which auto-updates as the CLI updates). Pinning here was the
    # source of the codex "gpt-5-codex not supported with ChatGPT account"
    # failure and locked gemini to 2.5-pro long after newer flashes shipped.
    # Override per-backend in config.toml under [roundtable.perspective_models]
    # if you want a specific model.
    perspective_models: dict[str, str] = Field(default_factory=dict)
    context_max_chars: int = 4000


class GithubSettings(BaseSettings):
    """Config for the GitHub Context Agent subsystem.
    See docs/superpowers/specs/2026-04-22-github-context-agent-design.md §7."""
    pat: str = ""
    poll_interval_minutes: int = 60
    ideate_tick_minutes: int = 60
    ideate_min_interval_hours: int = 24
    ideas_per_run: int = 4
    ideate_model: str = "claude-sonnet-4-6"


class BlogSettings(BaseSettings):
    """Config for the blog-writer subsystem.
    See docs/superpowers/specs/2026-04-22-blog-writer-design.md §12.

    No ``blog_model`` key: ``claude_bridge.run_claude()`` doesn't take a
    model arg — it uses whatever the user's ``claude -p`` default is. The
    Ollama fallback (and the theme-rerank pass) both use ``ollama_model``.

    ``min_relevance_score`` is the cutoff applied to LLM-reranked sources
    after the personal-evidence boost; entries below it are dropped before
    drafting so weak fits don't get cited. ``max_sources`` stays intentionally
    small because public essays need one sharp argument, not a ledger of every
    adjacent wiki item.
    """
    default_window_days: int = 14
    allowed_window_days: list[int] = Field(default_factory=lambda: [7, 14, 30, 90])
    max_sources: int = 10
    pre_rank_limit: int = 80
    per_source_char_limit: int = 1500
    min_per_source_chars: int = 300
    total_prompt_char_limit: int = 60000
    ollama_prompt_char_limit: int = 20000
    draft_word_count_min: int = 700
    draft_word_count_max: int = 1200
    claude_timeout_seconds: int = 240
    # When unset, falls back to top-level summarize_model_heavy.
    ollama_model: str | None = None
    min_relevance_score: float = 0.4

    # Draft-time public context. The wiki supplies the author's point of view;
    # live web search supplies reader-recognizable anchors and recent examples.
    # Search is best-effort and fail-open so blog generation still works
    # offline on the user's Mac.
    web_search_enabled: bool = True
    web_search_result_limit: int = 5
    web_search_fetch_limit: int = 3
    web_search_timeout_seconds: float = 8.0
    web_context_char_limit: int = 4500
    web_page_excerpt_char_limit: int = 900

    # Anti-repeat: candidates whose (kind, ref) was cited in any of the last
    # ``recent_post_lookback`` non-deleted blog posts have their rerank score
    # multiplied by ``recent_post_penalty``. lookback=0 disables the penalty.
    recent_post_lookback: int = 5
    recent_post_penalty: float = 0.4

    # ── topic_suggester ──
    # 48-hour windows over notes (user signal) and articles (world signal).
    # Caps bound the pairwise crossing cost: max_user * max_world pair scores
    # then top max_crossings flow into the LLM prompt.
    topic_suggester_max_user_items: int = 30
    topic_suggester_max_world_items: int = 60
    topic_suggester_max_crossings: int = 8

    # ── opinion_gap_miner ──
    # Weekly cadence; lower yield + smaller candidate set than topic_suggester
    # because the conflict signal is rarer than the crossing signal. The
    # caps trim before the pairwise scan — assertive notes are pre-filtered
    # in _pull_user_assertions.
    opinion_gap_miner_max_user_items: int = 20
    opinion_gap_miner_max_world_items: int = 40
    opinion_gap_miner_max_pairs: int = 6


class TweetSettings(BaseSettings):
    """Config for short-form thread suggestions.

    The local wiki supplies the point of view. Web/browser context supplies
    what is recent enough to make the thread timely.
    """
    default_window_days: int = 7
    allowed_window_days: list[int] = Field(default_factory=lambda: [1, 3, 7, 14, 30])
    max_local_sources: int = 8
    max_web_sources: int = 6
    web_search_enabled: bool = True
    web_search_timeout_seconds: float = 8.0
    web_page_excerpt_char_limit: int = 700
    x_browser_search_enabled: bool = True
    x_browser_search_limit: int = 9
    x_browser_search_per_action_limit: int = 3
    x_browser_search_max_actions: int = 5
    x_browser_search_plan_timeout_seconds: int = 60
    x_browser_search_timeout_seconds: float = 35.0
    grok_browser_search_enabled: bool = True
    grok_browser_timeout_seconds: float = 90.0
    grok_browser_excerpt_char_limit: int = 3500
    browser_context_timeout_seconds: float = 25.0
    prompt_char_limit: int = 40000
    per_local_source_char_limit: int = 1200
    claude_timeout_seconds: int = 180
    max_tweet_chars: int = 240
    max_hook_chars: int = 180


class Settings(BaseSettings):
    # populate_by_name: accept both the field name (from TOML) AND the alias
    # (from env vars). Without this, pydantic v2 silently drops TOML kwargs
    # for any Field with an alias — so e.g. ollama_local_only in config.toml
    # would be ignored and fall back to the default.
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", case_sensitive=False, populate_by_name=True,
    )

    # Server
    host: str = Field(default="0.0.0.0", alias="MASTISK_HOST")
    port: int = Field(default=5555, alias="MASTISK_PORT")
    server: ServerSettings = Field(default_factory=ServerSettings)

    # Claude
    claude_cmd: str = Field(default="claude", alias="CLAUDE_CMD")

    # Anthropic API (direct Messages API tier — see bridges/anthropic_bridge.py).
    # Secret: keep in config.toml or the environment, never in the vault.
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")

    # Ollama
    ollama_cloud_url: str = Field(default="https://ollama.com", alias="OLLAMA_CLOUD_URL")
    ollama_cloud_key: str | None = Field(default=None, alias="OLLAMA_CLOUD_KEY")
    ollama_local_url: str = Field(default="http://localhost:11434", alias="OLLAMA_LOCAL_URL")
    ollama_local_only: bool = Field(default=False, alias="OLLAMA_LOCAL_ONLY")

    # Models. Defaults chosen to work out of the box with common local + cloud-proxied Ollama stacks.
    # Override in ~/Library/Application Support/Mastisk/config.toml
    embed_model: str = "nomic-embed-text"        # `ollama pull nomic-embed-text`
    summarize_model_heavy: str = "kimi-k2.5:cloud"  # cloud-proxied via signed-in local ollama
    summarize_model_cheap: str = "qwen3.5:4b"    # local, fast

    # Agent budgets (daily caps — enforced by Agent.run_once)
    budget: AgentBudget = Field(default_factory=AgentBudget)

    notes: NotesSettings = Field(default_factory=NotesSettings)

    capture: CaptureSettings = Field(default_factory=CaptureSettings)

    intelligence: IntelligenceSettings = Field(default_factory=IntelligenceSettings)

    compiler: CompilerSettings = Field(default_factory=CompilerSettings)

    reminders: RemindersSettings = Field(default_factory=RemindersSettings)

    notify: NotifySettings = Field(default_factory=NotifySettings)

    attachments: AttachmentsSettings = Field(default_factory=AttachmentsSettings)

    domains: DomainsSettings = Field(default_factory=DomainsSettings)

    dashboard: DashboardSettings = Field(default_factory=DashboardSettings)

    calendar: CalendarSettings = Field(default_factory=CalendarSettings)

    roundtable: RoundtableSettings = Field(default_factory=RoundtableSettings)

    github: GithubSettings = Field(default_factory=GithubSettings)

    blog: BlogSettings = Field(default_factory=BlogSettings)

    tweet: TweetSettings = Field(default_factory=TweetSettings)

    # RSS feeds to subscribe (managed via CLI, stored in DB — this is just the initial seed)
    seed_feeds: list[str] = Field(default_factory=list)


def _load_toml_if_present() -> dict:
    p = config_path()
    if not p.exists():
        return {}
    with p.open("rb") as f:
        return tomllib.load(f)


def read_capture_bearer_token() -> str | None:
    """Read the capture bearer token directly from config.toml."""
    p = config_path()
    if not p.exists():
        return None
    with p.open("rb") as f:
        capture = tomllib.load(f).get("capture")
    if not isinstance(capture, dict):
        return None
    token = capture.get("bearer_token")
    return token if isinstance(token, str) else None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # Dev-mode: load .env from cwd if present
    load_dotenv()
    toml_data = _load_toml_if_present()
    # Pydantic reads env + defaults. Merge toml on top (toml wins over defaults, env wins over toml).
    return Settings(**toml_data)


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()


def update_toml_key(section: str, key: str, value: Any) -> None:
    """Surgically update one key in config.toml. Creates the file if missing.

    Only mutates the single (section, key) pair. Any other keys in the file
    are preserved verbatim. Caller is responsible for calling reload_settings()
    after this returns.
    """
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if p.exists():
        with p.open("rb") as f:
            data = tomllib.load(f)
    section_dict = data.setdefault(section, {})
    section_dict[key] = value
    # Atomic write via tempfile + rename
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=p.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            tomli_w.dump(data, f)
        os.replace(tmp, p)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
