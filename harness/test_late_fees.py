#!/usr/bin/env python
"""Unit tests for the Late Fees detector (harness/checks/late_fees.py)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Add repo root and harness to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "harness"))

from checks.late_fees import check_late_fees


def test_example_late_fee_not_owed():
    """Test 1: Load expected_finding.json and verify check_late_fees matches exactly.

    Invoice TX-US-2032-5494 had due date extended to 2032-08-25 by authorized person.
    It was paid on 2032-08-17 (paid on time before extended due date).
    Invoice TX-US-2032-5496 billed a late fee of 12.65 USD.
    Fee is NOT owed. Output must match expected_finding.json exactly.
    """
    expected_path = REPO_ROOT / "data" / "examples" / "late_fees" / "expected_finding.json"
    expected_doc = json.loads(expected_path.read_text(encoding="utf-8"))

    as_of = expected_doc["as_of"]  # "2033-02-28"
    exp_item = expected_doc["findings"][0]

    contract_fact = {
        "fact_type": "contract_terms",
        "contract_id": "TBX-US-EXP-E42",
        "grace_days": 5,
        "late_fee_rate_pct": "1.86",
        "extension_clause": (
            "10.5 Extension of due date. A due date may be extended by written notice from the "
            "Carrier's Authorised Person (payment terms); the extended date replaces the due date for clause 10.2."
        ),
        "extension_clause_page": 4,
        "source": {"doc_ref": "batch_01/0002.eml#MSA-TBX-US-EXP-E42.pdf", "page": 4},
        "schedule_1_contacts": [
            {
                "name": "Ulrich Ellwood",
                "role": "Billing Specialist",
                "categories": ["payment_terms"],
                "email": "ulrich.ellwood@carrier.example",
            }
        ],
    }

    extension_fact = {
        "fact_type": "email_event",
        "event_type": "payment_extension",
        "email_date": "2032-08-07",
        "sender_name": "Ulrich Ellwood",
        "sender_email": "ulrich.ellwood@carrier.example",
        "referenced_invoice_id": "TX-US-2032-5494",
        "extended_due_date": "2032-08-25",
        "quote": "We extend the due date of invoice TX-US-2032-5494 to 2032-08-25.",
        "source": {"doc_ref": "batch_01/0005.eml", "page": 1},
    }

    payment_fact = {
        "fact_type": "payment",
        "date": "2032-08-31",
        "payment_entries": [
            {
                "invoice_id": "TX-US-2032-5494",
                "invoice_date": "2032-06-26",
                "due_date": "2032-08-10",
                "amount": "680.12",
                "status": "paid",
                "paid_date": "2032-08-17",
                "quote": "TX-US-2032-5494 | 2032-06-26 | due 2032-08-10 | USD 680.12 | paid | 2032-08-17",
                "source": {"doc_ref": "batch_01/0008.eml#ap-register-2032-08-31.xlsx", "page": 1},
            }
        ],
    }

    invoice_fact = {
        "fact_type": "invoice",
        "invoice_id": "TX-US-2032-5496",
        "invoice_date": "2032-08-22",
        "contract_id": "TBX-US-EXP-E42",
        "source": {"doc_ref": "batch_01/0006.eml#TX-US-2032-5496.pdf", "page": 1},
        "lines": [
            {
                "line_no": 13,
                "charge_type": "Late fee",
                "description": "Late fee on TX-US-2032-5494: USD 680.12 overdue, month 1 @ 1.86% - 1 month 1.86% 12.65",
                "amount": "12.65",
                "quote": "13 - - Late fee on TX-US-2032-5494: USD 680.12 overdue, month 1 @ 1.86% - 1 month 1.86% 12.65",
                "source": {"doc_ref": "batch_01/0006.eml#TX-US-2032-5496.pdf", "page": 1},
            }
        ],
    }

    facts = [contract_fact, extension_fact, payment_fact, invoice_fact]
    findings = check_late_fees(facts, as_of_date=as_of, mode="supervised")

    assert len(findings) == 1, f"Expected 1 finding, got {len(findings)}"
    finding = findings[0]

    for key, expected_val in exp_item.items():
        actual_val = finding.get(key)
        assert actual_val == expected_val, f"Mismatch on {key}: expected {expected_val!r}, got {actual_val!r}"

    print("PASS: test_example_late_fee_not_owed matched expected_finding.json exactly")


def test_lookalike_fee_owed_does_not_flag():
    """Test 2: Look-alike from EXPLANATION.md: late fee IS owed.

    TX-US-2032-5498 had extended due date 2032-12-12.
    Paid 2033-01-01 (20 days late > 5 days grace).
    1 month's fee @ 1.86% on 4200.07 is 78.12.
    Invoice TX-US-2033-5582 billed 78.12.
    Fee is owed, so it must NOT flag as late_fee_not_owed.
    """
    contract_fact = {
        "fact_type": "contract_terms",
        "contract_id": "TBX-US-EXP-E42",
        "grace_days": 5,
        "late_fee_rate_pct": "1.86",
    }

    payment_fact = {
        "fact_type": "payment",
        "payment_entries": [
            {
                "invoice_id": "TX-US-2032-5498",
                "due_date": "2032-12-12",
                "amount": "4200.07",
                "status": "paid",
                "paid_date": "2033-01-01",
            }
        ],
    }

    invoice_fact = {
        "fact_type": "invoice",
        "invoice_id": "TX-US-2033-5582",
        "invoice_date": "2033-01-18",
        "lines": [
            {
                "charge_type": "Late fee",
                "description": "Late fee on TX-US-2032-5498: USD 4200.07 overdue, month 1 @ 1.86% - 1 month 1.86% 78.12",
                "amount": "78.12",
            }
        ],
    }

    findings = check_late_fees([contract_fact, payment_fact, invoice_fact], as_of_date="2033-02-28")
    not_owed = [f for f in findings if f.get("class") == "late_fee_not_owed"]
    assert len(not_owed) == 0, f"Expected 0 findings when late fee is owed, got {len(not_owed)}"
    print("PASS: test_lookalike_fee_owed_does_not_flag correctly produced 0 findings")


def test_paid_within_grace_period_not_owed():
    """Test 3: Invoice paid 3 days late, within 5 days grace period -> fee NOT owed."""
    contract_fact = {
        "fact_type": "contract_terms",
        "contract_id": "TBX-01",
        "grace_days": 5,
        "late_fee_rate_pct": "2.0",
        "source": {"doc_ref": "contract.pdf", "page": 1},
    }

    payment_fact = {
        "fact_type": "payment",
        "payment_entries": [
            {
                "invoice_id": "TX-US-001",
                "due_date": "2033-05-10",
                "amount": "500.00",
                "status": "paid",
                "paid_date": "2033-05-13",  # 3 days late <= 5 grace days
                "quote": "TX-US-001 paid 2033-05-13",
                "source": {"doc_ref": "ap.xlsx", "page": 1},
            }
        ],
    }

    invoice_with_fee = {
        "fact_type": "invoice",
        "invoice_id": "TX-US-002",
        "invoice_date": "2033-05-20",
        "source": {"doc_ref": "inv2.pdf", "page": 1},
        "lines": [
            {
                "charge_type": "Late fee",
                "description": "Late fee on TX-US-001",
                "amount": "10.00",
                "quote": "Late fee on TX-US-001: 10.00",
                "source": {"doc_ref": "inv2.pdf", "page": 1},
            }
        ],
    }

    findings = check_late_fees([contract_fact, payment_fact, invoice_with_fee], as_of_date="2033-06-01")
    not_owed = [f for f in findings if f.get("class") == "late_fee_not_owed"]
    assert len(not_owed) == 1
    assert not_owed[0]["amount_usd"] == "10.00"
    print("PASS: test_paid_within_grace_period_not_owed passed")


def test_late_fee_risk():
    """Test 4: Unpaid invoice due within 1 to 14 days of as_of -> flags late_fee_risk."""
    contract_fact = {
        "fact_type": "contract_terms",
        "contract_id": "TBX-01",
        "late_fee_rate_pct": "1.5",
    }

    # Invoice due 2033-03-08, as_of is 2033-02-28 (8 days until due -> in [1, 14] window)
    unpaid_invoice = {
        "fact_type": "invoice",
        "invoice_id": "TX-US-RISK-100",
        "invoice_date": "2033-01-20",
        "due_date": "2033-03-08",
        "invoice_total": "2000.00",
        "quote": "Invoice TX-US-RISK-100 total 2000.00 due 2033-03-08",
        "source": {"doc_ref": "inv_risk.pdf", "page": 1},
    }

    findings = check_late_fees([contract_fact, unpaid_invoice], as_of_date="2033-02-28")
    risk = [f for f in findings if f.get("class") == "late_fee_risk"]
    assert len(risk) == 1
    assert risk[0]["deadline"] == "2033-03-08"
    assert risk[0]["amount_usd"] == "30.00"  # 1.5% of 2000.00
    assert risk[0]["action"] == "internal:payment_reminder"
    assert risk[0]["confidence"] == 0.90
    print("PASS: test_late_fee_risk passed")


def test_late_fee_risk_outside_window():
    """Test 5: Unpaid invoice due 30 days away (> 14 days) -> does NOT flag late_fee_risk."""
    contract_fact = {
        "fact_type": "contract_terms",
        "contract_id": "TBX-01",
        "late_fee_rate_pct": "1.5",
    }

    unpaid_invoice_far = {
        "fact_type": "invoice",
        "invoice_id": "TX-US-FAR-100",
        "invoice_date": "2033-02-01",
        "due_date": "2033-03-30",  # 30 days after as_of 2033-02-28 (> 14 days)
        "invoice_total": "2000.00",
    }

    findings = check_late_fees([contract_fact, unpaid_invoice_far], as_of_date="2033-02-28")
    risk = [f for f in findings if f.get("class") == "late_fee_risk"]
    assert len(risk) == 0, f"Expected 0 risk findings for due date outside window, got {len(risk)}"
    print("PASS: test_late_fee_risk_outside_window passed")


def main():
    print("=" * 60)
    print("Running Late Fees detector unit tests")
    print("=" * 60)
    test_example_late_fee_not_owed()
    test_lookalike_fee_owed_does_not_flag()
    test_paid_within_grace_period_not_owed()
    test_late_fee_risk()
    test_late_fee_risk_outside_window()
    print("\nALL LATE FEES UNIT TESTS PASSED!")


if __name__ == "__main__":
    main()
