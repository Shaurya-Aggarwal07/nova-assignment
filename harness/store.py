"""Storage layer for facts and feedback under --state-dir.

State lives only under --state-dir.
- Facts are stored per-document as JSON in <state_dir>/facts/<doc_ref_safe>.json
- Feedback is stored as JSON in <state_dir>/feedback/<filename>.json
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def sanitize_doc_ref(doc_ref: str) -> str:
    """Convert a doc_ref into a safe filename."""
    return doc_ref.replace("/", "__").replace("#", "__").replace("\\", "__")


def get_facts_dir(state_dir: str | Path) -> Path:
    p = Path(state_dir) / "facts"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_feedback_dir(state_dir: str | Path) -> Path:
    p = Path(state_dir) / "feedback"
    p.mkdir(parents=True, exist_ok=True)
    return p


def save_fact_file(state_dir: str | Path, doc_ref: str, facts_dict: dict[str, Any]) -> Path:
    """Save extracted facts for one document."""
    facts_dir = get_facts_dir(state_dir)
    safe = sanitize_doc_ref(doc_ref)
    target = facts_dir / f"{safe}.json"
    target.write_text(json.dumps(facts_dict, ensure_ascii=False, indent=1), encoding="utf-8")
    return target


def fact_file_exists(state_dir: str | Path, doc_ref: str) -> bool:
    """Check if facts for doc_ref already exist on disk."""
    facts_dir = Path(state_dir) / "facts"
    safe = sanitize_doc_ref(doc_ref)
    return (facts_dir / f"{safe}.json").exists()


def _normalize(fact: Any, doc_ref: str) -> dict[str, Any] | None:
    try:
        from harness.extract import normalize_fact
        return normalize_fact(fact, doc_ref)
    except Exception:
        try:
            from extract import normalize_fact
            return normalize_fact(fact, doc_ref)
        except Exception:
            return fact if isinstance(fact, dict) else None


def load_all_facts(state_dir: str | Path) -> list[dict[str, Any]]:
    """Load ALL individual facts across all document files in <state_dir>/facts/."""
    facts_dir = Path(state_dir) / "facts"
    if not facts_dir.is_dir():
        return []
    all_facts: list[dict[str, Any]] = []
    for fp in sorted(facts_dir.glob("*.json")):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "facts" in data and isinstance(data["facts"], list):
                for f in data["facts"]:
                    nf = _normalize(f, fp.stem)
                    if nf is not None:
                        all_facts.append(nf)
        except Exception:
            pass
    return all_facts


def load_facts_by_doc(state_dir: str | Path) -> dict[str, list[dict[str, Any]]]:
    """Load facts grouped by document reference."""
    facts_dir = Path(state_dir) / "facts"
    if not facts_dir.is_dir():
        return {}
    by_doc: dict[str, list[dict[str, Any]]] = {}
    for fp in sorted(facts_dir.glob("*.json")):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "facts" in data and isinstance(data["facts"], list):
                for f in data["facts"]:
                    nf = _normalize(f, fp.stem)
                    if nf is not None:
                        dr = nf.get("source", {}).get("doc_ref", fp.stem)
                        by_doc.setdefault(dr, []).append(nf)
        except Exception:
            pass
    return by_doc


def save_feedback(state_dir: str | Path, feedback_file: str | Path) -> Path:
    """Store feedback file under <state_dir>/feedback/."""
    src = Path(feedback_file)
    if not src.is_file():
        raise FileNotFoundError(f"Feedback file not found: {feedback_file}")
    fb_dir = get_feedback_dir(state_dir)
    dest = fb_dir / src.name
    data = json.loads(src.read_text(encoding="utf-8"))
    dest.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return dest


def load_all_feedback(state_dir: str | Path) -> list[dict[str, Any]]:
    """Load all feedback files stored under <state_dir>/feedback/."""
    fb_dir = Path(state_dir) / "feedback"
    if not fb_dir.is_dir():
        return []
    all_fb: list[dict[str, Any]] = []
    for fp in sorted(fb_dir.glob("*.json")):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            all_fb.append(data)
        except Exception:
            pass
    return all_fb
