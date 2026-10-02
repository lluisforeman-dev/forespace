# EigenGraph — Knowledge Construction Model

How a fact earns its place in this graph. Every rule here is implemented in
code — the file references are normative, not aspirational.

---

## 0. The one-sentence version

> A fact enters the graph as a *claim* tied to a source; its trust is a
> computed function of source tier, recency, independent corroboration and
> mechanical verification; conflicting claims are resolved without erasing the
> disagreement; larger facts are computed from smaller ones with an explicit
> derivation chain; and every fact decays with time unless re-verified.

---

## 1. Sources (layer 0)

| Signal | Where | Values |
|---|---|---|
| `Source.base_trust` | `Source` table, hand-set | regulator 95 · primary 80 · trade press 60 · social 28 |
| `domain_trust(url)` | `ingest/confidence.py` | ~30 curated domains: nasa.gov 92 · reuters 82 · techcrunch 65 · unknown 55; `.gov` +3 tiers |
| `Document.trust_override` | per-document | used by the research path to pin per-cited-URL trust |

Rules:
- Crawler honors **robots.txt** and paces ≥2s per domain (`crawl.py`).
- Raw bytes are archived on `Document.raw_content` — never overwritten. The
  graph is re-derivable from source.

## 2. Claim confidence (layer 1) — `ingest/confidence.py::score`

```
score = source_trust
      + 10  if extractor self-reports "high"          (−15 if "low")
      − recency_penalty(now − claim's as_of, volatility_window)   [0–30]
      + min(15, 5 × independent_corroboration_count)
      − 20  if source is an aggregator
      − 25  if method == imputed                      (clamp 0–100)
```

- **as_of, not scrape time**: a claim's recency anchor is the date the value
  was true in the world (`valid_range.lower`), so "100 employees in 2016"
  scores as 2016 evidence even if researched today.
- Acceptance: ≥50 accepted, <50 stays candidate (invisible in projections
  until promoted by adjudication or a human).

## 3. Corroboration (layer 2a) — `adjudicate.py`

When a second claim asserts the **same value** for the same (entity, attribute):

- **Same document** → the new claim is rejected as a duplicate. No credit.
- **Different document, independent** (`documents_independent()` in
  `confidence.py`): same document row, identical content hash, or same
  publication domain ⇒ NOT independent (reprints and syndication echo, they do
  not confirm). Otherwise independent:
  - `corroboration_count += 1`, document id appended to `corroborated_by`
  - confidence bumps asymptotically toward 99 (min +5 per hit)
- **Different document, not independent** → recorded, **zero trust gain**.

Independence v1 is domain-level; content-level story dedup (embeddings) is the
v2 upgrade and can only make trust stricter. Both counters are stored on the
assertion — "how many independent sources say this" is a queryable fact, not a
feeling.

## 4. Conflict & disagreement (layer 2b)

Different values for the same (entity, attribute), in order:

1. **Multi-cardinality attributes** (e.g. each funding round) hold independent
   values — every value is accepted; there is no conflict by construction.
2. **Volatile attribute, clearly newer evidence (>30d)** → old value superseded
   (`valid_range` closed at the new point in time), new accepted.
3. **Wild jump (>5× or ÷5)** → new value parked as `candidate` with
   `review_state=pending` **and a `Conflict` row** (severity high for
   immutable attributes) — order-of-magnitude jumps are usually unit errors or
   identity failures, never silently accepted.
4. **Otherwise → LLM synthesis**, which must:
   - supersede the conflicting claims **but keep them** (status `superseded`,
     fully queryable),
   - chain the new value to its evidential basis: `derived_from=[superseded ids]`,
   - write a `Conflict` row with `resolution='picked'`,
     `resolved_by='llm_synthesis'` and the reasoning — **resolution replaces,
     never erases**. A curator can always see that sources disagreed.

## 5. Mechanical verification (layer 2c) — `ingest/tasks/verify.py`

Quotes on the web-research path are **verified against the fetched source**:

| Outcome | `quote_verified` | Effect |
|---|---|---|
| Quote found verbatim (raw HTML or extracted prose) | `true` | +5 confidence |
| Quote not found in source | `false` | −20 confidence; drops out of `accepted` if <50 |
| Quote < 20 chars (mention-fallback) | `false` | treated as unverified, no fetch wasted |
| Source unfetchable (404 / paywall / robots / timeout) | `null` | **no penalty** — unreachable ≠ false |

