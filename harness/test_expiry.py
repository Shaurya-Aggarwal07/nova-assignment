#!/usr/bin/env python
"""Unit tests for the Expiry detector (harness/checks/expiry.py)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Add repo root and harness to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "harness"))

from checks.expiry import check_expiry


def test_example_expiry():
    """Test 1: Load expected_finding.json and verify check_expiry matches exactly."""
    expected_path = REPO_ROOT / "data" / "examples" / "expiry" / "expected_finding.json"
    expected_doc = json.loads(expected_path.read_text(encoding="utf-8"))

    as_of = expected_doc["as_of"]  # "2034-02-28"
    expected_items = expected_doc["portfolio"]["expiry"]
    assert len(expected_items) == 1
    expected_item = expected_items[0]

    # Contract facts corresponding to data/examples/expiry/docs
    contract_facts = [
        {
            "fact_type": "contract_terms",
            "contract_id": "TBX-US-EXP-E24",
            "effective_date": "2033-03-21",
            "end_date": "2034-03-20",
            "auto_renew": False,
            "source": {
                "doc_ref": "batch_01/0002.eml#MSA-TBX-US-EXP-E24.pdf",
                "page": 3,
            },
            "quote": "2.2 Initial term. The initial term is 12 months, ending on 2034-03-20.",
        }
    ]

    findings = check_expiry(contract_facts, as_of_date=as_of)

    assert len(findings) == 1, f"Expected 1 finding, got {len(findings)}"
    finding = findings[0]

    # Assert exact match per prompt requirements
    assert finding["verdict"] == expected_item["verdict"]  # "finding"
    assert finding["amount_usd"] == expected_item["amount_usd"]  # None
    assert finding["deadline"] == expected_item["deadline"]  # "2034-03-20"
    assert finding["action"] == expected_item["action"]  # "internal:alert"
    assert finding["confidence"] == expected_item["confidence"]  # 0.95
    assert len(finding["evidence"]) == 1
    assert finding["evidence"][0]["quote"] == expected_item["evidence"][0]["quote"]
    assert finding["evidence"][0]["doc"] == expected_item["evidence"][0]["doc"]
    assert finding["evidence"][0]["page"] == expected_item["evidence"][0]["page"]
    assert finding["id"] == expected_item["id"]
    assert finding["contract_id"] == expected_item["contract_id"]

    print("PASS: test_example_expiry matched expected_finding.json exactly")


def test_lookalike_decoy():
    """Test 2: Look-alike test from EXPLANATION.md.

    The same contract, reported as of 2033-08-31, is 201 days away (> 60 days).
    It must NOT flag.
    """
    contract_facts = [
        {
            "fact_type": "contract_terms",
            "contract_id": "TBX-US-EXP-E24",
            "effective_date": "2033-03-21",
            "end_date": "2034-03-20",
            "auto_renew": False,
            "source": {
                "doc_ref": "batch_01/0002.eml#MSA-TBX-US-EXP-E24.pdf",
                "page": 3,
            },
            "quote": "2.2 Initial term. The initial term is 12 months, ending on 2034-03-20.",
        }
    ]

    findings = check_expiry(contract_facts, as_of_date="2033-08-31")
    assert len(findings) == 0, f"Expected 0 findings for decoy outside 60-day window, got {len(findings)}"
    print("PASS: test_lookalike_decoy correctly returned 0 findings (> 60 days)")


def test_auto_renew_and_notice_deadline():
    """Test 3: Auto-renew contracts with notice deadlines."""
    # 3a. Auto-renew with notice deadline inside window
    contract_auto = [
        {
            "fact_type": "contract_terms",
            "contract_id": "MSA-AUTO-01",
            "effective_date": "2033-01-01",
            "end_date": "2034-07-31",
            "auto_renew": True,
            "notice_days": 30,  # Notice deadline = 2034-07-01
            "source": {"doc_ref": "contract.pdf", "page": 1},
            "quote": "Notice of non-renewal must be given 30 days prior.",
        }
    ]

    # as_of = 2034-06-15 -> notice deadline 2034-07-01 is 16 days away (inside window)
    findings = check_expiry(contract_auto, as_of_date="2034-06-15")
    assert len(findings) == 1
    assert findings[0]["deadline"] == "2034-07-01"

    # as_of = 2034-03-01 -> notice deadline 2034-07-01 is 122 days away (outside window)
    findings = check_expiry(contract_auto, as_of_date="2034-03-01")
    assert len(findings) == 0

    # 3b. Auto-renew with no explicit end date -> no flag
    contract_perpetual = [
        {
            "fact_type": "contract_terms",
            "contract_id": "MSA-PERP-01",
            "auto_renew": True,
            "end_date": None,
        }
    ]
    findings = check_expiry(contract_perpetual, as_of_date="2034-06-15")
    assert len(findings) == 0

    print("PASS: test_auto_renew_and_notice_deadline passed")


def test_amendment_extension():
    """Test 4: Amendment extending the term moves the date outside window."""
    facts = [
        {
            "fact_type": "contract_terms",
            "contract_id": "MSA-EXT-01",
            "end_date": "2034-03-20",
            "source": {"doc_ref": "msa.pdf", "page": 1},
            "quote": "Initial term ending on 2034-03-20.",
        },
        {
            "fact_type": "amendment",
            "amends_contract": "MSA-EXT-01",
            "effective_date": "2034-02-01",
            "changes": [{"field": "end_date", "new_value": "2035-03-20"}],
            "source": {"doc_ref": "amd1.pdf", "page": 1},
            "quote": "Term extended to 2035-03-20.",
        },
    ]

    # As of 2034-02-28, with extension, new end_date is 2035-03-20 (> 60 days away)
    findings = check_expiry(facts, as_of_date="2034-02-28")
    assert len(findings) == 0, f"Expected 0 findings after amendment extension, got {len(findings)}"
    print("PASS: test_amendment_extension passed")


def main():
    print("=" * 60)
    print("Running Expiry detector unit tests")
    print("=" * 60)
    test_example_expiry()
    test_lookalike_decoy()
    test_auto_renew_and_notice_deadline()
    test_amendment_extension()
    print("\nALL EXPIRY UNIT TESTS PASSED!")


if __name__ == "__main__":
    main()
