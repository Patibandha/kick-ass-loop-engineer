# Research reference — the provenance gate

No build proceeds on unverified facts. `research` is the **first** stage in the `next` loop
(ahead of `plan`). You run a sourced sweep, write `.loop-engineer/RESEARCH.md`, and the
engine gates it **deterministically, like a test**. A substantive failure that survives the
retry budget is `RESEARCH_BLOCKED` — a terminal, honest dead-end, not a warning.

## The RESEARCH.md contract
`kickass_loop_engineer.research.parse_research` reads bullet claims; a claim line is
`[-*+] [TAG] text` with an optional ` (source: URL "quote")` clause. Tags are UPPERCASE,
exactly one of `VERIFIED` / `CITED` / `ASSUMED`. The `##` heading a claim sits under decides
whether it is **load-bearing** (`## Decisions`) or **speculative** (`## Notes`) — heading
match is case- and suffix-tolerant (`## Decisions (load-bearing)` still counts).

```markdown
# RESEARCH

## Questions
- What TUI framework fits a half-block pixel renderer?

## Decisions
- [VERIFIED] Use Textual >= 0.60 for the TUI (source: https://textual.textualize.io "Rapid Application Development framework for Python")
- [CITED] Piper runs on CPU with no GPU (source: https://github.com/rhasspy/piper "does not require a GPU")

## Notes
- [ASSUMED] The user prefers a dark theme by default

## Assumptions
- Local Ollama is reachable on :11434

## Validity
- valid_until: 2026-08-06
```

**Load-bearing = the `## Decisions` section.** Every stack / API / version choice goes there,
each `[VERIFIED]` or `[CITED]` with a source. Speculation goes under `## Notes` — never
load-bearing. This makes "load-bearing" deterministic without any NLP.

## Shape gate (runs before the two-part gate)
`research_v1` (`validate_artifact`) is a cheap shape check the `next` machine runs first: the
doc must have a `## Decisions` heading **and at least one tagged `[VERIFIED|CITED|ASSUMED]`
bullet somewhere**. A literally content-free RESEARCH.md fails and re-dispatches — so a
no-external-research run still writes one self-evident tagged bullet (put an `[ASSUMED]` under
`## Notes`, `## Decisions` can be `(none)`). Shape only proves the doc's form; the two-part
gate below proves its provenance.

## The two-part gate (both pure, both engine-side)
1. **Tag coverage** — `check_tag_coverage`: every `- ` bullet under `## Decisions`/`## Notes`
   must be a valid tagged claim (an untagged or unknown-tag bullet FAILS), and **any
   `[ASSUMED]` under `## Decisions` FAILS** (a load-bearing guess). A `## Decisions` with no
   claims (or only self-evident sourced `[VERIFIED]` ones) passes trivially — `[ASSUMED]`
   under `## Notes` is fine. Runs entirely inside the engine.
2. **Citation spot-check** — `select_citations` picks up to five `VERIFIED`/`CITED` claims
   that carry both a url and a quote (document order). The session re-fetches them in a
   `verify_citations` round; `check_citations` then compares each **normalized claimed quote
   ⊂ normalized fetched text**. Normalization casefolds, collapses whitespace, and folds
   smart punctuation — but a genuinely absent quote still mismatches. `incomplete` = a
   selected url has no fetched section yet (re-fetch); `mismatch` = the page does not contain
   the quote (a fabricated/stale citation).

## The byte-verbatim rule
When you satisfy a `verify_citations` round, paste the quote **exactly as it appears on the
fetched page** into `.loop-engineer/citations.md` under a `## <url>` heading. Do not
straighten curly quotes or dashes, do not paraphrase. If the quote is not on the page, do
**not** invent text — let the check `mismatch`. `RESEARCH_BLOCKED` is the correct outcome;
fabricating a citation to pass the gate is the one thing this stage exists to catch.

## Source hierarchy (binding)
Context7 / version-pinned docs → official docs + changelogs → verified web → training data
**only as `[ASSUMED]`, never for a library / API / version choice**. A negative claim
("X is impossible") must cite official docs; absence of memory is not evidence.

## RESEARCH_BLOCKED semantics
A terminal state (`TerminalState.RESEARCH_BLOCKED`). Substantive failures — a load-bearing
`[ASSUMED]`, or a citation `mismatch` — that outlive the retry budget end the run here,
carrying `reason` + `attempted`. The fix is better sourcing, never re-running to slip past.

## Memory reuse (don't re-research settled facts)
On the gate passing, `research-ingest` records the `## Decisions` claims to memory as
`kind="research"` (status `working`); a run's SUCCESS consolidates them to `canonical`.
`ResearchBrief.from_spec` reuses canonical research so later runs list it under **"Already
known (do not re-research)"** in the brief and spend effort only on what is genuinely new.
