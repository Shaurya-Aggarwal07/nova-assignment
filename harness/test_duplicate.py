#!/usr/bin/env python
"""Unit tests for the Duplicate detector (harness/checks/duplicate.py)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Add repo root and harness to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "harness"))

from checks.duplicate import check_duplicate


def test_example_duplicate_resend():
    """Test 1: Load expected_finding.json and verify check_duplicate matches exactly.

    Invoice TX-US-2033-5511 was paid on 2033-02-07 per AP register.
    A resend email arrives on 2033-02-18 asking for payment again.
    Must flag TX-US-2033-5511 as duplicate with amount 1259.23 USD.
    """
    expected_path = REPO_ROOT / "data" / "examples" / "duplicate" / "expected_finding.json"
    expected_doc = json.loads(expected_path.read_text(encoding="utf-8"))

    as_of = expected_doc["as_of"]  # "2033-08-31"
    exp_item = expected_doc["findings"][0]

    payment_fact = {
        "fact_type": "payment",
        "date": "2033-02-28",
        "source": {"doc_ref": "batch_01/0006.eml#ap-register-2033-02-28.xlsx", "page": 1},
        "quote": "TX-US-2033-5511 | 2033-01-31 | due 2033-03-02 | USD 1259.23 | paid | 2033-02-07",
        "payment_entries": [
            {
                "invoice_id": "TX-US-2033-5511",
                "invoice_date": "2033-01-31",
                "due_date": "2033-03-02",
                "amount": "1259.23",
                "status": "paid",
                "paid_date": "2033-02-07",
                "quote": "TX-US-2033-5511 | 2033-01-31 | due 2033-03-02 | USD 1259.23 | paid | 2033-02-07",
                "source": {"doc_ref": "batch_01/0006.eml#ap-register-2033-02-28.xlsx", "page": 1},
            }
        ],
    }

    email_resend = {
        "fact_type": "email_event",
        "event_type": "other",
        "email_date": "2033-02-18",
        "referenced_invoice_id": "TX-US-2033-5511",
        "subject": "Invoice TX-US-2033-5511",
        "quote": "Please process payment at your earliest convenience.",
        "source": {"doc_ref": "batch_01/0004.eml", "page": 1},
    }

    invoice_fact = {
        "fact_type": "invoice",
        "invoice_id": "TX-US-2033-5511",
        "invoice_date": "2033-01-31",
        "invoice_total": "1259.23",
        "source": {"doc_ref": "batch_01/0003.eml#TX-US-2033-5511.pdf", "page": 1},
    }

    facts = [payment_fact, email_resend, invoice_fact]
    findings = check_duplicate(facts, as_of_date=as_of, mode="supervised")

    assert len(findings) == 1, f"Expected 1 finding, got {len(findings)}"
    finding = findings[0]

    for key, expected_val in exp_item.items():
        actual_val = finding.get(key)
        assert actual_val == expected_val, f"Mismatch on {key}: expected {expected_val!r}, got {actual_val!r}"

    print("PASS: test_example_duplicate_resend matched expected_finding.json exactly")


def test_cross_invoice_line_duplicate():
    """Test 2: Same shipment and charge billed on two different invoices months apart with different wording.

    Invoice 1 on 2033-01-15: TE889900 Air freight 750.00
    Invoice 2 on 2033-05-15 (4 months later): TE889900 Airfreight charge 750.00
    Must flag Invoice 2 as duplicate of Invoice 1.
    """
    inv1 = {
        "fact_type": "invoice",
        "invoice_id": "INV-EARLY-101",
        "invoice_date": "2033-01-15",
        "source": {"doc_ref": "inv1.pdf", "page": 1},
        "quote": "Line 1: AWB TE889900 Air freight 750.00",
        "lines": [
            {
                "shipment_id": "TE889900",
                "charge_type": "Air freight",
                "amount": "750.00",
                "quote": "Line 1: AWB TE889900 Air freight 750.00",
                "source": {"doc_ref": "inv1.pdf", "page": 1},
            }
        ],
    }

    inv2 = {
        "fact_type": "invoice",
        "invoice_id": "INV-LATE-202",
        "invoice_date": "2033-05-15",
        "source": {"doc_ref": "inv2.pdf", "page": 1},
        "quote": "Line 1: AWB TE889900 Airfreight charge 750.00",
        "lines": [
            {
                "shipment_id": "TE889900",
                "charge_type": "Airfreight charge",
                "amount": "750.00",
                "quote": "Line 1: AWB TE889900 Airfreight charge 750.00",
                "source": {"doc_ref": "inv2.pdf", "page": 1},
            }
        ],
    }

    findings = check_duplicate([inv1, inv2], as_of_date="2033-06-01")
    assert len(findings) == 1, f"Expected 1 finding, got {len(findings)}"
    f = findings[0]
    assert f["invoice_id"] == "INV-LATE-202"
    assert f["verdict"] == "finding"
    assert f["amount_usd"] == "750.00"
    assert f["confidence"] == 0.95
    assert len(f["evidence"]) == 2
    assert f["evidence"][0]["quote"] == "Line 1: AWB TE889900 Air freight 750.00"
    assert f["evidence"][1]["quote"] == "Line 1: AWB TE889900 Airfreight charge 750.00"
    print("PASS: test_cross_invoice_line_duplicate passed")


def test_lookalike_different_charge_types_does_not_flag():
    """Test 3: Look-alike test: same shipment with genuinely different charge types.

    Invoice 1 bills "Air freight" on shipment SHP-500.
    Invoice 2 bills "Handling fee" on shipment SHP-500.
    These are legitimate separate charges, NOT a duplicate. Must NOT flag.
    """
    inv1 = {
        "fact_type": "invoice",
        "invoice_id": "INV-A",
        "invoice_date": "2033-02-01",
        "lines": [
            {"shipment_id": "SHP-500", "charge_type": "Air freight", "amount": "400.00"}
        ],
    }

    inv2 = {
        "fact_type": "invoice",
        "invoice_id": "INV-B",
        "invoice_date": "2033-02-15",
        "lines": [
            {"shipment_id": "SHP-500", "charge_type": "Handling fee", "amount": "400.00"}
        ],
    }

    findings = check_duplicate([inv1, inv2], as_of_date="2033-03-01")
    assert len(findings) == 0, f"Expected 0 findings for different charge types, got {len(findings)}"
    print("PASS: test_lookalike_different_charge_types_does_not_flag correctly produced 0 findings")


def test_lookalike_split_house_awbs_does_not_flag():
    """Test 4: Look-alike from EXPLANATION.md: split house AWBs on the same master.

    Invoice 1 bills house AWB TE19536828-H1 (240 kg, 981.60).
    Invoice 2 bills house AWB TE19536828-H2 (103 kg, 421.27).
    They bill different parts of the master AWB. Must NOT flag.
    """
    inv1 = {
        "fact_type": "invoice",
        "invoice_id": "TX-US-2033-5512",
        "invoice_date": "2033-05-20",
        "lines": [
            {
                "shipment_id": "TE19536828-H1",
                "charge_type": "Air freight",
                "amount": "981.60",
                "weight_kg": "240",
            }
        ],
    }

    inv2 = {
        "fact_type": "invoice",
        "invoice_id": "TX-US-2033-5514",
        "invoice_date": "2033-05-27",
        "lines": [
            {
                "shipment_id": "TE19536828-H2",
                "charge_type": "Air freight",
                "amount": "421.27",
                "weight_kg": "103",
            }
        ],
    }

    findings = check_duplicate([inv1, inv2], as_of_date="2033-08-31")
    assert len(findings) == 0, f"Expected 0 findings for split house AWBs, got {len(findings)}"
    print("PASS: test_lookalike_split_house_awbs_does_not_flag correctly produced 0 findings")


def main():
    print("=" * 60)
    print("Running Duplicate detector unit tests")
    print("=" * 60)
    test_example_duplicate_resend()
    test_cross_invoice_line_duplicate()
    test_lookalike_different_charge_types_does_not_flag()
    test_lookalike_split_house_awbs_does_not_flag()
    print("\nALL DUPLICATE UNIT TESTS PASSED!")


if __name__ == "__main__":
    main()
