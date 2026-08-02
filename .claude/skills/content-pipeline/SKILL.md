---
name: content-pipeline
description: Run the five-stage agentic content pipeline over papers, YouTube videos, articles or notes — ingest and transcribe, summarize each source, edit into one brief, then fan out to a wiki/Neo4j knowledge-graph update and a LinkedIn draft in Notion. Use when the user supplies sources to process, says "run the pipeline", "ingest these", or invokes /content-pipeline.
---

# Content pipeline

Five subagents, four stages, with a fan-out at the end:

```
inputs ──▶ content-ingestor ──▶ source-summarizer (one per source, parallel)
                                          │
                                          ▼
                                   content-editor ──▶ brief.md
                                          │
                        ┌─────────────────┴─────────────────┐
                        ▼                                   ▼
                wiki-graph-writer                    linkedin-drafter
             (wiki/ pages + index + log)          (Notion draft page)
```

You orchestrate. The subagents do the work and write files; you pass paths between
them, not document text.

## Before you start

Inputs come from the skill arguments. If none were given, ask the user for them —
do not guess. Accepted: YouTube URLs, arXiv/PDF links, local PDF paths, article
URLs, local `.md`/`.txt` notes. Mixed batches are normal and expected.

Create the run directory: `.pipeline/<YYYY-MM-DD>-<short-topic-slug>/`. Every
intermediate artifact lives there, so a run is inspectable and re-runnable.

## Stage 1 — ingest

Spawn **content-ingestor** once, with all inputs and the run directory. It runs
`scripts/fetch_source.py`, repairs failures, and writes `manifest.json`.

Read the manifest yourself before continuing. If a source failed and it was the
only one, stop and tell the user — do not run a pipeline over nothing. If some
succeeded, report which failed and continue with the rest.

## Stage 2 — summarize (parallel)

Spawn one **source-summarizer** per successfully ingested source, **all in a single
message** so they run concurrently. Give each one: its single `raw/` path and the
run directory. Do not give a summarizer other sources — independence keeps the
comparison in stage 3 honest.

## Stage 3 — edit

Once every summarizer has returned, spawn **content-editor** once with the run
directory. It reads all summaries plus `wiki/index.md` and writes `brief.md`.

Read `brief.md` yourself. This is the pipeline's quality gate: if the through-line
is empty, the concept map has no entries, or citations have gone missing, send it
back to a fresh content-editor with specific corrections rather than propagating a
weak brief into both outputs.

## Stage 4 — fan out (parallel)

Spawn **wiki-graph-writer** and **linkedin-drafter** together **in a single
message**. Both read the same `brief.md`; neither depends on the other. Give each
the run directory.

They write to different places (`wiki/` vs Notion), so there is no write conflict.

## Finish

Report to the user, compactly:

- Sources ingested, and anything that failed.
- Wiki pages created and updated; links added.
- The Notion draft URL and its opening line.
- The reminder: `python scripts/neo4j_import.py` to refresh the Neo4j mirror.
- Any judgement call a subagent flagged for review.

Nothing is published anywhere — the LinkedIn post is a draft for the user to edit.

## Notes

- **`raw/` is immutable.** New sources are added as new files; existing ones are
  never edited. Verify with `git status raw/` if a subagent's report is ambiguous.
- **Running a single stage is fine.** If the user asks only for a LinkedIn draft
  from an existing run, spawn just that subagent with the existing run directory.
- **Re-running a stage** means spawning a fresh subagent with corrections, not
  editing its output yourself — that keeps the artifacts in the run directory
  consistent with what actually produced them.
