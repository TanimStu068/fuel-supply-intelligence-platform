#!/usr/bin/env python3
"""Build four evidence PDFs from the repository's Markdown sources."""

from __future__ import annotations

import base64
from collections import Counter
import hashlib
import html
import mimetypes
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urlsplit

from PIL import Image

try:
    from markdown_it import MarkdownIt
    from markdown_it.token import Token
except ImportError as exc:
    raise SystemExit(
        "Missing Python package markdown-it-py. Install it with "
        "`python -m pip install markdown-it-py`."
    ) from exc


ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = ROOT / "docs"
REPO_URL = "https://github.com/TanimStu068/fuel-supply-intelligence-platform"
MAX_PDF_BYTES = 15 * 1024 * 1024


@dataclass
class SourceDocument:
    path: Path
    tokens: list[Token]
    title: str
    doc_id: str
    headings: list[dict[str, str | int]] = field(default_factory=list)
    heading_ids: dict[str, str] = field(default_factory=dict)
    mermaid_count: int = 0


PDF_SPECS: list[tuple[str, str, list[Path]]] = [
    (
        "01-judging-evidence.pdf",
        "Judging Evidence",
        [
            Path("docs/bug-catalog.md"),
            Path("docs/postmortems/README.md"),
            *sorted(
                path
                for path in (ROOT / "docs/postmortems").glob("*.md")
                if path.name != "README.md"
            ),
            Path("docs/requirements-traceability.md"),
            Path("docs/project-summary.md"),
            Path("docs/demo-qa.md"),
        ],
    ),
    (
        "02-technical-reference.pdf",
        "Technical Reference",
        [
            Path("docs/adr/README.md"),
            *sorted((ROOT / "docs/adr").glob("[0-9][0-9][0-9][0-9]-*.md")),
            Path("docs/intelligence-methodology.md"),
            Path("docs/decision-engine.md"),
            Path("docs/api-reference.md"),
            Path("docs/data-model.md"),
        ],
    ),
    (
        "03-operations-and-quality.pdf",
        "Operations and Quality",
        [
            Path("docs/runbook.md"),
            Path("docs/observability-guide.md"),
            Path("docs/security-and-threat-model.md"),
            Path("docs/testing-strategy-and-report.md"),
            Path("docs/cicd-release-and-rollback.md"),
            Path("docs/configuration-reference.md"),
        ],
    ),
    (
        "04-story-and-repo-hygiene.pdf",
        "Story and Repo Hygiene",
        [
            Path("docs/known-limitations-and-roadmap.md"),
            Path("docs/operator-user-guide.md"),
            Path("docs/project-journey.md"),
            Path("CHANGELOG.md"),
            Path("CONTRIBUTING.md"),
            Path("SECURITY.md"),
            Path("docs/glossary.md"),
        ],
    ),
]


def plain_text(children: list[Token] | None) -> str:
    if not children:
        return ""
    pieces: list[str] = []
    for token in children:
        if token.type in {"text", "code_inline", "html_inline"}:
            pieces.append(token.content)
        elif token.type in {"softbreak", "hardbreak"}:
            pieces.append(" ")
        elif token.type == "image":
            pieces.append(token.attrGet("alt") or "")
    return re.sub(r"\s+", " ", html.unescape("".join(pieces))).strip()


def heading_slug(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).lower().replace(" ", "-")
    return re.sub(r"[^\w-]", "", value, flags=re.UNICODE)


