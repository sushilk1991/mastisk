---
name: mastisk
description: Use Mastisk as intelligent personal memory on this machine. Use before work where prior decisions, incidents, preferences, research, or learnings could materially improve the result; when the user asks what they know or remember; and after verified work produces a durable learning worth preserving for future agents. Recall with `mastisk ask` and write only through `mastisk ingest`.
---

# Mastisk memory

Use the installed `mastisk` CLI. Let Mastisk's intelligence choose retrieval,
graph traversal, synthesis, and freshness checks. Do not assemble a manual
search pipeline.

## Recall relevant knowledge

Give Mastisk the complete question or task context:

```bash
mastisk ask "Before I change this retry flow, what prior decisions, failures, and current constraints matter?" --json
```

For multi-line task context up to 8,000 characters, use stdin:

```bash
printf '%s' "$TASK_CONTEXT" | mastisk ask - --json
```

- Recall before substantial work when previous Mastisk knowledge could change
  the approach or prevent repeating a mistake.
- Include the current repository, component, goal, symptoms, versions, and
  decision being made when relevant.
- Use the synthesized answer and its Mastisk source IDs as evidence. Do not
  silently convert a hypothesis or low-confidence note into fact.
- Let `mastisk ask` decide which corpus queries and live checks are needed.
  Do not fall back to direct SQLite or vault reads for ordinary recall.

## Judge freshness intelligently

Use the current system date at execution time; never treat the date on this
skill file as today's date.

- Treat age as evidence, not an automatic relevance score. Old incident
  learnings, principles, and personal decisions may remain valuable.
- Treat claims about AI models, APIs, libraries, product behavior, security,
  laws, pricing, active plans, and vendor capabilities as time-sensitive.
- Prefer newer direct evidence when an old claim conflicts with a newer one.
  State what was superseded and the relevant as-of date.
- If Mastisk reports that current research was unavailable, verify through the
  freshest primary source available or clearly label the answer as unverified.

## Preserve durable learnings

After verified work, decide whether the result could materially help a future
agent. If so, proactively ingest a compact learning:

```bash
mastisk ingest "Learning (mastisk ingest, as of $(date +%F)): API writes must pass through HTTP ingestion routes; direct database writes bypass pipeline invariants. Evidence: focused CLI/API tests. Recheck if the ingestion contract changes." --json
```

Use the actual current date. Include:

- The decision, failure mode, or reusable fact.
- Its scope and the system/version it applies to.
- The evidence that made it trustworthy.
- Conditions that would make it stale or require rechecking.

Ingest the source or learning; do not write a database row, vault file, or
finished article directly. Mastisk will classify, deduplicate, link, compile,
and synthesize it through the normal pipeline.

Do not ingest routine progress, unverified guesses, hidden chain-of-thought,
credentials, tokens, private keys, or material the user did not authorize this
system to retain.

## Ingest any supported source

```bash
mastisk ingest "A durable text learning" --json
mastisk ingest ./report.pdf --json
mastisk ingest https://example.com/source --json
some-command | mastisk ingest - --json
```

URL ingestion preserves the submitted URL; provide a direct feed or media URL
when that is the intended source rather than a page that merely advertises it.

Treat a returned note ID as durably captured. Treat a returned job ID with
`queued` status as accepted into the asynchronous pipeline, not as proof that
compilation has finished.

## Handle availability failures

If the CLI reports that the Mastisk daemon is unavailable, tell the user. Do
not restart `com.mastisk.agents` or reinstall the daemon unless the user
explicitly approves that interruption.
