"""Normalize mixed inputs (YouTube videos, PDFs, article URLs, local text) into raw/ source files.

This is the mechanical half of the ingest step: it does the fetching, transcribing
and text extraction so the content-ingestor agent can focus on judgement calls
(which inputs matter, what to do when a transcript is unavailable, etc.).

Each input becomes one markdown file in raw/ with a provenance header, and the
script prints a JSON manifest describing every source it produced.

Supported inputs:
    - YouTube URL or bare video ID  -> timestamped transcript (youtube-transcript-api)
    - .pdf path or PDF URL          -> per-page text extraction (pypdf)
    - arXiv abs/pdf URL             -> rewritten to the PDF and extracted
    - http(s) URL                   -> article text (stdlib HTML stripper)
    - local .md / .txt path         -> copied verbatim

Existing files in raw/ are never overwritten -- a colliding slug gets a "-2",
"-3" suffix -- so the immutability rule in CLAUDE.md holds.

Usage:
    python scripts/fetch_source.py <input> [<input> ...]
    python scripts/fetch_source.py <input> --manifest .pipeline/run-1/manifest.json
    python scripts/fetch_source.py <input> --lang de --lang en
"""

import argparse
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "raw"

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
ARXIV_RE = re.compile(r"arxiv\.org/(?P<kind>abs|pdf)/(?P<id>[\w.\-/]+)")
USER_AGENT = "Mozilla/5.0 (compatible; llm-wiki-graph-toolkit/1.0)"
TIMESTAMP_EVERY_SECONDS = 30


# --------------------------------------------------------------------------- helpers


def slugify(text: str, fallback: str = "source") -> str:
    """Lowercase kebab-case slug, matching the wiki's page-naming rule."""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.replace(".", " ")  # keep version/ID dots as separators, not deletions
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return (text or fallback)[:80]


def unique_path(directory: Path, slug: str, suffix: str = ".md") -> Path:
    """Return a path in `directory` that does not exist yet (never overwrite raw/)."""
    candidate = directory / f"{slug}{suffix}"
    n = 2
    while candidate.exists():
        candidate = directory / f"{slug}-{n}{suffix}"
        n += 1
    return candidate


