# LLM Wiki Graph Toolkit

Tooling for mirroring a personal, AI-maintained wiki (Obsidian-style, see [CLAUDE.md](CLAUDE.md)) into Neo4j, plus two comparable question-answering agents over the same content — one backed by graph traversal (Cypher), one by vector-embedding retrieval.

## What's here

- **[CLAUDE.md](CLAUDE.md)** — the conventions the wiki follows (page format, ingest workflow, linking rules). If you're pointing this toolkit at your own wiki, this is the spec your `wiki/` folder needs to match.
- **`scripts/neo4j_import.py`** — parses every `wiki/*.md` page and mirrors it into Neo4j as `(:WikiPage)` nodes with `[:LINKS_TO]` relationships derived from `[[wikilinks]]`. Idempotent (MERGE-based) — safe to re-run after any wiki edit.
- **`scripts/ask_graph.py`** — question → Cypher → answer. Generates a read-only Cypher query from the question via OpenAI, runs it against the Neo4j mirror, and synthesizes a cited answer from the results.
- **`scripts/ask_vector.py`** — question → embed → top-k → answer. A deliberately minimal vector-RAG baseline over the same `wiki/*.md` files, for comparing against the graph agent (no chunking, no caching, no reranking).

## What's not here

`wiki/` (the actual knowledge-base content), `raw/` (source documents), and `.env` (secrets) are intentionally excluded — see [.gitignore](.gitignore). This repo is the tooling only; point it at your own wiki following the `CLAUDE.md` conventions.

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

Both Q&A scripts accept a question as a command-line argument, or run with no arguments for an interactive loop (`exit` to quit).

```
python scripts/ask_graph.py "What connects operator learning to Bayesian deep learning?"
python scripts/ask_vector.py "What connects operator learning to Bayesian deep learning?" --k 5
```

Re-run `neo4j_import.py` any time `wiki/` changes to keep the graph in sync.
