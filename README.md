# LLM Wiki Graph Toolkit

Tooling for mirroring a personal, AI-maintained wiki (Obsidian-style, see [CLAUDE.md](CLAUDE.md)) into Neo4j, plus two comparable question-answering agents over the same content — one backed by graph traversal (Cypher), one by vector-embedding retrieval. It also ships an agentic content pipeline that takes papers and videos in one end and produces wiki pages and a LinkedIn draft out the other.

## What's here

- **[CLAUDE.md](CLAUDE.md)** — the conventions the wiki follows (page format, ingest workflow, linking rules). If you're pointing this toolkit at your own wiki, this is the spec your `wiki/` folder needs to match.
- **`.claude/agents/` + `.claude/skills/content-pipeline/`** — the five-subagent content pipeline (see below).
- **`scripts/fetch_source.py`** — normalizes mixed inputs into `raw/` source files: YouTube transcripts, PDF/arXiv text extraction, article text, local notes. Used by the pipeline's ingest stage, but runnable on its own.
- **`scripts/neo4j_import.py`** — parses every `wiki/*.md` page and mirrors it into Neo4j as `(:WikiPage)` nodes with `[:LINKS_TO]` relationships derived from `[[wikilinks]]`. Idempotent (MERGE-based) — safe to re-run after any wiki edit.
- **`scripts/ask_graph.py`** — question → Cypher → answer. Generates a read-only Cypher query from the question via OpenAI, runs it against the Neo4j mirror, and synthesizes a cited answer from the results.
- **`scripts/ask_vector.py`** — question → embed → top-k → answer. A deliberately minimal vector-RAG baseline over the same `wiki/*.md` files, for comparing against the graph agent (no chunking, no caching, no reranking).

## The content pipeline

Run `/content-pipeline <urls and paths>` in Claude Code. Five subagents, with a fan-out at the end:

```
inputs ──▶ content-ingestor ──▶ source-summarizer (one per source, in parallel)
                                          │
                                          ▼
                                   content-editor ──▶ brief.md
                                          │
                        ┌─────────────────┴─────────────────┐
                        ▼                                   ▼
                wiki-graph-writer                    linkedin-drafter
             (wiki/ pages + index + log)          (Notion draft page)
```

| Stage | Subagent | Does |
|---|---|---|
| 1 | `content-ingestor` | Transcribes YouTube videos, extracts PDFs and articles into `raw/`; repairs failed fetches via `WebFetch`; writes a manifest |
| 2 | `source-summarizer` | One per source, run concurrently and independently — a dense, fully cited structured summary |
| 3 | `content-editor` | Merges the summaries into one editorial brief: through-line, concept map, contradictions between sources, public angles |
| 4a | `wiki-graph-writer` | Writes `wiki/` pages in the CLAUDE.md format, wires up `[[wikilinks]]` in both directions, updates `index.md` and `log.md` |
| 4b | `linkedin-drafter` | Writes a LinkedIn post draft with a fact-check section and saves it as a Notion page |

Inputs can be mixed in one run — a paper, two videos and a blog post is a normal batch. Intermediate artifacts (manifest, summaries, brief) land in `.pipeline/<date>-<topic>/` so a run is inspectable and individual stages can be re-run.

Notes:

- Stages 2 and 4 fan out genuinely in parallel; stage 4's two agents write to different places (`wiki/` vs Notion), so they never conflict.
- `raw/` stays immutable, per CLAUDE.md — colliding filenames get a `-2` suffix rather than overwriting.
- The LinkedIn stage **drafts only**, never publishes. If Notion is unavailable it falls back to `drafts/<slug>-linkedin.md` rather than losing the work.
- The wiki stage deliberately does *not* run `neo4j_import.py` (it needs live credentials) — re-run it yourself to refresh the graph.
- The Notion destination is found by search (a "LinkedIn Drafts" / "Content Drafts" database or page). Create one if you want drafts filed somewhere specific.

## What's not here

`wiki/` (the actual knowledge-base content), `raw/` (source documents), `.pipeline/` (run artifacts), `drafts/` and `.env` (secrets) are intentionally excluded — see [.gitignore](.gitignore). This repo is the tooling only; point it at your own wiki following the `CLAUDE.md` conventions. The pipeline's agent and skill definitions under `.claude/` *are* tracked — they're project config, not machine-local settings.

## Setup

1. `pip install -r requirements.txt`
2. `cp .env.example .env` and fill in your `OPENAI_API_KEY`
3. Run Neo4j, e.g.:
   ```
   docker run -d --name neo4j -p 7474:7474 -p 7687:7687 -e NEO4J_AUTH=neo4j/your-password neo4j:5
   ```
   If you use different credentials, update `NEO4J_URI`/`NEO4J_USER`/`NEO4J_PASSWORD` at the top of `neo4j_import.py` and `ask_graph.py`.
4. `python scripts/neo4j_import.py` to build (or refresh) the graph mirror from `wiki/`

## Usage

### Ingesting new material

In Claude Code, hand the pipeline whatever you're working through:

```
/content-pipeline https://youtu.be/VIDEO_ID https://arxiv.org/abs/1706.03762 ~/notes/seminar.md
```

Or run just the fetcher, without the agents:

```
python scripts/fetch_source.py https://youtu.be/VIDEO_ID paper.pdf --manifest manifest.json
python scripts/fetch_source.py https://youtu.be/VIDEO_ID --lang de --lang en   # non-English video
```

Then `python scripts/neo4j_import.py` to fold the new pages into the graph.

### Asking questions

Both Q&A scripts accept a question as a command-line argument, or run with no arguments for an interactive loop (`exit` to quit).

```
python scripts/ask_graph.py "What connects operator learning to Bayesian deep learning?"
python scripts/ask_vector.py "What connects operator learning to Bayesian deep learning?" --k 5
```

Re-run `neo4j_import.py` any time `wiki/` changes to keep the graph in sync.
