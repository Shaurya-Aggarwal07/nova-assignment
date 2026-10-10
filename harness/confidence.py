"""Deterministic confidence scoring over facts for audit findings.

Per GLOSSARY.md and README.md:
Confidence is a probability between 0.0 and 1.0 that the verdict and amount
are both right, computed deterministically from:
- Evidence completeness
- Source authority
- Clause in force
- Extraction confidence
- Cross-source agreement / disagreement
- Human feedback confirmation
"""
from __future__ import annotations

import re
from typing import Any


def compute_confidence(
    finding: dict[str, Any],
    facts: list[dict[str, Any]] | None = None,
) -> float:
    """Compute a deterministic confidence score (0.0 to 1.0) for an audit finding.

    Adjustments:
    - Base confidence: 0.85
    - Missing evidence: -0.15 if cannot_determine or missing_doc
    - Source authority:
        - -0.02 if from signed contract (level 1)
        - -0.10 if from forwarded / quoted text (level 4)
        - -0.05 for intermediate authority (e.g. standard email)
    - Clause not in force: -0.10 if effective_date is after the relevant date
    - Low extraction confidence: -0.10 if extracted fields have low confidence / OCR
    - Cross-source disagreement: -0.20 if conflict exists for this finding; 0.0 if verdict is conflict
    - Feedback confirmation: +0.10 if confirmed by a human
    - Clamped to [0.0, 1.0]
    """
    if not isinstance(finding, dict):
        return 0.0

    verdict = str(finding.get("verdict") or "").lower()

    # Special case: conflict findings have 0.0 confidence per GLOSSARY
    # ("never pick a winner, let a person decide")
    if verdict == "conflict":
        return 0.0

    score = 0.85

    # 1. Missing evidence (-0.15 if cannot_determine or missing_doc or empty evidence)
    is_missing_evidence = (
        verdict == "cannot_determine"
        or finding.get("missing_doc") is not None
        or finding.get("missing_evidence") is True
        or (finding.get("evidence") is not None and len(finding.get("evidence", [])) == 0)
    )
    if is_missing_evidence:
        score -= 0.15

    # 2. Source authority
    # -0.02 if from signed contract
    # -0.10 if from forwarded / quoted text
    auth = str(finding.get("source_authority") or finding.get("authority") or "").lower()
    is_forwarded = (
        auth in ("forwarded_text", "quoted_text", "forwarded", "quoted", "level_4")
        or finding.get("is_forwarded") is True
        or any(
            ">" in str(ev.get("quote", "")) or "forwarded" in str(ev.get("quote", "")).lower()
            for ev in finding.get("evidence", [])
            if isinstance(ev, dict)
        )
    )
    is_signed_contract = (
        auth in ("signed_contract", "contract", "level_1", "signed")
        or finding.get("is_signed_contract") is True
        or any(
            any(kw in str(ev.get("doc", "")).lower() for kw in ("msa", "contract", "amd", "amendment"))
            for ev in finding.get("evidence", [])
            if isinstance(ev, dict)
        )
    )

    if is_forwarded:
        score -= 0.10
    elif is_signed_contract:
        score -= 0.02
    else:
        score -= 0.05

    # 3. Clause not in force (-0.10 if effective_date is after relevant date)
    not_in_force = (
        finding.get("clause_in_force") is False
        or finding.get("not_in_force") is True
    )
    if not not_in_force:
        eff_date = finding.get("effective_date")
        rel_date = (
            finding.get("pickup_date")
            or finding.get("relevant_date")
            or finding.get("invoice_date")
        )
        if eff_date and rel_date and str(eff_date) > str(rel_date):
            not_in_force = True

    if not_in_force:
        score -= 0.10

    # 4. Low extraction confidence (-0.10 if OCR or ambiguous quote)
    low_extraction = (
        finding.get("extraction_confidence") in ("low", "uncertain")
        or finding.get("extraction_uncertain") is True
        or finding.get("is_ocr") is True
        or finding.get("ocr") is True
        or any(
            "[ocr]" in str(ev.get("quote", "")).lower() or ".xlsx" in str(ev.get("doc", "")).lower() and "unclear" in str(ev.get("quote", "")).lower()
            for ev in finding.get("evidence", [])
            if isinstance(ev, dict)
        )
    )
    if low_extraction:
        score -= 0.10

    # 5. Cross-source disagreement (-0.20 if a conflict flag exists)
    has_disagreement = (
        finding.get("has_conflict") is True
        or finding.get("disagreement") is True
        or finding.get("amount_range") is not None
    )
    if has_disagreement:
        score -= 0.20

    # 6. Feedback confirmation (+0.10 if confirmed)
    is_confirmed = (
        finding.get("confirmed") is True
        or finding.get("feedback") == "confirmed"
        or finding.get("feedback_answer") == "confirmed"
        or finding.get("learned_at") is not None
    )
    if is_confirmed:
        score += 0.10

    # Clamp to [0.0, 1.0]
    final_score = max(0.0, min(1.0, score))
    return round(final_score, 4)