def source_documents(paths: list[Path]) -> tuple[list[SourceDocument], list[str]]:
    documents: list[SourceDocument] = []
    missing: list[str] = []
    parser = MarkdownIt("commonmark", {"html": True}).enable(["table", "strikethrough"])

    for path in paths:
        absolute_path = path if path.is_absolute() else ROOT / path
        if not absolute_path.is_file():
            missing.append(path.as_posix())
            continue

        relative_path = absolute_path.resolve().relative_to(ROOT)
        source = absolute_path.read_text(encoding="utf-8")
        tokens = parser.parse(source)
        path_hash = hashlib.sha1(relative_path.as_posix().encode()).hexdigest()[:10]
        doc_id = f"doc-{path_hash}"
        headings: list[dict[str, str | int]] = []
        heading_ids: dict[str, str] = {}
        duplicate_counts: dict[str, int] = {}

        for index, token in enumerate(tokens):
            if token.type != "heading_open" or index + 1 >= len(tokens):
                continue
            inline = tokens[index + 1]
            title = plain_text(inline.children)
            level = int(token.tag[1:])
            slug = heading_slug(title) or "section"
            duplicate_counts[slug] = duplicate_counts.get(slug, 0) + 1
            if duplicate_counts[slug] > 1:
                slug = f"{slug}-{duplicate_counts[slug] - 1}"
            anchor = f"{doc_id}-h{len(headings) + 1}"
            token.attrSet("id", anchor)
            heading = {"id": anchor, "level": level, "text": title}
            headings.append(heading)
            heading_ids[slug] = anchor
            heading_ids[title.lower()] = anchor

        for index, token in enumerate(tokens):
            if token.type != "table_open":
                continue
            column_count = 0
            for candidate in tokens[index + 1 :]:
                if candidate.type in {"tbody_open", "table_close"}:
                    break
                if candidate.type == "th_open":
                    column_count += 1
            if column_count > 5:
                token.attrJoin("class", "wide-table")

        first_title = next(
            (str(heading["text"]) for heading in headings if heading["level"] == 1),
            relative_path.name,
        )
        documents.append(
            SourceDocument(
                relative_path,
                tokens,
                first_title,
                doc_id,
                headings,
                heading_ids,
            )
        )

    return documents, missing


def resolve_link(
    source: SourceDocument,
    href: str,
    documents_by_path: dict[Path, SourceDocument],
    missing_links: list[str],
) -> tuple[str | None, str | None]:
    """Return an internal href or a filename suffix for non-internal links."""
    parsed = urlsplit(href)
    if parsed.scheme or href.startswith("//") or href.startswith("data:"):
        return href, None

    target_path = unquote(parsed.path)
    fragment = unquote(parsed.fragment)
    if target_path:
        resolved = (ROOT / source.path.parent / target_path).resolve()
        destination = documents_by_path.get(resolved)
        if destination is None:
            if not resolved.exists():
                missing_links.append(f"{source.path}: {href}")
            return None, f" (see {Path(target_path).name or target_path})"
    else:
        destination = source

    if fragment:
        fragment_key = fragment.lower()
        anchor = destination.heading_ids.get(fragment_key)
        if anchor is None:
            anchor = destination.heading_ids.get(heading_slug(fragment))
        if anchor is None:
            missing_links.append(f"{source.path}: unresolved anchor {href}")
            return None, f" (see {Path(target_path).name or destination.path.name})"
        return f"#{anchor}", None

    return f"#{destination.doc_id}", None


