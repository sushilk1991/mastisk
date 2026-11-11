# Mastisk domain language

The corpus compounds: every article links to others through wiki-link spans, and the link graph drives clustering, ranking and synthesis.

| Term | Meaning |
|------|---------|
| **Note** | Raw user capture (a sentence or paragraph). Has `classification`, `confidence`, `escalation_state`. Stored at `vault/_notes/YYYY-MM-DD/HHMMSS-slug.md`. |
| **Article** | Structured wiki page of kind `Concept`, `Entity`, `Source` or `Synthesis`, with sections, related links, sources, media and a confidence. |
| **Stub article** | Placeholder with `confidence=0`: `escalator (stub)` (a note awaiting enrichment) or `Compiler (stub)` (created from an unknown wiki-link target). |
| **Escalation** | Note-to-article promotion. States: `none → pending → retrying → auto_done | manual_done | failed`, plus `skipped_cap` and `skipped_dup`. |
| **Notetaker** | Classifies new notes, assigns confidence, links related articles (30 s tick). |
| **Escalator** | Promotes worthy notes into stub articles and enqueues `compiler/enrich_stub` jobs (60 s tick). |
| **Compiler** | Turns a raw source or stub into a structured article; sets `updated_by='Compiler'` (5 min tick). Capture to article takes about 5 to 15 minutes end to end. |
| **Synthesizer** | Picks a cluster of related articles with Personalized PageRank (HippoRAG2-style) and writes a `Synthesis` article (30 min tick). |
| **Roundtable** | Multi-LLM debate over an `input_ref`, usually an article; produces N perspectives and a synthesis. |
| **Scout** | Pulls sources from RSS and web feeds. |
| **Listener** | Transcribes queued YouTube videos and podcasts into sources. |
| **Topic Suggester, Blog Writer, Artifact Agent, Vault Integrity** | Other specialist agents in the daemon. |
| **Signals** | User behaviour events: `opened`, `time_read`, `pinned`, `unpinned`, `deleted`, `edited`, `asked`, `skipped`, `liked`, `disliked`. |
| **Digest** | Today's curated reading list, ranked by signals and recency. |
| **Feed** | Activity stream of agent actions. |
| **Open question** | An analytical loose end inside an article (a section with `kind='open'`). |
| **Wiki link** | `<span class="link" data-target="slug">label</span>` inside article HTML bodies. |
| **Confidence** | 0.0 to 1.0. Stubs are 0; Compiler-written articles are usually 0.5 to 0.8. |
| **`_self` / identity** | `vault/_self/{identity,interests,dislikes,style,learnings}.md`, loaded by every generative agent. Read them first before writing in the user's voice. |

## Where data lives

- HTTP API: `http://127.0.0.1:5555/api/*`.
- Vault: `~/Library/Mobile Documents/com~apple~CloudDocs/Mastisk/vault/` (`_self/`, `_notes/YYYY-MM-DD/`, `concepts/`, `entities/`, `sources/`, `synthesis/`, `blog/`).
- SQLite: `~/Library/Application Support/Mastisk/mastisk.db`.
- Logs: `~/Library/Application Support/Mastisk/logs/mastisk.log`.
