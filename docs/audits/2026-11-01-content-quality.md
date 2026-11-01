# Mastisk content-quality audit

As of 2026-11-01.

## Verdict

Source-backed articles often preserve useful detail and uncertainty. The automatic note-to-article pipeline is less reliable: it repeatedly expands research suggestions into long articles, adds unsupported facts, and feeds those articles into syntheses. The critic records problems without blocking publication. Digest ranking then favors syntheses over original sources.

The calendar defect is separate. It hid dates that had never been requested; October had eligible articles on every day.

## Scope and limits

- Structural checks covered 19,338 article rows and 1,951 active notes. October creation dates account for 2,675 articles and 373 notes; 2,652 of those articles have bodies.
- Full-text review covered 23 articles and 20 notes. Selection included the first, middle, and last October article of each kind, recent November output, duplicate-topic pairs, and one extraction failure. This is a diagnostic sample, not a random sample or an estimate of the corpus-wide factual error rate.
- Four attached primary sources were fetched now. One referenced local commit and Yoyo's round-three experiment instructions were also checked. Other external claims in the sample remain unverified.
- Production API reads verified all 23 sampled articles and 20 sampled notes. Database inspection used SQLite `mode=ro`; no corpus content was edited, deleted, promoted, or regenerated. The earlier calendar-diagnosis memory capture is excluded from the content sample and October counts.
- Month membership uses the stored creation-date prefix, not update dates. Article timestamps and note timestamps have different timezone conventions; boundary counts are not normalized to one timezone. Counts describe a snapshot, not permanent totals.

The inspected Compiler, Synthesizer, Scout, Escalator, GithubIdeator, and digest-ranker files match the installed package byte-for-byte. The corrected calendar route does not yet match the installed package.

## 1. Prevent unsupported facts during note promotion