def prepare_tokens(
    documents: list[SourceDocument],
    missing_links: list[str],
    missing_images: list[str],
) -> None:
    documents_by_path = {doc.path.resolve(): doc for doc in documents}

    for doc in documents:
        for token in doc.tokens:
            if token.type == "fence" and token.info.strip().lower() == "mermaid":
                doc.mermaid_count += 1
            if token.type != "inline" or not token.children:
                continue

            children = token.children
            index = 0
            while index < len(children):
                child = children[index]
                if child.type == "image":
                    src = child.attrGet("src") or ""
                    if src.startswith(("http://", "https://", "data:")):
                        index += 1
                        continue
                    image_path = (ROOT / doc.path.parent / unquote(src)).resolve()
                    if not image_path.is_file():
                        missing_images.append(f"{doc.path}: {src}")
                        child.type = "html_inline"
                        child.tag = ""
                        child.content = (
                            '<div class="missing-image">Missing image skipped: '
                            f"{html.escape(src)}</div>"
                        )
                        child.attrs = None
                        index += 1
                        continue
                    mime_type = mimetypes.guess_type(image_path.name)[0] or "image/png"
                    alt_text = child.content
                    with Image.open(image_path) as image:
                        width, height = image.size
                        if height / width > 1.25:
                            slice_height = int(width * 1.18)
                            slice_step = slice_height - min(80, slice_height // 5)
                            slices: list[str] = []
                            for top in range(0, height, slice_step):
                                crop = image.crop(
                                    (0, top, width, min(top + slice_height, height))
                                )
                                buffer = BytesIO()
                                crop.save(buffer, format="PNG", optimize=True)
                                encoded = base64.b64encode(buffer.getvalue()).decode(
                                    "ascii"
                                )
                                slices.append(
                                    '<span class="image-slice"><img alt="'
                                    f'{html.escape(alt_text)}" '
                                    f'src="data:image/png;base64,{encoded}"></span>'
                                )
                            child.type = "html_inline"
                            child.tag = ""
                            child.content = "".join(slices)
                            child.attrs = None
                            index += 1
                            continue
                    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
                    child.attrSet("alt", alt_text)
                    child.attrSet("src", f"data:{mime_type};base64,{encoded}")
                    index += 1
                    continue

                if child.type != "link_open":
                    index += 1
                    continue

                href = child.attrGet("href") or ""
                internal_href, suffix = resolve_link(
                    doc, href, documents_by_path, missing_links
                )
                if internal_href is not None:
                    child.attrSet("href", internal_href)
                    index += 1
                    continue

                child.tag = "span"
                child.attrSet("class", "plain-link")
                child.attrs.pop("href", None)
                close_index = index + 1
                while close_index < len(children) and children[close_index].type != "link_close":
                    close_index += 1
                if close_index < len(children):
                    children[close_index].tag = "span"
                    if suffix:
                        suffix_token = Token("text", "", 0)
                        suffix_token.content = suffix
                        children.insert(close_index + 1, suffix_token)
                index = close_index + 2


def render_mermaid_source(tokens: list[Token], index: int, *_args: object) -> str:
    code = html.escape(tokens[index].content.rstrip("\n"))
    return (
        '<div class="mermaid-source"><p>Mermaid diagram source '
        "(renderer not installed)</p>"
        f"<pre><code>{code}</code></pre></div>\n"
    )


def toc_rows(
    documents: list[SourceDocument],
    page_numbers: dict[str, int],
) -> str:
    rows: list[str] = []
    for doc in documents:
        main_heading = next(
            (heading for heading in doc.headings if heading["level"] == 1),
            None,
        )
        title_anchor = str(main_heading["id"]) if main_heading else doc.doc_id
        title_page = page_numbers.get(title_anchor, 0)
        rows.append(
            '<div class="toc-row toc-document">'
            f'<a href="#{title_anchor}">{html.escape(doc.title)}</a>'
            f'<span class="toc-path">{html.escape(doc.path.as_posix())}</span>'
            f'<span class="toc-page">{title_page:03d}</span></div>'
        )
        for heading in doc.headings:
            if heading["level"] not in (1, 2) or heading is main_heading:
                continue
            anchor = str(heading["id"])
            page = page_numbers.get(anchor, 0)
            row_class = "toc-major" if heading["level"] == 1 else "toc-section"
            rows.append(
                f'<div class="toc-row {row_class}">'
                f'<a href="#{anchor}">{html.escape(str(heading["text"]))}</a>'
                f'<span class="toc-page">{page:03d}</span></div>'
            )
    return "\n".join(rows)


def render_html(
    title: str,
    documents: list[SourceDocument],
    git_describe: str,
    page_numbers: dict[str, int],
) -> str:
    parser = MarkdownIt("commonmark", {"html": True}).enable(["table", "strikethrough"])
    default_fence_renderer = parser.renderer.rules["fence"]
    if default_fence_renderer is None:
        raise RuntimeError("Markdown-It has no default fenced-code renderer.")

    def render_fence(
        tokens: list[Token], index: int, options: object, env: object
    ) -> str:
        if tokens[index].info.strip().lower() == "mermaid":
            return render_mermaid_source(tokens, index)
        return default_fence_renderer(tokens, index, options, env)

    parser.renderer.rules["fence"] = render_fence
    body: list[str] = []

    for doc in documents:
        body.append(
            f'<section class="source-document" id="{html.escape(doc.doc_id)}">'
        )
        body.append(parser.renderer.render(doc.tokens, parser.options, {}))
        body.append("</section>")

    cover = f"""
    <section class="cover">
      <h1>Fuel Supply Intelligence &amp; Resilience Platform</h1>
      <h2>{html.escape(title)}</h2>
      <p class="event">BUP CSE Fest 2026 Hackathon Finals</p>
      <p><a href="{REPO_URL}">{REPO_URL}</a></p>
      <p>Date: {date.today().strftime("%B %d, %Y")}</p>
      <p>Git tag/commit: {html.escape(git_describe)}</p>
    </section>
    """
    contents = toc_rows(documents, page_numbers)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
@page {{
  size: A4;
  margin: 18mm 16mm 18mm;
  @top-center {{
    content: "Fuel Supply Intelligence & Resilience Platform — {title}";
    font: 8pt Arial, sans-serif;
    color: #536273;
  }}

  @bottom-center {{
    content: "Page " counter(page);
    font: 8pt Arial, sans-serif;
    color: #536273;
  }}
}}
@page:first {{
  @top-center {{ content: none; }}
}}
* {{ box-sizing: border-box; }}
body {{
  font-family: Arial, "Liberation Sans", sans-serif;
  font-size: 10.2pt;
  line-height: 1.42;
  color: #17212b;
  overflow-wrap: anywhere;
}}
a {{ color: #155e9a; text-decoration: none; }}
.cover {{
  min-height: 258mm;
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
  text-align: center;
  break-after: page;
  page-break-after: always;
}}
.cover h1 {{ max-width: 165mm; font-size: 27pt; line-height: 1.2; margin: 0 0 16mm; }}
.cover h2 {{ font-size: 19pt; margin: 0 0 12mm; color: #17466c; }}
.cover p {{ margin: 3mm 0; }}
.cover .event {{ font-size: 12pt; font-weight: bold; }}
.contents {{ break-after: page; page-break-after: always; }}
.contents h1 {{ font-size: 21pt; margin: 0 0 6mm; }}
.toc-row {{
  display: flex;
  align-items: baseline;
  gap: 2mm;
  min-height: 5mm;
  font-size: 9pt;
  line-height: 1.25;
}}
.toc-row a {{ flex: 1 1 auto; }}
.toc-document {{ margin-top: 2mm; font-weight: bold; }}
.toc-document .toc-path {{ color: #5b6875; font-size: 7.7pt; font-weight: normal; }}
.toc-major {{ padding-left: 6mm; }}
.toc-section {{ padding-left: 10mm; }}
.toc-page {{ flex: 0 0 10mm; text-align: right; font-variant-numeric: tabular-nums; }}
.source-document {{ break-before: page; page-break-before: always; }}
h1 {{ font-size: 20pt; line-height: 1.2; margin: 0 0 8mm; color: #133a59; }}
h2 {{ font-size: 15pt; line-height: 1.25; margin: 8mm 0 4mm; color: #17466c; }}
h3 {{ font-size: 12pt; margin: 6mm 0 3mm; color: #24577d; }}
h4, h5, h6 {{ font-size: 10.5pt; margin: 5mm 0 2mm; }}
p, ul, ol, blockquote {{ margin-top: 2.5mm; margin-bottom: 3mm; }}
ul, ol {{ padding-left: 7mm; }}
li {{ margin: 1mm 0; }}
blockquote {{
  margin-left: 0;
  padding: 2mm 4mm;
  border-left: 2px solid #9aafc0;
  color: #35485a;
}}
table {{
  border-collapse: collapse;
  width: 100%;
  table-layout: fixed;
  margin: 4mm 0 5mm;
  font-size: 8.1pt;
  line-height: 1.25;
}}
thead {{ display: table-header-group; }}
tr {{ break-inside: avoid; page-break-inside: avoid; }}
th, td {{
  border: 0.5pt solid #b9c5cf;
  padding: 2mm 1.6mm;
  vertical-align: top;
  overflow-wrap: anywhere;
  word-break: normal;
}}
th {{ background: #eaf0f5; text-align: left; }}
table.wide-table {{ font-size: 7.2pt; }}
table.wide-table th, table.wide-table td {{ padding: 1.2mm 0.7mm; }}
pre {{
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
  font: 7.8pt/1.3 "Liberation Mono", monospace;
  background: #f3f5f7;
  border: 0.5pt solid #d5dce2;
  padding: 3mm;
  margin: 3mm 0 4mm;
}}
code {{
  font: 8.2pt "Liberation Mono", monospace;
  overflow-wrap: anywhere;
  word-break: break-word;
}}
pre code {{ font-size: inherit; }}
hr {{ border: 0; border-top: 0.7pt solid #c5ced6; margin: 7mm 0; }}
img {{
  display: block;
  max-width: 100%;
  max-height: 220mm;
  height: auto;
  object-fit: contain;
  margin: 4mm auto;
  break-inside: avoid;
  page-break-inside: avoid;
}}
.image-slice {{
  display: block;
  text-align: center;
  break-before: page;
  page-break-before: always;
}}
.image-slice img {{
  width: 100%;
  max-width: 100%;
  max-height: 220mm;
}}
.missing-image {{
  border: 1pt dashed #9a4c42;
  padding: 4mm;
  color: #84372e;
  background: #fff3f1;
}}
.mermaid-source {{
  margin: 4mm 0;
  padding: 2mm;
  border-left: 2px solid #7893a8;
  background: #f7f9fa;
}}
.mermaid-source p {{ margin: 1mm 0; font-size: 8pt; color: #536273; }}
.plain-link {{ color: inherit; }}
</style>
</head>
<body>
{cover}
<section class="contents">
  <h1>Contents</h1>
  {contents}
</section>
{''.join(body)}
</body>
</html>"""


def print_pdf(html_path: Path, pdf_path: Path, temp_dir: Path) -> None:
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if not chrome:
        raise RuntimeError("Chrome/Chromium is required to print the PDFs.")
    profile = temp_dir / "chrome-profile"
    command = [
        chrome,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--disable-background-networking",
        "--no-first-run",
        "--no-pdf-header-footer",
        f"--user-data-dir={profile}",
        f"--print-to-pdf={pdf_path}",
        html_path.as_uri(),
    ]
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode or not pdf_path.is_file():
        raise RuntimeError(
            f"Chrome failed to create {pdf_path.name}:\n"
            f"{result.stderr[-3000:]}\n{result.stdout[-1000:]}"
        )


def extract_pages(pdf_path: Path) -> list[str]:
    result = subprocess.run(
        ["pdftotext", "-layout", str(pdf_path), "-"],
        check=True,
        capture_output=True,
        text=True,
    )
    pages = result.stdout.split("\f")
    while pages and not pages[-1].strip():
        pages.pop()
    return pages


def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).lower()
    value = re.sub(r"\s*-\s*", "-", value)
    return re.sub(r"\s+", " ", value).strip()


def page_map_from_pdf(
    pdf_path: Path,
    documents: list[SourceDocument],
) -> dict[str, int]:
    result = subprocess.run(
        ["pdfinfo", "-dests", str(pdf_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    result_by_id: dict[str, int] = {}
    for line in result.stdout.splitlines():
        match = re.match(r'\s*(\d+)\s+\[.*\]\s+"([^"]+)"\s*$', line)
        if match:
            result_by_id[match.group(2)] = int(match.group(1))

    required_ids = {
        str(heading["id"])
        for doc in documents
        for heading in doc.headings
        if heading["level"] in (1, 2)
    }
    required_ids.update(
        str(next(
            (heading["id"] for heading in doc.headings if heading["level"] == 1),
            doc.doc_id,
        ))
        for doc in documents
    )
    missing = sorted(required_ids - result_by_id.keys())
    if missing:
        raise RuntimeError(
            f"PDF named destinations missing for {pdf_path.name}: {missing[:8]}"
        )
    result = {key: result_by_id[key] for key in required_ids}
    return result


def validate_toc_text(
    pages: list[str],
    documents: list[SourceDocument],
    page_numbers: dict[str, int],
) -> None:
    first_doc = documents[0]
    first_heading = next(
        (heading for heading in first_doc.headings if heading["level"] == 1),
        None,
    )
    first_anchor = str(first_heading["id"]) if first_heading else first_doc.doc_id
    first_content_page = page_numbers[first_anchor]
    toc_text = normalized(" ".join(pages[1 : first_content_page - 1]))
    expected = Counter(f"{page:03d}" for page in page_numbers.values())
    actual = Counter(re.findall(r"(?<!\d)\d{3}(?!\d)", toc_text))
    missing = {
        number: count - actual[number]
        for number, count in expected.items()
        if actual[number] < count
    }
    if missing:
        raise RuntimeError(f"TOC page-number labels missing from PDF: {missing}")


def validate_pdf(
    pdf_path: Path,
    documents: list[SourceDocument],
    expected_pages: dict[str, int],
) -> tuple[int, int, list[str]]:
    pages = extract_pages(pdf_path)
    full_text = "\n".join(pages)
    normalized_text = normalized(full_text)
    missing_titles = [
        doc.path.as_posix()
        for doc in documents
        if normalized(doc.title) not in normalized_text
    ]
    if missing_titles:
        raise RuntimeError(
            f"Source document title missing from {pdf_path.name}: {missing_titles}"
        )

    actual_pages = page_map_from_pdf(pdf_path, documents)
    validate_toc_text(pages, documents, actual_pages)
    mismatches = {
        anchor: (expected_pages.get(anchor), page)
        for anchor, page in actual_pages.items()
        if expected_pages.get(anchor) != page
    }
    if mismatches:
        raise RuntimeError(
            f"Table of contents page numbers changed in {pdf_path.name}: "
            f"{list(mismatches.items())[:8]}"
        )

    raw_patterns = [
        r"```",
        r"^\s*\|(?:\s*:?-{3,}:?\s*\|)+\s*$",
        r"\[[^\]\n]+\]\([^) \n]+\)",
        r"\bundefined\b",
    ]
    issues = [
        pattern
        for pattern in raw_patterns
        if re.search(pattern, full_text, re.IGNORECASE | re.MULTILINE)
    ]
    size = pdf_path.stat().st_size
    if size >= MAX_PDF_BYTES:
        raise RuntimeError(
            f"{pdf_path.name} is {size / 1024 / 1024:.2f} MiB; maximum is 15 MiB."
        )
    return len(pages), size, issues


def build_pdf(filename: str, title: str, paths: list[Path], git_describe: str) -> None:
    documents, missing_sources = source_documents(paths)
    if missing_sources:
        print(f"Missing sources for {filename} (skipped):")
        for path in missing_sources:
            print(f"  - {path}")
    if not documents:
        print(f"No available sources for {filename}; skipped.")
        return

    missing_links: list[str] = []
    missing_images: list[str] = []
    prepare_tokens(documents, missing_links, missing_images)

    mermaid_documents = [
        (doc.path.as_posix(), doc.mermaid_count)
        for doc in documents
        if doc.mermaid_count
    ]
    if mermaid_documents:
        print(f"Mermaid blocks kept as readable code listings in {filename}:")
        for path, count in mermaid_documents:
            print(f"  - {path}: {count}")
    for message in missing_images:
        print(f"Missing image (visible note inserted): {message}")
    for message in missing_links:
        print(f"Unresolved relative link/anchor (plain text used): {message}")

    OUTPUTS.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUTS / filename
    page_numbers: dict[str, int] = {}

    with tempfile.TemporaryDirectory(prefix=f"pdf-{output_path.stem}-") as temp_name:
        temp_dir = Path(temp_name)
        html_path = temp_dir / "document.html"
        draft_path = temp_dir / "draft.pdf"
        final_path = temp_dir / "final.pdf"

        html_path.write_text(
            render_html(title, documents, git_describe, page_numbers),
            encoding="utf-8",
        )
        print_pdf(html_path, draft_path, temp_dir)
        page_numbers = page_map_from_pdf(draft_path, documents)

        for attempt in range(3):
            html_path.write_text(
                render_html(title, documents, git_describe, page_numbers),
                encoding="utf-8",
            )
            print_pdf(html_path, final_path, temp_dir)
            actual_pages = page_map_from_pdf(final_path, documents)
            if actual_pages == page_numbers:
                break
            page_numbers = actual_pages
        else:
            raise RuntimeError(f"TOC pagination did not stabilize for {filename}.")

        final_pages, size, issues = validate_pdf(
            final_path, documents, page_numbers
        )
        shutil.move(final_path, output_path)

    print(
        f"Built {output_path.relative_to(ROOT)}: {final_pages} pages, "
        f"{size / 1024 / 1024:.2f} MiB"
    )
    if issues:
        print(
            f"Raw-markdown validation patterns found in {filename}: "
            f"{', '.join(issues)}"
        )
    if missing_sources:
        print(f"Missing source count for {filename}: {len(missing_sources)}")


def main() -> int:
    required_tools = ["pdftotext", "pdfinfo"]
    missing_tools = [name for name in required_tools if not shutil.which(name)]
    if missing_tools:
        print(f"Missing required tools: {', '.join(missing_tools)}", file=sys.stderr)
        return 2

    result = subprocess.run(
        ["git", "describe", "--tags", "--always"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    git_describe = result.stdout.strip()

    for filename, title, paths in PDF_SPECS:
        print(f"\n== {title} ==")
        build_pdf(filename, title, paths, git_describe)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