Fetched sources are archived on `Document.raw_content` and never refetched.
The RSS pipeline applies the same rule *before* storage (a claim whose quote is
not a verbatim substring of the document is rejected outright).

## 6. Derived facts (layer 3) — `ingest/tasks/derive.py`

Larger facts are computed from smaller sourced facts, never scraped:

```
total_funding_usd  =  Σ funding events (deduplicated: identical rounds by
                      amount+year; near-duplicates — same year, amounts within
                      15%, regardless of event typing — keep the
                      higher-confidence report)
derived_from       =  [event ids]          ← explicit, auditable chain
confidence         =  min(contributing events' confidence)   ← a chain is never
                                                             stronger than its
                                                             weakest link
```

- Only capital-*raising* entity types (company, university, end_user) get a
  derived total — arithmetic on an asset or investor entity with mis-attached
  events is exactly the nonsense derivation exists to surface, so it is
  type-guarded off, and stale derived totals are always fully recomputed.

- Derived assertions use `method='derived'` and are **excluded from
  corroboration and adjudication** — a computation is never evidence (§9.2).
- A corrected input (a round's amount fixed) invalidates the output on the
  next derivation run.
- Extraction-derived totals for the same attribute are superseded by the
  derived value: traceable arithmetic beats a single-source guess.

## 7. Truth decay (layer 4) — `confidence.py::display_trust`, `refresh_stale_facts`

- Every attribute carries `volatility_days` (immutable ones effectively 10y).
- Read-time trust = `confidence − 0.05 per volatility-period past the window`,
  floored at 0.1 — anchored to the claim's world date, not its discovery date.
- A daily cron (`refresh_stale_facts`) re-researches the stalest volatile
  facts, so decay is a *prompt to re-verify*, not a slow rot.

## 8. Entity identity — how mentions become the right node

Resolution ladder (`ingest/tasks/resolve.py`) — cheapest signal first:

1. **Geographic routing** — countries/cities are relation targets, never companies.
2. **Exact canonical name** — an ESTABLISHED entity always outranks a data-less
   stub sharing the name (first-come stub ownership is how false splits are born).
3. **Exact alias** — an alias owned by an established entity routes immediately;
   an alias owned only by a stub is a weak hint, not a verdict.
4. **Candidate adjudication** — ONE batched LLM call over all candidates from
   trigram similarity **plus word-boundary containment** ("Spire" must see
   "Spire Global" even when higher-similarity names crowd it out). Uncertain →
   `match: null`.
5. **World-knowledge canonical lookup** — acronyms and language variants.
6. **Stub creation** — last resort, created as `status='stub'`: a hypothesis,
   not a company.

**Lifecycle**: stub → (≥3 accepted assertions via adjudication) → active.
Empty stubs never masquerade as companies in the graph's company views.

**Alias ownership is arbitration, not bookkeeping**: when a name resolves to an
established entity but a stub owns the alias, the alias is rebound; when two
established entities claim one alias, the pair is queued for context-aware dedup.

**Reopenable verdicts** (`EntityNonMerge.assertions_a/b`): a "different"
decision made when either side was data-less is a guess. Dedup re-litigates a
decided pair when either side's evidence has grown past the decision-time base
(`_should_reopen`) — wrong early verdicts heal themselves as research accrues.

**Fragment healing** (`heal_entity_fragments`): entities with zero evidence
whose name is word-contained in an established entity are merged
deterministically — no LLM, nothing to misattribute; the fragment's name becomes
an alias of the container. Genuinely distinct subsidiaries (which have
evidence) are untouched.

## 9. What this model deliberately does NOT do (yet)

- **Content-level story dedup** — cross-domain syndication still counts as
  independent (v1 rule above); embedding-based near-duplicate detection is the
  next tightening.
- **Gold-set evaluation** — the scoring formula is implemented but its
  precision/recall is not yet measured against a hand-verified set.
- **Full quote-verification backfill** — new claims are verified
  automatically; the historical corpus backfills via `verify_quotes`.
