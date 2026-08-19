---
name: mastisk
description: Personal memory on this machine. Use before work where prior decisions, incidents, preferences, research, or learnings could change the approach; when the user asks what they know or remember; and after verified work produces a durable learning. Recall with `mastisk ask`, write only through `mastisk ingest`.
---

# Mastisk memory

Use the installed `mastisk` CLI. It picks the retrieval, graph traversal,
synthesis, and freshness checks. Do not assemble a manual search pipeline.

## Recall relevant knowledge

Give Mastisk the complete question or task context:

```bash
mastisk ask "Before I change this retry flow, what prior decisions, failures, and current constraints matter?" --json
```

For multi-line context up to 8,000 characters, use stdin:

```bash
printf '%s' "$TASK_CONTEXT" | mastisk ask - --json
```

- Recall before substantial work when prior knowledge could change the approach
  or prevent a repeat mistake.
- Include the repository, component, goal, symptoms, versions, and the decision
  at hand.
- Cite the answer's Mastisk source IDs as evidence. Never promote a hypothesis
  or low-confidence note into fact.
- `mastisk ask` decides which corpus queries and live checks run. Do not fall
  back to SQLite or vault reads for ordinary recall.

## Judge freshness

Use the system date at runtime. The date on this file is not today.

- Age is evidence, not a relevance score. Old incident learnings, principles,
  and personal decisions stay valuable.
- Claims about AI models, APIs, libraries, product behavior, security, laws,
  pricing, active plans, and vendor capabilities go stale fast.
- When an old claim conflicts with newer direct evidence, prefer the new one.
  Say what it superseded and the as-of date.
- If Mastisk reports current research unavailable, verify against the freshest
  primary source or label the answer unverified.

## Preserve durable learnings

After verified work, decide whether the result would help a future agent. If so,
ingest a compact learning:

```bash
mastisk ingest "Learning (mastisk ingest, as of $(date +%F)): API writes must pass through HTTP ingestion routes; direct database writes bypass pipeline invariants. Evidence: focused CLI/API tests. Recheck if the ingestion contract changes." --json
```

Use the real current date. Include:

- The decision, failure mode, or reusable fact.
- Its scope and the system/version it applies to.
- The evidence that made it trustworthy.
- What would make it stale or need a recheck.

Ingest the source or learning. Never write a database row, vault file, or
finished article directly. Mastisk classifies, deduplicates, links, compiles,
and synthesizes it through the pipeline.

Never ingest routine progress, unverified guesses, hidden chain-of-thought,
credentials, tokens, private keys, or material the user did not authorize this
system to retain.

## Ingest any supported source

```bash
mastisk ingest "A durable text learning" --json
mastisk ingest ./report.pdf --json
mastisk ingest https://example.com/source --json
some-command | mastisk ingest - --json
```

URL ingestion keeps the submitted URL. Pass the direct feed or media URL when
that is the source, not a page advertising it.

A returned note ID means captured. A returned job ID with `queued` status means
accepted into the async pipeline, not that compilation finished.

## Handle availability failures

If the CLI reports the daemon unavailable, tell the user. Do not restart
`com.mastisk.agents` or reinstall it unless the user explicitly approves that
interruption.
