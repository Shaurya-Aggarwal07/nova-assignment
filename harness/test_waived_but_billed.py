#!/usr/bin/env python
"""Unit tests for the Waived but Billed detector (harness/checks/waived_but_billed.py)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Add repo root and harness to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "harness"))

from checks.waived_but_billed import check_waived_but_billed


def test_example_waived_but_billed():
    """Test 1: Load expected_finding.json and verify check_waived_but_billed matches exactly."""
    expected_path = REPO_ROOT / "data" / "examples" / "waived_but_billed" / "expected_finding.json"
    expected_doc = json.loads(expected_path.read_text(encoding="utf-8"))

    as_of = expected_doc["as_of"]  # "2034-02-28"
    exp_item = expected_doc["findings"][0]  # The finding for TX-US-2033-5337

    contract_fact = {
        "fact_type": "contract_terms",
        "contract_id": "TBX-US-EXP-E49",
        "source": {"doc_ref": "batch_01/0002.eml#MSA-TBX-US-EXP-E49.pdf", "page": 4},
        "waiver_clause": "11.1 Waiver authority. A charge may be waived only in writing by a person listed in Schedule 1 for waivers. Schedule 1 is exhaustive.",
        "waiver_clause_page": 4,
        "schedule_1_contacts": [
            {
                "name": "Elspeth Iyengar",
                "role": "Customer Finance Lead",
                "party": "TBX-US",
                "categories": ["waivers", "payment_terms"],
                "email": "elspeth.iyengar@tarrowby.example",
                "quote": "Elspeth Iyengar, Customer Finance Lead, TBX-US: waivers, payment_terms <elspeth.iyengar@tarrowby.example>",
                "page": 11,
                "source": {"doc_ref": "batch_01/0002.eml#MSA-TBX-US-EXP-E49.pdf", "page": 11},
            }
        ],
    }

    waiver_fact = {
        "fact_type": "email_event",
        "event_type": "waiver",
        "email_date": "2033-06-18",
        "sender_name": "Elspeth Iyengar",
        "sender_email": "elspeth.iyengar@tarrowby.example",
        "source": {"doc_ref": "batch_01/0004.eml", "page": 1},
        "quote": "Following our call, we waive the Delivery fee on AWB TE97049901.",
        "waiver_scope": {
            "type": "single_shipment",
            "shipment_ids": ["TE97049901"],
            "charge_types": ["delivery fee"],
        },
    }

    invoice_fact = {
        "fact_type": "invoice",
        "invoice_id": "TX-US-2033-5337",
        "invoice_date": "2033-06-28",
        "source": {"doc_ref": "batch_01/0005.eml#TX-US-2033-5337.pdf", "page": 1},
        "lines": [
            {
                "line_no": 1,
                "shipment_id": "TE97049901",
                "charge_type": "Air freight",
                "amount": "1319.50",
            },
            {
                "line_no": 4,
                "shipment_id": "TE97049901",
                "charge_type": "Delivery fee",
                "amount": "133.44",
                "quote": "4 TE97049901 2033-06-18 Delivery fee USE2-INN1 1 shpt 133.44 133.44",
                "source": {"doc_ref": "batch_01/0005.eml#TX-US-2033-5337.pdf", "page": 1},
            },
        ],
    }

    facts = [contract_fact, waiver_fact, invoice_fact]
    findings = check_waived_but_billed(facts, as_of_date=as_of, mode="supervised")

    assert len(findings) == 1, f"Expected 1 finding, got {len(findings)}"
    finding = findings[0]

    for key, expected_val in exp_item.items():
        actual_val = finding.get(key)
        assert actual_val == expected_val, f"Mismatch on {key}: expected {expected_val!r}, got {actual_val!r}"

    print("PASS: test_example_waived_but_billed matched expected_finding.json exactly")


def test_lookalike_different_shipment_does_not_flag():
    """Test 2: Look-alike from EXPLANATION.md: waiver names a different shipment.

    Waiver names AWB TE33628218.
    Invoice TX-US-2034-5561 bills Delivery fee on AWB TE10597216.
    The waiver must NOT apply to TE10597216.
    """
    contract_fact = {
        "fact_type": "contract_terms",
        "contract_id": "TBX-US-EXP-E49",
        "source": {"doc_ref": "batch_01/0002.eml#MSA-TBX-US-EXP-E49.pdf", "page": 4},
        "waiver_clause": "11.1 Waiver authority. A charge may be waived only in writing by a person listed in Schedule 1 for waivers. Schedule 1 is exhaustive.",
        "schedule_1_contacts": [
            {
                "name": "Elspeth Iyengar",
                "email": "elspeth.iyengar@tarrowby.example",
                "categories": ["waivers"],
                "quote": "Elspeth Iyengar, Customer Finance Lead: waivers",
                "page": 11,
            }
        ],
    }

    waiver_fact = {
        "fact_type": "email_event",
        "event_type": "waiver",
        "email_date": "2033-11-26",
        "sender_name": "Elspeth Iyengar",
        "sender_email": "elspeth.iyengar@tarrowby.example",
        "source": {"doc_ref": "batch_02/0002.eml", "page": 1},
        "quote": "Thanks for flagging this. We waive the Delivery fee on AWB TE33628218 in full.",
        "waiver_scope": {
            "type": "single_shipment",
            "shipment_ids": ["TE33628218"],
            "charge_types": ["delivery fee"],
        },
    }

    decoy_invoice = {
        "fact_type": "invoice",
        "invoice_id": "TX-US-2034-5561",
        "invoice_date": "2034-01-28",
        "source": {"doc_ref": "batch_02/0005.eml#TX-US-2034-5561.pdf", "page": 1},
        "lines": [
            {
                "shipment_id": "TE10597216",
                "charge_type": "Delivery fee",
                "amount": "133.44",
                "quote": "Delivery fee TE10597216 USE2-USC1 23 Jan 2034 133.44 1 shpt 133.44",
            }
        ],
    }

    findings = check_waived_but_billed([contract_fact, waiver_fact, decoy_invoice], as_of_date="2034-02-28")
    assert len(findings) == 0, f"Expected 0 findings for different shipment, got {len(findings)}"
    print("PASS: test_lookalike_different_shipment_does_not_flag correctly produced 0 findings")


def test_waiver_after_invoice_date_does_not_flag():
    """Test 3: Waiver issued AFTER the invoice date does not make the past invoice waived-but-billed."""
    contract_fact = {
        "fact_type": "contract_terms",
        "contract_id": "TBX-01",
        "schedule_1_contacts": [
            {"name": "Auth Person", "email": "auth@carrier.example", "categories": ["waivers"]}
        ],
    }

    # Invoice dated 2033-05-01
    invoice_fact = {
        "fact_type": "invoice",
        "invoice_id": "INV-EARLIER",
        "invoice_date": "2033-05-01",
        "lines": [
            {"shipment_id": "SHP-001", "charge_type": "Handling fee", "amount": "50.00"}
        ],
    }

    # Waiver dated 2033-05-15 (after invoice was issued)
    waiver_fact = {
        "fact_type": "email_event",
        "event_type": "waiver",
        "email_date": "2033-05-15",
        "sender_email": "auth@carrier.example",
        "quote": "We waive handling fee on SHP-001.",
        "waiver_scope": {"type": "single_shipment", "shipment_ids": ["SHP-001"], "charge_types": ["handling fee"]},
    }

    findings = check_waived_but_billed([contract_fact, invoice_fact, waiver_fact], as_of_date="2033-06-01")
    assert len(findings) == 0, f"Expected 0 findings when waiver is after invoice date, got {len(findings)}"
    print("PASS: test_waiver_after_invoice_date_does_not_flag passed")


def test_unauthorized_sender_does_not_flag():
    """Test 4: Waiver from someone not authorized in Schedule 1 must NOT bind."""
    contract_fact = {
        "fact_type": "contract_terms",
        "contract_id": "TBX-01",
        "schedule_1_contacts": [
            {"name": "Alice Smith", "email": "alice@carrier.example", "categories": ["notices"]}
            # Note: Alice is only authorized for notices, NOT waivers
        ],
    }

    waiver_fact = {
        "fact_type": "email_event",
        "event_type": "waiver",
        "email_date": "2033-06-01",
        "sender_email": "alice@carrier.example",
        "quote": "I can waive the delivery fee on SHP-999.",
        "waiver_scope": {"type": "single_shipment", "shipment_ids": ["SHP-999"], "charge_types": ["delivery fee"]},
    }

    invoice_fact = {
        "fact_type": "invoice",
        "invoice_id": "INV-UNAUTH",
        "invoice_date": "2033-06-10",
        "lines": [
            {"shipment_id": "SHP-999", "charge_type": "Delivery fee", "amount": "80.00"}
        ],
    }

    findings = check_waived_but_billed([contract_fact, invoice_fact, waiver_fact], as_of_date="2033-07-01")
    assert len(findings) == 0, f"Expected 0 findings for unauthorized sender, got {len(findings)}"
    print("PASS: test_unauthorized_sender_does_not_flag passed")


def main():
    print("=" * 60)
    print("Running Waived but Billed detector unit tests")
    print("=" * 60)
    test_example_waived_but_billed()
    test_lookalike_different_shipment_does_not_flag()
    test_waiver_after_invoice_date_does_not_flag()
    test_unauthorized_sender_does_not_flag()
    print("\nALL WAIVED BUT BILLED UNIT TESTS PASSED!")


if __name__ == "__main__":
    main()
