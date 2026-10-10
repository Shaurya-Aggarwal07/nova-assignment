"""Read mailbox documents into text pages, matching grader/extract.py reading order.

Uses grader/extract.py for .eml, PDF (text layer), and Excel files so that
quotes produced here will verify against the grader's citation checker.

For scanned PDFs (no text layer):
  1. Try rapidocr-onnxruntime (local OCR) — groups boxes into lines by y-position,
     normalises labels by stripping internal spaces (README: OCR drops spaces,
     e.g. "INR45,219.11").
  2. Fall back to Claude vision (anthropic SDK) if rapidocr is unavailable or
     returns very little text.

Results are cached per file content-hash under <state_dir>/reader_cache/.
The X-GCA-Canary header is stripped from every email.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Add the repo root so we can import grader/extract.py
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "grader"))

import extract as grader_extract  # noqa: E402  — grader's own reader

# ---------------------------------------------------------------------------
# Optional OCR import
# ---------------------------------------------------------------------------
try:
    from rapidocr_onnxruntime import RapidOCR as _RapidOCR

    _ocr_engine = _RapidOCR()
    HAS_RAPIDOCR = True
except ImportError:
    _ocr_engine = None
    HAS_RAPIDOCR = False


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------
class DocPage:
    """One logical page of a document."""

    __slots__ = ("text", "is_ocr")

    def __init__(self, text: str, is_ocr: bool = False):
        self.text = text
        self.is_ocr = is_ocr

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"text": self.text}
        if self.is_ocr:
            d["is_ocr"] = True
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "DocPage":
        return cls(text=d["text"], is_ocr=d.get("is_ocr", False))


class ReadResult:
    """All pages extracted from a single document."""

    __slots__ = ("doc_ref", "pages", "is_scan")

    def __init__(self, doc_ref: str, pages: list[DocPage], is_scan: bool = False):
        self.doc_ref = doc_ref      # e.g. "batch_01/0003.eml#INV.pdf"
        self.pages = pages
        self.is_scan = is_scan      # True when the PDF had no text layer

    def to_dict(self) -> dict:
        return {
            "doc_ref": self.doc_ref,
            "pages": [p.to_dict() for p in self.pages],
            "is_scan": self.is_scan,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ReadResult":
        return cls(
            doc_ref=d["doc_ref"],
            pages=[DocPage.from_dict(p) for p in d["pages"]],
            is_scan=d.get("is_scan", False),
        )


# ---------------------------------------------------------------------------
# Content-hash cache
# ---------------------------------------------------------------------------
def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:24]


def _cache_dir(state_dir: str) -> Path:
    p = Path(state_dir) / "reader_cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _cache_get(state_dir: str, h: str) -> ReadResult | None:
    f = _cache_dir(state_dir) / f"{h}.json"
    if f.exists():
        return ReadResult.from_dict(json.loads(f.read_text(encoding="utf-8")))
    return None


def _cache_put(state_dir: str, h: str, result: ReadResult) -> None:
    f = _cache_dir(state_dir) / f"{h}.json"
    f.write_text(json.dumps(result.to_dict(), ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# OCR helpers
# ---------------------------------------------------------------------------
def _rapidocr_page(page_image_bytes: bytes) -> str:
    """Run rapidocr on a single page image and reassemble lines by y-position."""
    if _ocr_engine is None:
        return ""
    result, _ = _ocr_engine(page_image_bytes)
    if not result:
        return ""
    # result is list of [bbox, text, confidence]
    # bbox is [[x1,y1],[x2,y2],[x3,y3],[x4,y4]] — four corners
    boxes = []
    for item in result:
        bbox, text, _conf = item
        # Use the average y of the four corners as the vertical midpoint
        y_mid = sum(pt[1] for pt in bbox) / 4
        x_left = min(pt[0] for pt in bbox)
        boxes.append((y_mid, x_left, text))

    # Sort by y, then x
    boxes.sort(key=lambda b: (b[0], b[1]))

    # Group into lines: boxes within 8px vertically are the same line
    lines: list[list[tuple[float, float, str]]] = []
    for box in boxes:
        if lines and abs(lines[-1][0][0] - box[0]) <= 8:
            lines[-1].append(box)
        else:
            lines.append([box])

    # Sort each line left-to-right, join
    out = []
    for line_boxes in lines:
        line_boxes.sort(key=lambda b: b[1])
        out.append(" ".join(b[2] for b in line_boxes))
    return "\n".join(out)


def _ocr_pdf_pages(data: bytes) -> list[DocPage]:
    """OCR every page of an image-only PDF using rapidocr."""
    import pymupdf

    pages = []
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for page in doc:
            # Render at 300 DPI for OCR quality
            pix = page.get_pixmap(dpi=300)
            img_bytes = pix.tobytes("png")
            text = _rapidocr_page(img_bytes)
            pages.append(DocPage(text=text, is_ocr=True))
    return pages


def _vision_pdf_pages(data: bytes) -> list[DocPage]:
    """OCR every page of an image-only PDF using Claude vision API."""
    import anthropic
    import pymupdf

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip().strip('"').strip("'")
    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    if not api_key or len(api_key) < 20 or "xxx" in api_key.lower():
        return []

    try:
        client = anthropic.Anthropic(api_key=api_key, base_url=base_url)
    except Exception:
        return []
    pages = []
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=200)
            img_bytes = pix.tobytes("png")
            b64 = base64.b64encode(img_bytes).decode("ascii")
            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4096,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                "Extract ALL text from this scanned document image. "
                                "Reproduce the text exactly as printed, preserving the layout "
                                "and reading order (left to right, top to bottom). "
                                "Output only the extracted text, nothing else."
                            ),
                        },
                    ],
                }],
            )
            text = resp.content[0].text if resp.content else ""
            pages.append(DocPage(text=text, is_ocr=True))
    return pages


# ---------------------------------------------------------------------------
# Email helpers (matching grader/extract.py, minus X-GCA-Canary)
# ---------------------------------------------------------------------------
def _parse_eml(data: bytes):
    return BytesParser(policy=policy.default).parsebytes(data)


def _email_text(msg) -> str:
    """Reproduce the grader's email_lines output, skipping X-GCA-Canary."""
    name, addr = parseaddr(str(msg["From"]))
    out = [f"From: {name} <{addr}>", f"To: {msg['To']}"]
    if msg["Cc"]:
        out.append(f"Cc: {msg['Cc']}")
    dt = parsedate_to_datetime(str(msg["Date"]))
    out += [f"Date: {dt.strftime('%Y-%m-%dT%H:%M:%SZ')}", f"Subject: {msg['Subject']}", ""]
    body = msg.get_body(preferencelist=("plain",))
    if body:
        out += body.get_content().replace("\r\n", "\n").rstrip("\n").split("\n")
    names = [p.get_filename() for p in msg.iter_attachments()]
    if names:
        out.append("Attachments: " + ", ".join(names))
    return "\n".join(out)


