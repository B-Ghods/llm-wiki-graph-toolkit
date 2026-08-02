"""Offline tests for scripts/fetch_source.py.

Everything here runs without network access: the YouTube path is exercised
against stub modules standing in for both youtube-transcript-api generations,
and the PDF path against a PDF built byte-by-byte in the fixture.

Usage:
    python -m unittest discover tests
    python tests/test_fetch_source.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import fetch_source as fs  # noqa: E402


def build_pdf(pages: list[str], title: str | None = None) -> bytes:
    """Build a minimal multi-page PDF with real, extractable text."""
    objs: list[bytes] = [b"", b""]  # placeholders for catalog + pages
    kids, page_objs = [], []
    next_id = 3
    font_id_slot: list[int] = []

    for text in pages:
        content = (
            "BT /F1 12 Tf 20 250 Td (" + text.replace("(", "").replace(")", "") + ") Tj ET"
        ).encode()
        page_id, content_id = next_id, next_id + 1
        next_id += 2
        kids.append(b"%d 0 R" % page_id)
        page_objs.append((page_id, content_id, content))

    font_id = next_id
    font_id_slot.append(font_id)

    body: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [" + b" ".join(kids) + b"] /Count %d >>" % len(pages),
        font_id: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for page_id, content_id, content in page_objs:
        body[page_id] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Contents %d 0 R "
            b"/Resources << /Font << /F1 %d 0 R >> >> >>" % (content_id, font_id)
        )
        body[content_id] = b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for obj_id in sorted(body):
        offsets[obj_id] = len(out)
        out += b"%d 0 obj\n" % obj_id + body[obj_id] + b"\nendobj\n"

    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (max(body) + 1)
    for obj_id in range(1, max(body) + 1):
        out += b"%010d 00000 n \n" % offsets.get(obj_id, 0)
    trailer = b"<< /Size %d /Root 1 0 R" % (max(body) + 1)
    if title:
        trailer += b" /Info << /Title (" + title.encode() + b") >>"
    out += b"trailer\n" + trailer + b" >>\nstartxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


class TempRawDir(unittest.TestCase):
    """Base class that redirects raw/ writes into a throwaway directory."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.raw = self.tmp / "raw"
        patcher_raw = mock.patch.object(fs, "RAW_DIR", self.raw)
        patcher_root = mock.patch.object(fs, "PROJECT_ROOT", self.tmp)
        patcher_raw.start()
        patcher_root.start()
        self.addCleanup(patcher_raw.stop)
        self.addCleanup(patcher_root.stop)
        self.addCleanup(self._tmp.cleanup)


class TestRouting(unittest.TestCase):
    def test_youtube_url_forms(self):
        for url in [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ?t=30",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "https://www.youtube.com/embed/dQw4w9WgXcQ",
            "https://www.youtube.com/live/dQw4w9WgXcQ",
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ&list=PL1",
            "dQw4w9WgXcQ",
        ]:
            with self.subTest(url=url):
                self.assertEqual(fs.youtube_video_id(url), "dQw4w9WgXcQ")
                self.assertEqual(fs.classify(url), "youtube")

    def test_non_youtube_returns_no_video_id(self):
        for url in ["https://vimeo.com/12345", "https://example.com/watch?v=short", "notes.md"]:
            with self.subTest(url=url):
                self.assertIsNone(fs.youtube_video_id(url))

    def test_pdf_and_arxiv_routing(self):
        cases = {
            "https://arxiv.org/abs/1706.03762": "pdf-url",
            "https://arxiv.org/abs/1706.03762v5": "pdf-url",
            "https://arxiv.org/pdf/1706.03762": "pdf-url",  # modern link, no extension
            "https://arxiv.org/pdf/2301.00001.pdf": "pdf-url",
            "https://openreview.net/pdf?id=abc": "pdf-url",  # path ends in /pdf
            "https://example.com/blog/post": "url",
            "paper.pdf": "pdf",
            "notes.md": "text",
            "transcript.vtt": "text",
            "archive.zip": "unknown",
        }
        for raw_input, expected in cases.items():
            with self.subTest(raw_input=raw_input):
                self.assertEqual(fs.classify(raw_input), expected)


class TestSlugify(unittest.TestCase):
    def test_kebab_case_and_punctuation(self):
        self.assertEqual(fs.slugify("Attention Is All You Need! (v2)"), "attention-is-all-you-need-v2")

    def test_dots_become_separators_not_deletions(self):
        self.assertEqual(fs.slugify("arxiv 1706.03762"), "arxiv-1706-03762")
        self.assertEqual(fs.slugify("GPT-4.5 Turbo"), "gpt-4-5-turbo")

    def test_unicode_is_transliterated(self):
        self.assertEqual(fs.slugify("Über Föhn: naïve café"), "uber-fohn-naive-cafe")

    def test_empty_falls_back(self):
        self.assertEqual(fs.slugify("   ", fallback="source"), "source")
        self.assertEqual(fs.slugify("!!!", fallback="paper"), "paper")

    def test_length_is_bounded(self):
        self.assertLessEqual(len(fs.slugify("word " * 100)), 80)


