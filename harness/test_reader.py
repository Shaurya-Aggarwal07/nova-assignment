#!/usr/bin/env python
"""Smoke-test harness/reader.py across all data directories.

Runs the reader over data/scenarios, data/exam, and data/examples and reports:
  - Total file count (emails + attachments)
  - Files with empty text
  - Files that used OCR

Usage:
    python harness/test_reader.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# Ensure repo root is on path
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "harness"))

from reader import read_batch, read_eml, HAS_RAPIDOCR  # noqa: E402


def scan_mailbox(root: Path, state_dir: str, label: str) -> dict:
    """Scan a mailbox directory (with batch_* subdirs) and return stats."""
    stats = {"total": 0, "empty": [], "ocr": [], "scans": []}
    batch_dirs = sorted(d for d in root.iterdir() if d.is_dir() and d.name.startswith("batch_"))
    for bd in batch_dirs:
        batch_results = read_batch(bd, state_dir=state_dir)
        for eml_name, rrs in batch_results.items():
            for rr in rrs:
                stats["total"] += 1
                all_text = "\n".join(p.text for p in rr.pages).strip()
                if not all_text:
                    stats["empty"].append(rr.doc_ref)
                if rr.is_scan:
                    stats["scans"].append(rr.doc_ref)
                if any(p.is_ocr for p in rr.pages):
                    stats["ocr"].append(rr.doc_ref)
    return stats


def scan_examples(root: Path, state_dir: str) -> dict:
    """Scan data/examples/<name>/docs/ directories."""
    stats = {"total": 0, "empty": [], "ocr": [], "scans": []}
    for example_dir in sorted(root.iterdir()):
        if not example_dir.is_dir():
            continue
        docs_dir = example_dir / "docs"
        if not docs_dir.exists():
            continue
        # Examples have batch_* subdirectories under docs/
        batch_dirs = sorted(d for d in docs_dir.iterdir() if d.is_dir() and d.name.startswith("batch_"))
        for bd in batch_dirs:
            batch_results = read_batch(bd, state_dir=state_dir)
            for eml_name, rrs in batch_results.items():
                for rr in rrs:
                    stats["total"] += 1
                    all_text = "\n".join(p.text for p in rr.pages).strip()
                    if not all_text:
                        stats["empty"].append(f"{example_dir.name}/{rr.doc_ref}")
                    if rr.is_scan:
                        stats["scans"].append(f"{example_dir.name}/{rr.doc_ref}")
                    if any(p.is_ocr for p in rr.pages):
                        stats["ocr"].append(f"{example_dir.name}/{rr.doc_ref}")
    return stats


def print_stats(label: str, stats: dict):
    print(f"\n{'=' * 60}")
    print(f"  {label}")
    print(f"{'=' * 60}")
    print(f"  Total documents:       {stats['total']}")
    print(f"  Empty text:            {len(stats['empty'])}")
    for f in stats["empty"]:
        print(f"    - {f}")
    print(f"  Scanned (no text):     {len(stats['scans'])}")
    for f in stats["scans"]:
        print(f"    - {f}")
    print(f"  Used OCR:              {len(stats['ocr'])}")
    for f in stats["ocr"]:
        print(f"    - {f}")


def main():
    data_root = _REPO_ROOT / "data"
    state_dir = str(_REPO_ROOT / ".state" / "reader_test")

    print(f"rapidocr available: {HAS_RAPIDOCR}")
    print(f"Cache dir: {state_dir}")

    t0 = time.time()

    # 1. data/scenarios
    scenarios_root = data_root / "scenarios"
    if scenarios_root.exists():
        s = scan_mailbox(scenarios_root, state_dir, "data/scenarios")
        print_stats("data/scenarios", s)

    # 2. data/exam
    exam_root = data_root / "exam"
    if exam_root.exists():
        s = scan_mailbox(exam_root, state_dir, "data/exam")
        print_stats("data/exam", s)

    # 3. data/examples
    examples_root = data_root / "examples"
    if examples_root.exists():
        s = scan_examples(examples_root, state_dir)
        print_stats("data/examples (all)", s)

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