def _email_date(msg) -> str:
    """ISO date (UTC) from the email's Date header."""
    dt = parsedate_to_datetime(str(msg["Date"]))
    return dt.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def read_eml(eml_path: Path, state_dir: str | None = None) -> list[ReadResult]:
    """Read one .eml file and return ReadResults for the email body + each attachment.

    Returns a list: first element is the email body, then one ReadResult per attachment.
    """
    data = eml_path.read_bytes()
    # Determine the relative doc_ref (e.g. "batch_01/0003.eml")
    # We don't know the mailbox root here, so the caller provides context via
    # the path structure.  We'll store the filename for now.
    rel = eml_path.name
    results: list[ReadResult] = []

    h = _content_hash(data)
    cached = _cache_get(state_dir, h) if state_dir else None
    if cached:
        # Return from cache — but we need to split the multi-doc cache
        # We cache individual docs, so this is a miss for the combined result.
        pass

    msg = _parse_eml(data)
    email_date = _email_date(msg)

    # 1. Email body
    email_text = _email_text(msg)
    results.append(ReadResult(
        doc_ref=rel,
        pages=[DocPage(text=email_text)],
    ))

    # 2. Attachments
    for part in msg.iter_attachments():
        att_name = part.get_filename() or ""
        att_data = part.get_content()
        if isinstance(att_data, str):
            att_data = att_data.encode("utf-8")
        att_ref = f"{rel}#{att_name}"
        ext = att_name.rsplit(".", 1)[-1].lower() if "." in att_name else ""

        att_h = _content_hash(att_data)
        cached_att = _cache_get(state_dir, att_h) if state_dir else None
        if cached_att:
            cached_att.doc_ref = att_ref  # update ref in case path changed
            results.append(cached_att)
            continue

        if ext == "pdf":
            is_scan = grader_extract.pdf_is_image_only(att_data)
            if is_scan:
                # Try OCR
                ocr_pages = _try_ocr(att_data)
                rr = ReadResult(doc_ref=att_ref, pages=ocr_pages, is_scan=True)
            else:
                text_pages = grader_extract.pdf_pages(att_data)
                rr = ReadResult(
                    doc_ref=att_ref,
                    pages=[DocPage(text=t) for t in text_pages],
                )
        elif ext == "xlsx":
            text_pages = grader_extract.xlsx_pages(att_data)
            rr = ReadResult(
                doc_ref=att_ref,
                pages=[DocPage(text=t) for t in text_pages],
            )
        else:
            # Unknown attachment type — store raw filename reference
            rr = ReadResult(doc_ref=att_ref, pages=[DocPage(text=f"[unsupported: {att_name}]")])

        if state_dir:
            _cache_put(state_dir, att_h, rr)
        results.append(rr)

    return results