class TestRawImmutability(TempRawDir):
    def test_colliding_names_get_suffixes_and_never_overwrite(self):
        self.raw.mkdir(parents=True)
        first = fs.write_source("note", "Note", "Local document", "/a.md", "one", {})
        second = fs.write_source("note", "Note", "Local document", "/b.md", "two", {})
        third = fs.write_source("note", "Note", "Local document", "/c.md", "three", {})

        self.assertEqual([p.name for p in (first, second, third)],
                         ["note.md", "note-2.md", "note-3.md"])
        self.assertIn("one", first.read_text())  # original untouched
        self.assertIn("two", second.read_text())

    def test_header_format(self):
        path = fs.write_source("t", "A Title", "PDF / paper", "http://x/y.pdf", "Body text.",
                               {"Pages extracted": "2"})
        text = path.read_text()
        self.assertTrue(text.startswith("# A Title\n"))
        self.assertIn("**Source type**: PDF / paper", text)
        self.assertIn("**Origin**: http://x/y.pdf", text)
        self.assertIn("**Fetched**: ", text)
        self.assertIn("**Pages extracted**: 2", text)
        self.assertIn("\n---\n", text)
        self.assertTrue(text.rstrip().endswith("Body text."))


class TestTranscriptFormatting(unittest.TestCase):
    def test_blocks_break_on_the_timestamp_interval(self):
        snippets = [(0.0, "hello"), (5.0, "there"), (35.0, "next"), (70.5, "third")]
        out = fs.format_transcript(snippets)
        self.assertIn("[00:00] hello there", out)
        self.assertIn("[00:35] next", out)
        self.assertIn("[01:10] third", out)

    def test_empty_and_blank_snippets(self):
        self.assertEqual(fs.format_transcript([]), "")
        self.assertNotIn("[00:00]", fs.format_transcript([(0.0, "   "), (1.0, "")]))

    def test_newlines_inside_snippets_are_flattened(self):
        self.assertIn("[00:00] a b", fs.format_transcript([(0.0, "a\nb")]))


class TestTranscriptApiCompat(unittest.TestCase):
    """The 0.6.x/1.x shim is the one piece that cannot be checked against the live API here."""

    @staticmethod
    def _install(fake_class):
        module = type(sys)("youtube_transcript_api")
        module.YouTubeTranscriptApi = fake_class
        return mock.patch.dict(sys.modules, {"youtube_transcript_api": module})

    def test_modern_1x_instance_api(self):
        class Snippet:
            def __init__(self, start, text):
                self.start, self.text = start, text

        captured = {}

        class Fake:
            def fetch(self, video_id, languages=("en",)):
                captured["video_id"] = video_id
                captured["languages"] = list(languages)
                return [Snippet(0.0, "hello"), Snippet(4.0, "world")]

        with self._install(Fake):
            result = fs.fetch_transcript_snippets("abc12345678", ["de", "en"])

        self.assertEqual(result, [(0.0, "hello"), (4.0, "world")])
        self.assertEqual(captured["video_id"], "abc12345678")
        self.assertEqual(captured["languages"], ["de", "en"])

    def test_legacy_06x_classmethod_api(self):
        captured = {}

        class Fake:  # no .fetch attribute -> legacy branch
            @staticmethod
            def get_transcript(video_id, languages=("en",)):
                captured["video_id"] = video_id
                captured["languages"] = list(languages)
                return [{"start": 0.0, "text": "hello"}, {"start": 4.0, "text": "world"}]

        with self._install(Fake):
            result = fs.fetch_transcript_snippets("abc12345678", ["en"])

        self.assertEqual(result, [(0.0, "hello"), (4.0, "world")])
        self.assertEqual(captured["video_id"], "abc12345678")

    def test_missing_dependency_is_reported_clearly(self):
        with mock.patch.dict(sys.modules, {"youtube_transcript_api": None}):
            with self.assertRaises(RuntimeError) as ctx:
                fs.fetch_transcript_snippets("abc12345678", ["en"])
        self.assertIn("pip install", str(ctx.exception))


class TestYoutubeIngest(TempRawDir):
    def test_writes_a_timestamped_transcript_file(self):
        snippets = [(0.0, "Neural operators"), (45.0, "learn function mappings")]
        with mock.patch.object(fs, "youtube_title", return_value="Operator Learning 101"), \
             mock.patch.object(fs, "fetch_transcript_snippets", return_value=snippets):
            result = fs.ingest_one("https://youtu.be/dQw4w9WgXcQ", ["en"])

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["path"], "raw/operator-learning-101.md")
        text = (self.tmp / result["path"]).read_text()
        self.assertIn("**Video ID**: dQw4w9WgXcQ", text)
        self.assertIn("**Origin**: https://www.youtube.com/watch?v=dQw4w9WgXcQ", text)
        self.assertIn("[00:00] Neural operators", text)
        self.assertIn("[00:45] learn function mappings", text)

    def test_empty_transcript_is_a_reported_failure_not_an_empty_file(self):
        with mock.patch.object(fs, "youtube_title", return_value="Silent"), \
             mock.patch.object(fs, "fetch_transcript_snippets", return_value=[]):
            result = fs.ingest_one("https://youtu.be/dQw4w9WgXcQ", ["en"])

        self.assertEqual(result["status"], "failed")
        self.assertIn("empty", result["error"])
        self.assertFalse(self.raw.exists() and any(self.raw.iterdir()))


