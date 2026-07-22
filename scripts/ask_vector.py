"""A minimal question -> embed -> top-k retrieve -> answer baseline over the wiki's raw markdown.

This is the vector-RAG baseline to compare against the graph-based ask_graph.py --
same source documents (wiki/*.md), no chunking beyond whole-page, no caching, no
reranking. Intentionally minimal: this exists to be compared against, not shipped.

Pipeline:
    1. Load every wiki/*.md page (the same docs neo4j_import.py mirrors), skipping
       index.md/log.md.
    2. Embed all pages (OpenAI embeddings, batched).
    3. Embed the question, rank pages by cosine similarity, take the top k.
    4. Feed the question + top-k page contents to an OpenAI model for a cited answer.

Usage:
    python scripts/ask_vector.py "question" [--k 5]
    python scripts/ask_vector.py            # interactive loop, type 'exit' to quit

Requires OPENAI_API_KEY in the environment or in a ".env" file in the project
root (see .env.example).
"""

import os
import sys
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

WIKI_DIR = Path(__file__).resolve().parent.parent / "wiki"
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
EMBED_MODEL = os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-small")
EMBED_BATCH_SIZE = 100
DEFAULT_K = 5

ANSWER_SYSTEM_PROMPT = """You answer questions about a personal knowledge-base wiki using ONLY \
the retrieved page excerpts provided to you. Cite specific page names (in backticks) when you \
reference them. If the excerpts don't actually answer the question, say so plainly rather than \
guessing."""


def load_pages() -> list[tuple[str, str]]:
    pages = []
    for path in sorted(WIKI_DIR.glob("*.md")):
        if path.stem in {"index", "log"}:
            continue
        pages.append((path.stem, path.read_text(encoding="utf-8")))
    return pages


def embed_all(client: OpenAI, texts: list[str]) -> np.ndarray:
    vectors = []
    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[i:i + EMBED_BATCH_SIZE]
        resp = client.embeddings.create(model=EMBED_MODEL, input=batch)
        vectors.extend(d.embedding for d in resp.data)
    return np.array(vectors)


def top_k_indices(question_vec: np.ndarray, page_vecs: np.ndarray, k: int) -> list[int]:
    # OpenAI embeddings are unit-normalized, so dot product == cosine similarity.
    sims = page_vecs @ question_vec
    return list(np.argsort(-sims)[:k])


def synthesize_answer(client: OpenAI, question: str, retrieved: list[tuple[str, str]]) -> str:
    context = "\n\n---\n\n".join(f"[{name}]\n{content}" for name, content in retrieved)
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
            {"role": "user", "content": f"Question: {question}\n\nRetrieved pages:\n{context}"},
        ],
        temperature=0,
    )
    return resp.choices[0].message.content.strip()


def ask(client: OpenAI, pages: list[tuple[str, str]], page_vecs: np.ndarray, question: str, k: int) -> None:
    q_vec = embed_all(client, [question])[0]
    retrieved = [pages[i] for i in top_k_indices(q_vec, page_vecs, k)]

    print(f"\n[retrieved] {', '.join(name for name, _ in retrieved)}")
    answer = synthesize_answer(client, question, retrieved)
    print(f"\n[answer]\n{answer}\n")


def main() -> None:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY not found. Copy .env.example to .env and fill in your key.")
        sys.exit(1)

    args = sys.argv[1:]
    k = DEFAULT_K
    if "--k" in args:
        idx = args.index("--k")
        k = int(args[idx + 1])
        del args[idx:idx + 2]

    client = OpenAI(api_key=api_key)

    print("Loading and embedding wiki pages...")
    pages = load_pages()
    page_vecs = embed_all(client, [content for _, content in pages])
    print(f"Embedded {len(pages)} pages (k={k}).\n")

    if args:
        ask(client, pages, page_vecs, " ".join(args), k)
    else:
        print("Interactive mode. Type 'exit' to quit.")
        while True:
            question = input("\n> ").strip()
            if question.lower() in {"exit", "quit"}:
                break
            if question:
                ask(client, pages, page_vecs, question, k)


if __name__ == "__main__":
    main()