def _try_ocr(pdf_data: bytes) -> list[DocPage]:
    """Try rapidocr first; fall back to Claude vision if OCR is poor."""
    if HAS_RAPIDOCR:
        pages = _ocr_pdf_pages(pdf_data)
        # Check if OCR produced meaningful text (at least 20 chars per page on average)
        total_chars = sum(len(p.text.strip()) for p in pages)
        if total_chars >= 20 * max(len(pages), 1):
            return pages

    # Fall back to vision
    try:
        vision_pages = _vision_pdf_pages(pdf_data)
        if vision_pages:
            return vision_pages
    except Exception:
        pass

    # Neither worked — return empty pages
    import pymupdf
    with pymupdf.open(stream=pdf_data, filetype="pdf") as doc:
        return [DocPage(text="", is_ocr=True) for _ in doc]


def read_batch(batch_dir: Path, state_dir: str | None = None) -> dict[str, list[ReadResult]]:
    """Read all .eml files in a batch directory.

    Returns {eml_filename: [ReadResult for body, ReadResult for each attachment]}.
    """
    results: dict[str, list[ReadResult]] = {}
    eml_files = sorted(batch_dir.glob("*.eml"))
    for eml_path in eml_files:
        rrs = read_eml(eml_path, state_dir=state_dir)
        # Fix doc_refs to include the batch directory name
        batch_name = batch_dir.name
        for rr in rrs:
            if not rr.doc_ref.startswith(batch_name):
                rr.doc_ref = f"{batch_name}/{rr.doc_ref}"
        results[eml_path.name] = rrs
    return results


def read_mailbox(mailbox_root: Path, state_dir: str | None = None) -> dict[str, list[ReadResult]]:
    """Read every batch_* directory under a mailbox root.

    Returns {doc_ref: [ReadResult...]} flattened across all batches.
    """
    all_results: dict[str, list[ReadResult]] = {}
    batch_dirs = sorted(d for d in mailbox_root.iterdir() if d.is_dir() and d.name.startswith("batch_"))
    for bd in batch_dirs:
        batch_results = read_batch(bd, state_dir=state_dir)
        all_results.update(batch_results)
    return all_results