**Evidence.** [Note 1952](http://127.0.0.1:5555/api/notes/1952) proposes investigating signed canvas state. Its article, [`note-001952-signed-canvas-state-for-cross-surface-ve`](http://127.0.0.1:5555/a/note-001952-signed-canvas-state-for-cross-surface-ve), adds:

> collaborative cursor positions leaking into exported frames

> A dense canvas history with 10,000 operations compresses to roughly 2-3MB of Merkle path data using SHA-256 and 64-operation fan-in.

Neither detail appears in the source note. The referenced commit, `bb8519dc687fde33d839e51924feb4f314e5303f` in `masonry-ai-frontend`, documents public/private download URL selection, blocked popup fallback, zoom floors, refetch-driven view resets, and download analytics. It does not document cursor leakage, Merkle trees, or storage measurements. The article also asserts typical 50–200MB exports without a cited measurement.

The initial note already overinterprets a shared-viewer fix as a collaborative-editing/export integrity problem. Enrichment adds more specificity without new evidence. [`agent-surfaces-mask-what-they-certify`](http://127.0.0.1:5555/a/agent-surfaces-mask-what-they-certify) and [`harness-economics-when-verification-becomes-the-rate-limiter`](http://127.0.0.1:5555/a/harness-economics-when-verification-becomes-the-rate-limiter) then reuse the signed-canvas article as supporting material.

**Mechanism.** `Compiler._handle_enrich_stub` supplies the note body, identity, and article registry to the model. It persists with `source_id=None`; that path does not retrieve the referenced commit. The shared schema asks for 700–1,400 words for substantive sources and defines confidence as the model's subjective calibration. In October, 303 Concept articles have a note back-reference and no attached external source. That is legitimate lineage for a proposal, but insufficient evidence for new factual claims.

**Recommended change.** Preserve machine-readable origin (`human_capture`, `repo_ideator`, `verified_learning`) and separate observations, proposals, inferences, and verified facts. Resolve referenced commits/documents before enrichment. Require factual numbers and incident details to cite a source passage; label unmeasured design estimates. Allow a short note to remain short. Do not use an unverified generated proposal as independent corroboration in another article.

**Acceptance check.** Replaying note 1952 must not produce cursor-leakage claims or measured-looking storage figures without supporting evidence. Its derived synthesis must retain the proposal's uncertainty.

## 2. Make the critic affect publication

**Evidence.** October has 434 synthesis-run records: 432 produced persisted drafts, all unreviewed. Of those drafts, 17 scored below 3/5, including one at 1/5. The two runs without critic scores have no draft article; they are not examples of uncritiqued publication in this month.

[`valid-outputs-outrun-evidence-trails`](http://127.0.0.1:5555/a/valid-outputs-outrun-evidence-trails) scored 1/5. Its recorded critic flags unsupported figures including “8%,” “300 re-judged,” “150 human re-checks,” and “30,000 lines.” Those are the critic's findings, not an independently measured error count. Other low-scoring runs flag forced connections and sequential summaries presented as synthesis.

`Synthesizer._draft_critic_loop` explicitly returns low-scoring drafts unchanged. `_synthesize_once` writes the article before any human acceptance. The digest ranker does not consult critic results or acceptance state. Thus the UI's Keep/Discard affordance is not a publication gate for the wiki or digest.

**Recommended change.** Keep low-scoring, critic-failed, and materially unsupported drafts in a separate review state. A provisional policy could require at least 4/5 plus claim-source checks before automatic publication. Permit one bounded revision, then abstain. A model score alone must not certify truth. Exclude rejected and quarantined drafts from normal retrieval and digests.

**Acceptance check.** Fixtures representing the 1/5 and 2/5 cases stay out of reading lists and ordinary answers until corrected or explicitly reviewed. Record the reason for withholding them.

## 3. Stop generating the same research note repeatedly

**Evidence.** Of October's 373 notes, 360 (96.5%) map directly to `repo_idea_runs.note_ids_json`; 13 arrived through the CLI. Of the 360 ideator notes, 303 reached `auto_done`. These are mostly system-generated research suggestions, not 360 new user observations.

[Notes 1910](http://127.0.0.1:5555/api/notes/1910) and [1934](http://127.0.0.1:5555/api/notes/1934) both ask whether cross-vendor agreement predicts correctness, using the same README claim and judge-bias concerns. Both became articles:

- `note-001910-does-cross-vendor-agreement-predict-corr`
- `note-001934-cross-vendor-agreement-as-a-correctness-`

[Notes 1911](http://127.0.0.1:5555/api/notes/1911) and [1936](http://127.0.0.1:5555/api/notes/1936) repeat the holdout-decay topic. The earlier article records that Yoyo retired earlier holdouts; the later article proposes a generic comparison without preserving that qualification. Yoyo's `extras/autoresearch/round3/program.md` confirms the earlier article's separation of development, once-only holdout evaluation, and smoke evidence.

October contains 19 normalized identical-title groups involving 44 nonempty articles. Across the corpus, 85 such groups involve 209 articles. Title collisions are screening signals, not proof every pair should merge. No exact nonempty body duplicates or exact October note-body duplicates were found after whitespace normalization.

**Mechanism.** GithubIdeator runs on a cadence and receives rolling repo context, but not prior idea history. Its prompt requests a fixed number of ideas and only requires distinctness within that response. Escalator dedup compares summary word overlap within a configurable time window; the checked default is 24 hours. It is not a durable topic registry.

**Recommended change.** Generate from meaningful repo changes, allow zero ideas, and show the generator prior ideas and their outcomes. Match candidates against canonical topics over a longer history before promotion. A repeated topic should update its evidence or remain suppressed unless it contributes a specific new fact, contradiction, or experiment result. Preserve provenance when proposing merges; do not delete duplicates automatically.

**Acceptance check.** The two note pairs above are detected before a second article is created. Unchanged repo context produces no new notes.

## 4. Make the digest expose source evidence

**Evidence.** All 15 saved October selections, across October 8, 18, and 19, are Synthesis articles. No digest-specific feedback signals exist for October. This is a small observed selection set, not a claim about every day's potential digest.

The ranker's kind multiplier is 2.0 for Synthesis and 0.4 for Source: a fivefold advantage before source counts, graph degree, and personal interest. It ignores critic verdicts. October also has 406 of 428 new syntheses with model-reported confidence at least 0.9; those numbers have not been calibrated against factual correctness.

A missing direct source attachment is not itself a synthesis defect. Lineage is recorded through `links` and `synthesis_runs.source_article_ids`; 426 October syntheses lack direct external attachments by design. Follow the lineage rather than treating them all as unsourced. Twenty October runs have no cluster member with a direct external attachment, which warrants a provenance check but does not prove they lack all upstream evidence.

**Recommended change.** Rank evidence quality and new information before kind. For a five-item digest, test a source-first mix with no more than one or two qualified syntheses when enough source-backed material exists. Show the originating sources and critic/review state. Track “useful new information” and repeated-topic dismissals; do not equate reading a prominently placed item with preference for that topic.

**Acceptance check.** A strong source article can outrank a weak synthesis. A 1/5 synthesis cannot appear. Test a mixed five-item digest against the current ranking with user feedback before treating the suggested mix as a permanent rule.

## 5. Detect incomplete extraction before writing a full article

**Evidence.** [`llm-tools-datasette-0-1-1`](http://127.0.0.1:5555/a/llm-tools-datasette-0-1-1) has confidence 0.98 and a 1,855-character body, enough to pass the digest length filter. It correctly says its supplied excerpt lacks release details, then pads the page with surrounding posts and generic relevance.

Its saved raw input contains a title, date, and “Recent articles” navigation. The [primary post](https://simonwillison.net/2026/Oct/18/llm-tools-datasette/), fetched during this audit, states that LLM's migration to `httpx2` broke plugins implicitly depending on `httpx`, and 0.1.1 fixes this plugin's dependency. The relevant release explanation is absent from the saved Compiler input. Re-running Scout's extraction recipe against the current page reproduced the failure: 341 characters of date/navigation, with no `httpx2` explanation.

`Scout._fetch_feed` accepts any nonempty extraction, even one that consists of navigation. URL dedup then skips an already-seen URL. Retrying extraction needs an explicit path rather than waiting for another ordinary feed poll.

**Recommended change.** Detect navigation-only and low-information extraction, retry with a second extractor or feed body, and mark unresolved cases `needs_fetch`. Store extraction method, content hash, and quality flags. Publish a short release record when the source is short; do not use a body-length threshold as a factual-quality test.

**Acceptance check.** A fixture for this post retains the `httpx2`/`httpx` dependency explanation and excludes navigation. Failed extraction is visible as incomplete work, not a confident full article.

## Additional quality observations

- All 1,841 October Source articles have an attached source. Source-count caches match actual `article_sources` rows across the corpus. All active notes were classified and have summaries. Every completed note escalation points to an article with a body. These are structural successes, not proof of factual accuracy.
- The primary-source checks for [`bootgpt-boot-sector-language-model`](https://github.com/kerlenton/bootgpt), [`ai-cost-of-javascript-frameworks`](https://nolanlawson.com/2026/10/28/the-ai-cost-of-javascript-frameworks/), and [`otape`](https://www.otape.org/) support their core technical descriptions. The framework article preserves the experiment's variance and limits. Some broader commentary remains interpretation.
- There are 4,746 empty `Compiler (stub)` Entity rows, 4,720 created before October. They are placeholders, not thousands of newly failed October generations. Distinguish placeholders from readable pages in counts and retrieval. Do not bulk-enrich them without an evidence and usefulness threshold.
- Self-referential link syntax appears in 184 October articles; this count needs semantic review, not blanket deletion. One confirmed defect is `note-001936-holdout-decay-across-yoyo-autoresearch-r`: a link labelled “Cross-vendor agreement as a correctness signal” targets that same holdout article. Add self-link and label/target checks. The stored markdown also concatenates list items and paragraphs in several samples; visual impact was not browser-verified.
- October job rows contain two failures: one source compilation and one note enrichment. The failed note now has a completed article. There were no queued or running jobs at inspection. Successful job status therefore does not measure content correctness or whether low-quality output reached readers.

## Writing quality

The sample frequently redirects unrelated subjects into the same verification/harness theme. Useful connections survive; obligatory connections make the corpus repetitive. Use fewer links and require each connection to add a specific mechanism or disagreement.

Examples:

- “These are not merely UX bugs. They are verification collapses” uses a binary contrast to overstate an unverified interpretation. Fix: describe the observed download and view-state defects.
- “The most consequential design question is key distribution” tells the reader what to value without supporting the ranking. Fix: state who would sign, what would be signed, and the unresolved trust assumption.
- “The gap between reported and actual behavior is a known canyon” in `evaluation-as-active-evidence-acquisition` substitutes metaphor for a precise limitation. Fix: name the self-report limitation and its evidence.

These are observed prose patterns, not an inference about who wrote a passage or a numerical writing-quality score.

## Calendar change and verification

Changed only the calendar route's eligibility query and its tests. The endpoint now returns article-update dates with bodies meeting `digest_ranker.MIN_BODY_CHARS`, even before a digest is requested. Opening the day still uses the existing on-demand ranker. Calendar reads do not create candidate rows.

Verification:

```text
Before fix: pytest tests/test_dashboard_intelligence.py -k calendar -q
4 failed, 5 passed

After fix: pytest tests/test_dashboard_intelligence.py tests/test_digest_ranker.py -q
46 passed

Corrected route + read-only production DB via isolated FastAPI TestClient:
HTTP 200, all 31 October dates

ruff check src/mastisk/routes/digest_route.py
All checks passed
```

Four import-order lint findings remain in untouched blocks of `tests/test_dashboard_intelligence.py`; running Ruff against `git show HEAD:tests/test_dashboard_intelligence.py` confirms the same four baseline findings. No frontend code changed. The new route has not been installed, restarted, or checked in the live calendar UI.

This change does not freeze historical digests. Article edits and current user signals can still change a past day's result. Immutable dated snapshots would be a separate archive-design change.

## Suggested implementation order

Engineering estimates, assuming focused regression tests and no data migration surprises:

| Order | Work | Estimate |
|---|---|---|
| 1 | Critic quarantine and retrieval/digest exclusions | Half a day |
| 2 | Origin/evidence fields and evidence-preserving note promotion | 2–3 days |
| 3 | Delta-driven ideation and durable topic dedup | 1–2 days |
| 4 | Mixed, evidence-aware digest ranking and feedback trial | 1 day |
| 5 | Extraction-quality checks and bounded retry | Half a day |

Do not regenerate or merge the corpus first. Prevent new bad output, then review the identified low-scoring drafts and duplicate pairs through supported APIs.

## Recheck queries

Use a read-only connection to `~/Library/Application Support/Mastisk/mastisk.db`. These queries use the same stored-date boundaries as the audit.

```sql
-- Automatic-origin notes: distinguish generated suggestions from human captures.
SELECT COUNT(DISTINCT n.id)
FROM notes n
JOIN repo_idea_runs r
JOIN json_each(r.note_ids_json) j ON CAST(j.value AS INTEGER) = n.id
WHERE n.deleted_at IS NULL
  AND n.created_at >= '2026-10-01' AND n.created_at < '2026-11-01';

-- Published drafts with low critic scores.
SELECT r.draft_article_id, r.eval_score, r.user_accepted
FROM synthesis_runs r JOIN articles a ON a.id = r.draft_article_id
WHERE r.created_at >= '2026-10-01' AND r.created_at < '2026-11-01'
  AND r.eval_score < 3;

-- Actual saved digest mix, not a newly recomputed historical ranking.
SELECT a.kind, COUNT(*)
FROM digest_candidates dc JOIN articles a ON a.id = dc.article_id
WHERE dc.selected = 1 AND dc.digest_date BETWEEN '2026-10-01' AND '2026-10-31'
GROUP BY a.kind;

-- Identical titles identify candidates for review, not automatic merges.
SELECT LOWER(TRIM(title)), COUNT(*), GROUP_CONCAT(id, ' | ')
FROM articles
WHERE body_md != '' AND created_at >= '2026-10-01' AND created_at < '2026-11-01'
GROUP BY LOWER(TRIM(title)) HAVING COUNT(*) > 1;

-- Readable-page counts should not silently include empty placeholders.
SELECT kind, COUNT(*), SUM(body_md = '')
FROM articles GROUP BY kind;
```

## Full-text sample manifest

Article IDs, grouped by selection reason:

```text
October source-backed articles:
56k-rip-1996-dial-up-internet
ebay-restricts-radio-equipment-listings
bootgpt-boot-sector-language-model
agent-worms-through-shared-channels
overlapping-crises-and-distributed-resilience
google-ai-mode-search-revenue-tension

October syntheses and entities:
verified-completion-is-the-price-of-cheap-agents
bounded-decisions-need-runtime-contracts
proof-claims-vs-verifiable-behavior
aweb-communication-for-ai-agents
decision-native-ai
evaluation-as-active-evidence-acquisition

Recent November output:
ai-cost-of-javascript-frameworks
otape
note-001950-skills-as-templates-and-the-transfer-pro
note-001952-signed-canvas-state-for-cross-surface-ve
agent-surfaces-mask-what-they-certify
harness-economics-when-verification-becomes-the-rate-limiter

Duplicate-topic and extraction probes:
note-001910-does-cross-vendor-agreement-predict-corr
note-001934-cross-vendor-agreement-as-a-correctness-
note-001936-holdout-decay-across-yoyo-autoresearch-r
note-001911-holdout-decay-across-yoyo-autoresearch-r
llm-tools-datasette-0-1-1
```

Note IDs:

```text
1568 1765 1940 1569 1727 1934 1570 1763 1914 1679
1702 1784 1862 1864 1868 1910 1911 1936 1952 1950
```

Next action: approve a drained deployment/restart of only the calendar fix. The unrelated working-tree changes must not be deployed with it.