class TestPdf(TempRawDir):
    def test_multi_page_extraction(self):
        data = build_pdf(["Neural operators learn mappings.", "Page two covers benchmarks."])
        text = fs.extract_pdf_text(data)
        self.assertIn("## Page 1", text)
        self.assertIn("## Page 2", text)
        self.assertIn("Neural operators", text)
        self.assertIn("benchmarks", text)

    def test_textless_pdf_reports_scanned_hint(self):
        with self.assertRaises(RuntimeError) as ctx:
            fs.extract_pdf_text(build_pdf([]))
        self.assertIn("scanned", str(ctx.exception))

    def test_local_pdf_ingest_uses_metadata_title(self):
        pdf = self.tmp / "input.pdf"
        pdf.write_bytes(build_pdf(["Some content here."], title="A Test Paper"))
        result = fs.ingest_one(str(pdf), ["en"])

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["title"], "A Test Paper")
        self.assertEqual(result["path"], "raw/a-test-paper.md")
        self.assertIn("**Pages extracted**: 1", (self.tmp / result["path"]).read_text())

    def test_arxiv_abs_url_is_rewritten_to_the_pdf(self):
        data = build_pdf(["Attention is all you need."])
        with mock.patch.object(fs, "http_get", return_value=data) as get:
            result = fs.ingest_one("https://arxiv.org/abs/1706.03762", ["en"])

        get.assert_called_once()
        self.assertEqual(get.call_args[0][0], "https://arxiv.org/pdf/1706.03762")
        self.assertEqual(result["status"], "ok")
        # Origin keeps the URL the user actually supplied.
        self.assertIn("**Origin**: https://arxiv.org/abs/1706.03762",
                      (self.tmp / result["path"]).read_text())

    def test_arxiv_without_metadata_title_gets_a_readable_slug(self):
        with mock.patch.object(fs, "http_get", return_value=build_pdf(["Body."])):
            result = fs.ingest_one("https://arxiv.org/abs/1706.03762", ["en"])
        self.assertEqual(result["path"], "raw/arxiv-1706-03762.md")


class TestHtmlExtraction(unittest.TestCase):
    def _extract(self, html: str):
        parser = fs._TextExtractor()
        parser.feed(html)
        return parser

    def test_scripts_styles_and_nav_are_dropped(self):
        parser = self._extract(
            "<html><head><title>My Post</title><style>a{color:red}</style></head>"
            "<body><nav>skipme</nav><p>Hello world.</p><script>bad()</script>"
            "<div>Second para</div></body></html>"
        )
        text = parser.text()
        self.assertEqual(parser.title.strip(), "My Post")
        self.assertIn("Hello world.", text)
        self.assertIn("Second para", text)
        for noise in ("skipme", "bad()", "color:red"):
            self.assertNotIn(noise, text)

    def test_entities_are_decoded(self):
        self.assertIn("A & B", self._extract("<p>A &amp; B</p>").text())


class TestIngestFailures(TempRawDir):
    def test_unknown_input_type(self):
        result = fs.ingest_one("archive.zip", ["en"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("unrecognized input", result["error"])

    def test_missing_local_file(self):
        result = fs.ingest_one(str(self.tmp / "nope.pdf"), ["en"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("file not found", result["error"])

    def test_thin_html_suggests_the_webfetch_fallback(self):
        with mock.patch.object(fs, "http_get", return_value=b"<html><body><p>hi</p></body></html>"):
            result = fs.ingest_one("https://example.com/spa", ["en"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("WebFetch", result["error"])

    def test_one_failure_does_not_stop_the_other_sources(self):
        note = self.tmp / "note.md"
        note.write_text("# My Note\n\nContent.\n")
        results = [fs.ingest_one(i, ["en"]) for i in [str(note), "archive.zip"]]
        self.assertEqual([r["status"] for r in results], ["ok", "failed"])


class TestLocalText(TempRawDir):
    def test_title_comes_from_the_h1(self):
        note = self.tmp / "seminar.md"
        note.write_text("# Operator Learning Seminar\n\nNotes from today.\n")
        result = fs.ingest_one(str(note), ["en"])
        self.assertEqual(result["title"], "Operator Learning Seminar")
        self.assertEqual(result["path"], "raw/operator-learning-seminar.md")

    def test_title_falls_back_to_the_filename(self):
        note = self.tmp / "scratch-notes.txt"
        note.write_text("no heading here\n")
        result = fs.ingest_one(str(note), ["en"])
        self.assertEqual(result["title"], "scratch-notes")


if __name__ == "__main__":
    unittest.main(verbosity=2)
