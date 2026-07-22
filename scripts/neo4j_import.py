"""Import the wiki/ markdown pages into Neo4j as a graph.

Each page becomes a (:WikiPage) node; each [[wikilink]] found in a page's
body becomes a (:WikiPage)-[:LINKS_TO]->(:WikiPage) relationship.
Page-to-course/type categorization is read from wiki/index.md section
headers (e.g. "Scientific Machine Learning — concept pages"), not guessed
from filenames.

wiki/*.md remains the source of truth; re-run this script to refresh Neo4j
after editing the markdown.

Usage:
    python scripts/neo4j_import.py
"""

import re
from pathlib import Path

from neo4j import GraphDatabase

WIKI_DIR = Path(__file__).resolve().parent.parent / "wiki"
NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "your-password"

LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:\|[^\]]*)?\]\]")
SUMMARY_RE = re.compile(r"\*\*Summary\*\*:\s*(.+)")
SOURCES_RE = re.compile(r"\*\*Sources\*\*:\s*(.+)")
UPDATED_RE = re.compile(r"\*\*Last updated\*\*:\s*(.+)")
INDEX_ROW_RE = re.compile(r"^\|\s*\[\[([^\]|#]+)")


def parse_index_categories(index_path: Path) -> dict[str, dict[str, str]]:
    """Map page name -> {course, page_type, section} from index.md's section headers."""
    categories: dict[str, dict[str, str]] = {}
    section = ""
    for line in index_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        m = INDEX_ROW_RE.match(line)
        if not m:
            continue
        name = m.group(1).strip()
        if "—" in section:
            course, page_type = (p.strip() for p in section.split("—", 1))
        else:
            course, page_type = section, ""
        categories[name] = {"course": course, "page_type": page_type, "section": section}
    return categories


def parse_page(md_path: Path) -> dict:
    text = md_path.read_text(encoding="utf-8")
    name = md_path.stem
    title = ""
    for line in text.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break
    summary_m = SUMMARY_RE.search(text)
    sources_m = SOURCES_RE.search(text)
    updated_m = UPDATED_RE.search(text)
    links = sorted({m.group(1).strip() for m in LINK_RE.finditer(text)} - {name})
    return {
        "name": name,
        "title": title or name,
        "summary": summary_m.group(1).strip() if summary_m else "",
        "sources": sources_m.group(1).strip() if sources_m else "",
        "last_updated": updated_m.group(1).strip() if updated_m else "",
        "content": text,
        "links": links,
    }


def main() -> None:
    categories = parse_index_categories(WIKI_DIR / "index.md")
    pages = [parse_page(p) for p in sorted(WIKI_DIR.glob("*.md")) if p.name != "index.md" and p.name != "log.md"]
    print(f"Parsed {len(pages)} pages from {WIKI_DIR}")

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    with driver.session() as session:
        session.run("CREATE CONSTRAINT wikipage_name IF NOT EXISTS FOR (p:WikiPage) REQUIRE p.name IS UNIQUE")

        for page in pages:
            cat = categories.get(page["name"], {"course": "", "page_type": "", "section": ""})
            session.run(
                """
                MERGE (p:WikiPage {name: $name})
                SET p.title = $title,
                    p.summary = $summary,
                    p.sources = $sources,
                    p.last_updated = $last_updated,
                    p.content = $content,
                    p.course = $course,
                    p.page_type = $page_type,
                    p.section = $section
                """,
                **page,
                **cat,
            )

        for page in pages:
            for target in page["links"]:
                session.run(
                    """
                    MERGE (a:WikiPage {name: $source})
                    MERGE (b:WikiPage {name: $target})
                    MERGE (a)-[:LINKS_TO]->(b)
                    """,
                    source=page["name"],
                    target=target,
                )

        node_count = session.run("MATCH (p:WikiPage) RETURN count(p) AS n").single()["n"]
        rel_count = session.run("MATCH ()-[r:LINKS_TO]->() RETURN count(r) AS n").single()["n"]
        print(f"Neo4j now has {node_count} WikiPage nodes and {rel_count} LINKS_TO relationships")

    driver.close()


if __name__ == "__main__":
    main()