def http_get(url: str, timeout: int = 30) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def write_source(slug: str, title: str, kind: str, origin: str, body: str, extra: dict) -> Path:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = unique_path(RAW_DIR, slug)
    header = [
        f"# {title}",
        "",
        f"**Source type**: {kind}",
        f"**Origin**: {origin}",
        f"**Fetched**: {date.today().isoformat()}",
    ]
    for key, value in extra.items():
        header.append(f"**{key}**: {value}")
    header += ["", "---", ""]
    path.write_text("\n".join(header) + body.strip() + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- input routing


def youtube_video_id(raw_input: str) -> str | None:
    if VIDEO_ID_RE.match(raw_input):
        return raw_input
    try:
        parsed = urllib.parse.urlparse(raw_input)
    except ValueError:
        return None
    if parsed.hostname not in YOUTUBE_HOSTS:
        return None
    if parsed.hostname in {"youtu.be", "www.youtu.be"}:
        candidate = parsed.path.lstrip("/").split("/")[0]
    elif parsed.path.startswith(("/shorts/", "/embed/", "/live/")):
        candidate = parsed.path.split("/")[2]
    else:
        candidate = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
    return candidate if VIDEO_ID_RE.match(candidate) else None


def classify(raw_input: str) -> str:
    if youtube_video_id(raw_input):
        return "youtube"
    parsed = urllib.parse.urlparse(raw_input)
    if parsed.scheme in {"http", "https"}:
        # arXiv's modern PDF links carry no .pdf extension, so match the host explicitly.
        is_pdf = parsed.path.lower().endswith((".pdf", "/pdf")) or bool(ARXIV_RE.search(raw_input))
        return "pdf-url" if is_pdf else "url"
    path = Path(raw_input)
    if path.suffix.lower() == ".pdf":
        return "pdf"
    if path.suffix.lower() in {".md", ".txt", ".markdown", ".vtt", ".srt"}:
        return "text"
    return "unknown"


# --------------------------------------------------------------------------- YouTube


def youtube_title(video_id: str) -> str:
    """Best-effort title via the keyless oEmbed endpoint; falls back to the video ID."""
    url = "https://www.youtube.com/oembed?" + urllib.parse.urlencode(
        {"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"}
    )
    try:
        return json.loads(http_get(url, timeout=15)).get("title") or video_id
    except Exception:
        return video_id


def fetch_transcript_snippets(video_id: str, languages: list[str]) -> list[tuple[float, str]]:
    """Fetch a transcript across youtube-transcript-api 0.6.x and 1.x APIs."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError(
            "youtube-transcript-api is not installed. Run: pip install -r requirements.txt"
        ) from exc

    if hasattr(YouTubeTranscriptApi, "fetch"):  # 1.x instance API
        fetched = YouTubeTranscriptApi().fetch(video_id, languages=languages)
        return [(float(s.start), s.text) for s in fetched]

    raw = YouTubeTranscriptApi.get_transcript(video_id, languages=languages)  # 0.6.x
    return [(float(s["start"]), s["text"]) for s in raw]


def format_transcript(snippets: list[tuple[float, str]]) -> str:
    """Join snippets into paragraphs, prefixing a [mm:ss] marker periodically."""
    lines: list[str] = []
    buffer: list[str] = []
    block_start = snippets[0][0] if snippets else 0.0

    def flush(start: float) -> None:
        if buffer:
            minutes, seconds = divmod(int(start), 60)
            lines.append(f"[{minutes:02d}:{seconds:02d}] " + " ".join(buffer))
            lines.append("")
            buffer.clear()

    for start, text in snippets:
        text = text.replace("\n", " ").strip()
        if not text:
            continue
        if start - block_start >= TIMESTAMP_EVERY_SECONDS:
            flush(block_start)
            block_start = start
        buffer.append(text)
    flush(block_start)
    return "\n".join(lines)


def ingest_youtube(raw_input: str, languages: list[str]) -> dict:
    video_id = youtube_video_id(raw_input)
    title = youtube_title(video_id)
    snippets = fetch_transcript_snippets(video_id, languages)
    if not snippets:
        raise RuntimeError("transcript came back empty")
    body = format_transcript(snippets)
    duration = int(snippets[-1][0])
    path = write_source(
        slug=slugify(title, fallback=video_id),
        title=title,
        kind="YouTube transcript",
        origin=f"https://www.youtube.com/watch?v={video_id}",
        body=body,
        extra={"Video ID": video_id, "Transcript length": f"~{duration // 60} min"},
    )
    return {"path": path, "title": title, "kind": "youtube"}


# --------------------------------------------------------------------------- PDF


def extract_pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError("pypdf is not installed. Run: pip install -r requirements.txt") from exc

    import io

    reader = PdfReader(io.BytesIO(data))
    parts = []
    for number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            parts.append(f"## Page {number}\n\n{text}")
    if not parts:
        raise RuntimeError("no extractable text (likely a scanned PDF -- OCR it first)")
    return "\n\n".join(parts)


def pdf_title(data: bytes, fallback: str) -> str:
    try:
        import io

        from pypdf import PdfReader

        title = (PdfReader(io.BytesIO(data)).metadata or {}).get("/Title")
        return str(title).strip() if title and str(title).strip() else fallback
    except Exception:
        return fallback


def ingest_pdf(raw_input: str, kind: str) -> dict:
    if kind == "pdf-url":
        url = raw_input
        arxiv = ARXIV_RE.search(url)
        if arxiv:
            url = f"https://arxiv.org/pdf/{arxiv.group('id')}"
            fallback = f"arxiv {arxiv.group('id').replace('/', '-')}"
        else:
            fallback = Path(urllib.parse.urlparse(url).path).stem or "paper"
        data = http_get(url, timeout=60)
        origin = raw_input
    else:
        source = Path(raw_input).expanduser().resolve()
        if not source.is_file():
            raise RuntimeError(f"file not found: {source}")
        data = source.read_bytes()
        fallback = source.stem
        origin = str(source)

    title = pdf_title(data, fallback)
    body = extract_pdf_text(data)
    path = write_source(
        slug=slugify(title, fallback="paper"),
        title=title,
        kind="PDF / paper",
        origin=origin,
        body=body,
        extra={"Pages extracted": str(body.count("\n## Page ") + 1)},
    )
    return {"path": path, "title": title, "kind": "pdf"}


# --------------------------------------------------------------------------- web article


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer"}
    BLOCK = {"p", "div", "section", "article", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self.BLOCK:
            self.chunks.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip_depth and data.strip():
            self.chunks.append(data.strip() + " ")

    def text(self) -> str:
        joined = "".join(self.chunks)
        joined = re.sub(r"[ \t]+", " ", joined)
        return re.sub(r"\n\s*\n\s*", "\n\n", joined).strip()


def ingest_url(raw_input: str) -> dict:
    html = http_get(raw_input, timeout=45).decode("utf-8", errors="replace")
    parser = _TextExtractor()
    parser.feed(html)
    body = parser.text()
    if len(body) < 200:
        raise RuntimeError(
            "extracted almost no text (JS-rendered page?) -- have the ingestor agent retry with WebFetch"
        )
    title = parser.title.strip() or urllib.parse.urlparse(raw_input).netloc
    path = write_source(
        slug=slugify(title, fallback="article"),
        title=title,
        kind="Web article",
        origin=raw_input,
        body=body,
        extra={},
    )
    return {"path": path, "title": title, "kind": "url"}


# --------------------------------------------------------------------------- local text


def ingest_text(raw_input: str) -> dict:
    source = Path(raw_input).expanduser().resolve()
    if not source.is_file():
        raise RuntimeError(f"file not found: {source}")
    body = source.read_text(encoding="utf-8", errors="replace")
    title = next(
        (line[2:].strip() for line in body.splitlines() if line.startswith("# ")),
        source.stem,
    )
    path = write_source(
        slug=slugify(title, fallback=source.stem),
        title=title,
        kind="Local document",
        origin=str(source),
        body=body,
        extra={},
    )
    return {"path": path, "title": title, "kind": "text"}


# --------------------------------------------------------------------------- driver


def ingest_one(raw_input: str, languages: list[str]) -> dict:
    kind = classify(raw_input)
    handlers = {
        "youtube": lambda: ingest_youtube(raw_input, languages),
        "pdf": lambda: ingest_pdf(raw_input, "pdf"),
        "pdf-url": lambda: ingest_pdf(raw_input, "pdf-url"),
        "url": lambda: ingest_url(raw_input),
        "text": lambda: ingest_text(raw_input),
    }
    if kind == "unknown":
        return {"input": raw_input, "kind": "unknown", "status": "failed",
                "error": "unrecognized input (expected a YouTube URL, PDF, http(s) URL, or .md/.txt path)"}
    try:
        result = handlers[kind]()
    except Exception as exc:
        return {"input": raw_input, "kind": kind, "status": "failed", "error": str(exc)}

    path: Path = result["path"]
    return {
        "input": raw_input,
        "kind": result["kind"],
        "status": "ok",
        "title": result["title"],
        "path": str(path.relative_to(PROJECT_ROOT)),
        "chars": len(path.read_text(encoding="utf-8")),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("inputs", nargs="+", help="YouTube URLs, PDF paths/URLs, article URLs, or local .md/.txt files")
    parser.add_argument("--lang", action="append", dest="languages", default=None,
                        help="preferred transcript language, repeatable (default: en)")
    parser.add_argument("--manifest", help="also write the JSON manifest to this path")
    args = parser.parse_args()

    languages = args.languages or ["en"]
    sources = [ingest_one(raw_input, languages) for raw_input in args.inputs]
    manifest = {"fetched": date.today().isoformat(), "sources": sources}
    payload = json.dumps(manifest, indent=2)

    if args.manifest:
        manifest_path = Path(args.manifest)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(payload + "\n", encoding="utf-8")

    print(payload)
    if any(source["status"] == "failed" for source in sources):
        sys.exit(1)


if __name__ == "__main__":
    main()
