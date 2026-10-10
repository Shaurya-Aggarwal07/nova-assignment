"""Unit tests for harness/confidence.py."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "harness"))
sys.path.insert(0, str(REPO_ROOT))

from confidence import compute_confidence


def test_high_confidence():
    """High-confidence finding (signed contract, complete evidence, no conflict, in force):

    assert confidence >= 0.75.
    """
    finding = {
        "id": "overcharge:TX-US-2035-0001",
        "class": "overcharge",
        "verdict": "finding",
        "amount_usd": "120.00",
        "source_authority": "signed_contract",
        "clause_in_force": True,
        "extraction_confidence": "high",
        "evidence": [
            {
                "doc": "batch_01/0001.eml#MSA.pdf",
                "page": 3,
                "quote": "Agreed rate USE1-USW2 200 kg is 4.77 USD per kg",
            },
            {
                "doc": "batch_01/0002.eml#TX-US-2035-0001.pdf",
                "page": 1,
                "quote": "Air freight USE1-USW2 200 kg 5.50 USD billed 1100.00",
            },
        ],
    }
    conf = compute_confidence(finding)
    assert conf >= 0.75, f"Expected confidence >= 0.75, got {conf}"
    print(f"PASS: test_high_confidence passed (confidence={conf})")


def test_low_confidence():
    """Low-confidence finding (forwarded email, missing amendment, extraction uncertain):

    assert confidence <= 0.50.
    """
    finding = {
        "id": "overcharge:TX-US-2035-0002",
        "class": "overcharge",
        "verdict": "cannot_determine",
        "source_authority": "forwarded_text",
        "missing_doc": {"name": "Annex A rev 2", "holder": {"party": "TBX"}},
        "extraction_confidence": "low",
        "evidence": [],
    }
    conf = compute_confidence(finding)
    assert conf <= 0.50, f"Expected confidence <= 0.50, got {conf}"
    print(f"PASS: test_low_confidence passed (confidence={conf})")


def test_conflict_scenario():
    """Conflict scenario:

    assert confidence close to 0.0.
    """
    finding = {
        "id": "overcharge:TX-US-2035-0003",
        "class": "overcharge",
        "verdict": "conflict",
        "amount_range": ["20.00", "50.00"],
        "evidence": [
            {"doc": "doc1.pdf", "page": 1, "quote": "Rate is 5.00 USD per kg"},
            {"doc": "doc2.pdf", "page": 1, "quote": "Rate notice rate is 5.25 USD per kg"},
        ],
    }
    conf = compute_confidence(finding)
    assert conf == 0.0 or abs(conf - 0.0) < 0.05, f"Expected confidence close to 0.0, got {conf}"
    print(f"PASS: test_conflict_scenario passed (confidence={conf})")


def test_feedback_confirmation():
    """Feedback confirmation: +0.10 if confirmed."""
    base_finding = {
        "id": "overcharge:TX-US-2035-0004",
        "class": "overcharge",
        "verdict": "finding",
        "amount_usd": "50.00",
        "source_authority": "signed_contract",
        "evidence": [{"doc": "contract.pdf", "page": 1, "quote": "Valid quote 123456"}],
    }
    c_unconfirmed = compute_confidence(base_finding)

    confirmed_finding = dict(base_finding)
    confirmed_finding["confirmed"] = True
    c_confirmed = compute_confidence(confirmed_finding)

    assert round(c_confirmed - c_unconfirmed, 2) == 0.10, f"Expected +0.10 boost, got {c_confirmed - c_unconfirmed}"
    print(f"PASS: test_feedback_confirmation passed (boost from {c_unconfirmed} to {c_confirmed})")


def test_clause_not_in_force():
    """Clause not in force: -0.10 penalty."""
    base_finding = {
        "id": "overcharge:TX-US-2035-0005",
        "class": "overcharge",
        "verdict": "finding",
        "amount_usd": "50.00",
        "source_authority": "signed_contract",
        "evidence": [{"doc": "contract.pdf", "page": 1, "quote": "Valid quote 123456"}],
    }
    c_normal = compute_confidence(base_finding)

    not_in_force_finding = dict(base_finding)
    not_in_force_finding["clause_in_force"] = False
    c_not_in_force = compute_confidence(not_in_force_finding)

    assert round(c_normal - c_not_in_force, 2) == 0.10, f"Expected -0.10 penalty, got {c_normal - c_not_in_force}"
    print(f"PASS: test_clause_not_in_force passed (penalty from {c_normal} to {c_not_in_force})")


def main():
    test_high_confidence()
    test_low_confidence()
    test_conflict_scenario()
    test_feedback_confirmation()
    test_clause_not_in_force()
    print("\nALL CONFIDENCE UNIT TESTS PASSED!")


if __name__ == "__main__":
    main()
