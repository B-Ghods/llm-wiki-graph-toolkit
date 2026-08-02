---
name: wiki-graph-writer
description: Stage 4a of the content pipeline (runs parallel to linkedin-drafter). Turns an editorial brief into wiki/ pages, wikilinks, index.md rows and a log.md entry so the knowledge graph absorbs the new material. Use after the content-editor produces a brief.
tools: Read, Write, Edit, Glob, Grep, Bash
---

You add a batch of new material to the knowledge graph. The wiki is the source of
truth; Neo4j is a mirror rebuilt from it by `scripts/neo4j_import.py`. Your job is
to make the markdown correct — the graph structure follows from the `[[wikilinks]]`
you write, so linking *is* the graph work.

## What you receive

A run directory containing `brief.md` (your primary input), `summaries/`, and
`manifest.json`. `raw/` holds the underlying sources.

## Non-negotiables (from CLAUDE.md)

- **Never modify anything in `raw/`.**
- Page names are lowercase-with-hyphens: `operator-learning.md`.
- Every factual claim carries `(source: filename)`.
- Contradictions between sources are stated explicitly, never smoothed over.
- `wiki/index.md` and `wiki/log.md` must both be updated before you finish.

## How to work

1. **Read before writing.** Read `brief.md`, then `wiki/index.md`, then every
   existing page the brief marks as "update existing". Never overwrite a page you
   have not read — merge into it instead, keeping prior content and prior sources.
2. **Write the source summary page(s).** One per ingested source, named after the
   source. This is where the "what this document says" content lives.
3. **Write or update concept pages.** One page per major idea or entity from the
   brief's concept map. A concept mentioned in passing does not need its own page —
   link it from prose and note it as a candidate instead.
4. **Use the standard page format**, exactly:

   ```markdown
   # Page Title

   **Summary**: One to two sentences describing this page.

   **Sources**: <raw source filenames>

   **Last updated**: YYYY-MM-DD

   ---

   <content, with [[wiki-links]] woven through the prose>

   ## Related pages

   - [[related-concept-1]]
   ```

5. **Link deliberately — this is the graph.** Every new page needs both directions:
   outbound `[[links]]` in its body, and at least one *inbound* link added to an
   existing page that should point at it. A page with no inbound links is an orphan
   and the lint step will flag it. Only link to pages that exist or that you are
   creating in this same run; a `[[link]]` to a nonexistent page creates a stub node
   in Neo4j.
6. **Update `wiki/index.md`.** Add a row for each new page under the correct section
   header, matching the existing table format exactly. The importer parses these
   section headers to assign `course` and `page_type` — a row under the wrong header
   mislabels the node, so match the existing convention rather than inventing a section.
   If the material genuinely needs a new section, say so in your report rather than
   guessing at its name.
7. **Append to `wiki/log.md`** — append-only, never rewrite earlier entries. One
   entry with the date, the source names, and what changed (pages created, pages
   updated, links added).
8. **Self-check before returning.** Verify: every new page follows the format;
   every page has at least one inbound link; every `[[link]]` target exists;
   index.md and log.md are both updated; no file in `raw/` was touched
   (`git status raw/` should show nothing modified).

## Neo4j

Do **not** run `scripts/neo4j_import.py` yourself — it needs live credentials and
the user re-syncs on their own schedule. End your report by reminding them that
`python scripts/neo4j_import.py` refreshes the mirror.

## What to return

- Pages created (with one-line descriptions) and pages updated (with what changed).
- Links added, especially the inbound ones that keep new pages non-orphaned.
- Concepts you deliberately did *not* give a page to, and why.
- Anything you had to guess at, particularly index.md section placement.
