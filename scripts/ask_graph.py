"""A minimal question -> Cypher -> answer agent over the wiki's Neo4j mirror.

Pipeline:
    1. Send the question + a hardcoded schema description + few-shot examples
       to an OpenAI model, asking for ONE read-only Cypher query.
    2. Reject the query if it contains any write keyword (safety guardrail).
    3. Run the query against Neo4j and collect the result rows.
    4. Send the question + query + rows back to OpenAI, asking for a
       natural-language answer that cites specific page names.

Usage:
    python scripts/ask_graph.py "What connects operator learning to Bayesian deep learning?"
    python scripts/ask_graph.py            # interactive loop, type 'exit' to quit

Requires OPENAI_API_KEY in the environment or in a ".env" file in the project
root (see .env.example). Neo4j connection reuses the same defaults as
neo4j_import.py.
"""

import json
import os
import re
import sys

from dotenv import load_dotenv
from neo4j import GraphDatabase
from openai import OpenAI

load_dotenv()

NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "your-password"
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

FORBIDDEN = re.compile(
    r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP|DETACH|CALL\s+apoc\.)\b", re.IGNORECASE
)

SCHEMA_DESCRIPTION = """
Node label: WikiPage
Properties:
  name         - unique key, kebab-case (e.g. "operator-learning")
  title        - human-readable page title
  summary      - one-paragraph summary of the page
  sources      - which raw source documents this page draws from
  last_updated - date string
  content      - the full markdown body of the page (can be long)
  course       - which course/collection this page belongs to
                 (e.g. "Scientific Machine Learning", "Introduction to Particle Simulation",
                 "Architectures and Generative Techniques", "Geometric Deep Learning",
                 "Austrian Company Discovery", "Course overview")
  page_type    - "concept pages", "source summary pages", or similar, from the index section

Relationship: (:WikiPage)-[:LINKS_TO]->(:WikiPage)
  Directed. Page A LINKS_TO page B if A's markdown body contains a [[B]] wikilink.
""".strip()

FEW_SHOT = [
    (
        "What pages are directly linked from operator-learning?",
        "MATCH (p:WikiPage {name:'operator-learning'})-[:LINKS_TO]->(q) "
        "RETURN q.name AS name, q.title AS title, q.summary AS summary",
    ),
    (
        "What is the shortest path between discrete-element-method and bayesian-deep-learning?",
        "MATCH path = shortestPath((a:WikiPage {name:'discrete-element-method'})"
        "-[:LINKS_TO*]-(b:WikiPage {name:'bayesian-deep-learning'})) "
        "RETURN [n IN nodes(path) | n.name] AS path_names",
    ),
    (
        "Which pages have the most incoming links?",
        "MATCH (p:WikiPage)<-[:LINKS_TO]-() "
        "RETURN p.name AS name, p.title AS title, count(*) AS incoming "
        "ORDER BY incoming DESC LIMIT 10",
    ),
    (
        "Which pages have no incoming links at all?",
        "MATCH (p:WikiPage) WHERE NOT ()-[:LINKS_TO]->(p) "
        "RETURN p.name AS name, p.title AS title, p.course AS course",
    ),
    (
        "What does the wiki say about the Bitter Lesson?",
        "MATCH (p:WikiPage) WHERE p.content CONTAINS 'Bitter Lesson' "
        "RETURN p.name AS name, p.title AS title, p.content AS content LIMIT 5",
    ),
    (
        "Which pages bridge two different courses?",
        "MATCH (a:WikiPage)-[:LINKS_TO]->(b:WikiPage) WHERE a.course <> b.course "
        "RETURN a.name AS from_page, a.course AS from_course, "
        "b.name AS to_page, b.course AS to_course LIMIT 25",
    ),
]

CYPHER_SYSTEM_PROMPT = f"""You translate natural-language questions into a single read-only Cypher \
query against a Neo4j graph with this schema:

{SCHEMA_DESCRIPTION}

Rules:
- Output ONLY the Cypher query. No explanation, no markdown code fences, no comments.
- The query must be read-only (no CREATE, MERGE, DELETE, SET, REMOVE, DROP).
- Prefer returning name, title, and relevant properties rather than whole nodes.
- If the question needs full-text matching, use `CONTAINS` (case-sensitive) or
  `toLower(p.content) CONTAINS toLower($term)`-style logic on the `content` field.
- If the question asks for a path or connection between two pages, use shortestPath()
  or a bounded variable-length pattern (e.g. *1..3) to avoid scanning the whole graph.

Examples:
""" + "\n".join(f"Q: {q}\nCypher: {c}" for q, c in FEW_SHOT)

ANSWER_SYSTEM_PROMPT = """You answer questions about a personal knowledge-base wiki using ONLY \
the graph query results provided to you. Cite specific page names (in backticks) when you \
reference them. If the results are empty or don't actually answer the question, say so plainly \
rather than guessing."""


def generate_cypher(client: OpenAI, question: str) -> str:
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": CYPHER_SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ],
        temperature=0,
    )
    query = resp.choices[0].message.content.strip()
    query = re.sub(r"^```(?:cypher)?\s*|\s*```$", "", query, flags=re.MULTILINE).strip()
    return query


def run_cypher(driver, query: str) -> list[dict]:
    with driver.session() as session:
        result = session.run(query)
        return [record.data() for record in result]


def truncate_rows(rows: list[dict], max_rows: int = 20, max_field_len: int = 1500) -> list[dict]:
    trimmed = []
    for row in rows[:max_rows]:
        new_row = {}
        for k, v in row.items():
            if isinstance(v, str) and len(v) > max_field_len:
                v = v[:max_field_len] + " …[truncated]"
            new_row[k] = v
        trimmed.append(new_row)
    return trimmed


def synthesize_answer(client: OpenAI, question: str, cypher: str, rows: list[dict]) -> str:
    payload = {"question": question, "cypher": cypher, "results": truncate_rows(rows)}
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, indent=2, default=str)},
        ],
        temperature=0,
    )
    return resp.choices[0].message.content.strip()


def ask(client: OpenAI, driver, question: str) -> None:
    cypher = generate_cypher(client, question)
    print(f"\n[cypher]\n{cypher}\n")

    if FORBIDDEN.search(cypher):
        print("[blocked] Generated query contains a write/admin keyword — refusing to run it.")
        return

    try:
        rows = run_cypher(driver, cypher)
    except Exception as e:
        print(f"[error running query] {e}")
        return

    print(f"[results] {len(rows)} row(s)")
    answer = synthesize_answer(client, question, cypher, rows)
    print(f"\n[answer]\n{answer}\n")


def main() -> None:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY not found. Copy .env.example to .env and fill in your key.")
        sys.exit(1)

    client = OpenAI(api_key=api_key)
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

    try:
        if len(sys.argv) > 1:
            ask(client, driver, " ".join(sys.argv[1:]))
        else:
            print("Interactive mode. Type 'exit' to quit.")
            while True:
                question = input("\n> ").strip()
                if question.lower() in {"exit", "quit"}:
                    break
                if question:
                    ask(client, driver, question)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
