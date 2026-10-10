#!/usr/bin/env python
"""Unit tests for the Overcharge detector (harness/checks/overcharge.py)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Add repo root and harness to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "harness"))

from checks.overcharge import check_overcharge


def test_example_overcharge():
    """Test 1: Load expected_finding.json and verify check_overcharge matches exactly."""
    expected_path = REPO_ROOT / "data" / "examples" / "overcharge" / "expected_finding.json"
    expected_doc = json.loads(expected_path.read_text(encoding="utf-8"))

    as_of = expected_doc["as_of"]  # "2032-08-31"
    expected_findings = expected_doc["findings"]
    assert len(expected_findings) >= 1
    exp_item = expected_findings[0]  # The overcharge finding for TX-US-2032-5394

    contract_fact = {
        "fact_type": "contract_terms",
        "contract_id": "TBX-US-EXP-E60",
        "effective_date": "2031-12-17",
        "end_date": "2032-12-16",
        "source": {
            "doc_ref": "batch_01/0002.eml#MSA-TBX-US-EXP-E60.pdf",
            "page": 3,
        },
        "quote": "3.2 Pricing date. Each shipment is priced at the rates, surcharges and fees in force on its Pickup Date.",
        "pricing_clause": "3.2 Pricing date. Each shipment is priced at the rates, surcharges and fees in force on its Pickup Date.",
        "pricing_clause_page": 3,
        "rates": [
            {
                "lane": "USE2-INE2",
                "weight_band": "100-299.5 kg",
                "rate": "6.01",
                "source": {
                    "doc_ref": "batch_01/0002.eml#MSA-TBX-US-EXP-E60.pdf",
                    "page": 8,
                },
                "quote": "USE2-INE2 100-299.5 kg 6.01 6.16",
            },
            {
                "lane": "USE2-INE2",
                "weight_band": "0-44.5 kg",
                "rate": "7.73",
                "source": {
                    "doc_ref": "batch_01/0002.eml#MSA-TBX-US-EXP-E60.pdf",
                    "page": 8,
                },
                "quote": "USE2-INE2 0-44.5 kg 7.73 7.93",
            },
            {
                "lane": "USE2-INE2",
                "weight_band": "45-99.5 kg",
                "rate": "6.81",
                "source": {
                    "doc_ref": "batch_01/0002.eml#MSA-TBX-US-EXP-E60.pdf",
                    "page": 8,
                },
                "quote": "USE2-INE2 45-99.5 kg 6.81 6.99",
            },
        ],
        "surcharges": [
            {
                "name": "fuel_surcharge",
                "rate_or_amount": "20.16",
            }
        ],
    }

    tracking_fact = {
        "fact_type": "shipment_record",
        "awb": "TE56915687",
        "source": {
            "doc_ref": "batch_01/0005.eml#tracking-2031-07-01.xlsx",
            "page": 1,
        },
        "quote": "TE56915687 | PICKED_UP | 2031-12-25 | USE2 | -",
        "tracking_events": [
            {"event": "PICKED_UP", "date": "2031-12-25"}
        ],
    }

    invoice_fact = {
        "fact_type": "invoice",
        "invoice_id": "TX-US-2032-5394",
        "invoice_date": "2032-01-02",
        "source": {
            "doc_ref": "batch_01/0003.eml#TX-US-2032-5394.pdf",
            "page": 1,
        },
        "quote": "1 TE56915687 2031-12-25 Air freight USE2-INE2 241 kg 6.69 1612.29",
        "lines": [
            {
                "shipment_id": "TE56915687",
                "awb": "TE56915687",
                "pickup_date": "2031-12-25",
                "lane": "USE2-INE2",
                "charge_type": "Air freight",
                "weight_kg": "241",
                "rate": "6.69",
                "amount": "1612.29",
                "quote": "1 TE56915687 2031-12-25 Air freight USE2-INE2 241 kg 6.69 1612.29",
                "source": {
                    "doc_ref": "batch_01/0003.eml#TX-US-2032-5394.pdf",
                    "page": 1,
                },
            },
            {
                "shipment_id": "TE56915687",
                "awb": "TE56915687",
                "pickup_date": "2031-12-25",
                "lane": "USE2-INE2",
                "charge_type": "Fuel surcharge",
                "rate": "20.16%",
                "amount": "325.04",
            },
        ],
    }

    facts = [contract_fact, tracking_fact, invoice_fact]
    findings = check_overcharge(facts, as_of_date=as_of, mode="supervised")

    assert len(findings) == 1, f"Expected 1 finding, got {len(findings)}"
    finding = findings[0]

    # Verify every field against expected finding
    for key, expected_val in exp_item.items():
        actual_val = finding.get(key)
        assert actual_val == expected_val, f"Mismatch on {key}: expected {expected_val!r}, got {actual_val!r}"

    print("PASS: test_example_overcharge matched expected_finding.json exactly")


def test_lookalike_decoy():
    """Test 2: Look-alike test from EXPLANATION.md (invoice at correct rate).

    Invoice TX-US-2032-5397 bills 44 kg at 7.73 per kg on lane USE2-INE2.
    Band 0-44.5 kg has contract rate 7.73.
    Billed rate equals contract rate for that band, so it must NOT flag.
    """
    contract_fact = {
        "fact_type": "contract_terms",
        "contract_id": "TBX-US-EXP-E60",
        "effective_date": "2031-12-17",
        "end_date": "2032-12-16",
        "source": {
            "doc_ref": "batch_01/0002.eml#MSA-TBX-US-EXP-E60.pdf",
            "page": 3,
        },
        "quote": "3.4 Minimum charge. The per-shipment minimum in Annex A applies where rate x chargeable weight is lower. Each shipment is rated at the weight band that contains its chargeable weight.",
        "pricing_clause": "3.4 Minimum charge. The per-shipment minimum in Annex A applies where rate x chargeable weight is lower. Each shipment is rated at the weight band that contains its chargeable weight.",
        "rates": [
            {
                "lane": "USE2-INE2",
                "weight_band": "0-44.5 kg",
                "rate": "7.73",
                "source": {
                    "doc_ref": "batch_01/0002.eml#MSA-TBX-US-EXP-E60.pdf",
                    "page": 8,
                },
                "quote": "USE2-INE2 0-44.5 kg 7.73 7.93",
            },
            {
                "lane": "USE2-INE2",
                "weight_band": "45-99.5 kg",
                "rate": "6.81",
                "source": {
                    "doc_ref": "batch_01/0002.eml#MSA-TBX-US-EXP-E60.pdf",
                    "page": 8,
                },
                "quote": "USE2-INE2 45-99.5 kg 6.81 6.99",
            },
        ],
        "surcharges": [{"name": "fuel_surcharge", "rate_or_amount": "22.00"}],
    }

    tracking_fact = {
        "fact_type": "shipment_record",
        "awb": "TE68271803",
        "source": {
            "doc_ref": "batch_01/0005.eml#tracking-2031-07-01.xlsx",
            "page": 1,
        },
        "quote": "TE68271803 | PICKED_UP | 2032-01-18 | USE2 | -",
        "tracking_events": [{"event": "PICKED_UP", "date": "2032-01-18"}],
    }

    decoy_invoice = {
        "fact_type": "invoice",
        "invoice_id": "TX-US-2032-5397",
        "invoice_date": "2032-01-26",
        "source": {
            "doc_ref": "batch_01/0004.eml#TX-US-2032-5397.pdf",
            "page": 1,
        },
        "quote": "1 TE68271803 2032-01-18 Air freight USE2-INE2 44 kg 7.73 340.12",
        "lines": [
            {
                "shipment_id": "TE68271803",
                "awb": "TE68271803",
                "pickup_date": "2032-01-18",
                "lane": "USE2-INE2",
                "charge_type": "Air freight",
                "weight_kg": "44",
                "rate": "7.73",
                "amount": "340.12",
            },
            {
                "shipment_id": "TE68271803",
                "awb": "TE68271803",
                "pickup_date": "2032-01-18",
                "lane": "USE2-INE2",
                "charge_type": "Fuel surcharge",
                "rate": "22.00%",
                "amount": "74.83",
            },
        ],
    }

    findings = check_overcharge([contract_fact, tracking_fact, decoy_invoice], as_of_date="2032-08-31")
    assert len(findings) == 0, f"Expected 0 findings for decoy at correct rate, got {len(findings)}"
    print("PASS: test_lookalike_decoy correctly produced 0 findings (did NOT flag)")


def test_future_rate_change_does_not_flag():
    """Test 3: Rate change that applies to future pickups but not this one should NOT flag."""
    contract_fact = {
        "fact_type": "contract_terms",
        "contract_id": "MSA-FUTURE-01",
        "effective_date": "2032-01-01",
        "end_date": "2032-12-31",
        "rates": [
            {
                "lane": "USC1-USS2",
                "weight_band": "0-100 kg",
                "rate": "6.00",
            }
        ],
    }

    # Amendment effective from 2032-06-01 reducing rate to 5.00
    future_amendment = {
        "fact_type": "amendment",
        "amends_contract": "MSA-FUTURE-01",
        "effective_date": "2032-06-01",
        "document_date": "2032-05-15",
        "changes": [
            {"field": "rate", "lane": "USC1-USS2", "new_value": "5.00"}
        ],
    }

    # Invoice for pickup on 2032-03-15 (before amendment effective date) billed at 6.00
    invoice_fact = {
        "fact_type": "invoice",
        "invoice_id": "INV-FUTURE-01",
        "invoice_date": "2032-03-20",
        "lines": [
            {
                "shipment_id": "SHP-001",
                "pickup_date": "2032-03-15",
                "lane": "USC1-USS2",
                "charge_type": "Air freight",
                "weight_kg": "50",
                "rate": "6.00",
                "amount": "300.00",
            }
        ],
    }

    # As of 2032-08-31, amendment is known, but does NOT apply to 2032-03-15 pickup
    findings = check_overcharge(
        [contract_fact, future_amendment, invoice_fact],
        as_of_date="2032-08-31",
    )
    assert len(findings) == 0, f"Expected 0 findings for pickup before amendment effective date, got {len(findings)}"
    print("PASS: test_future_rate_change_does_not_flag passed")


def test_backdated_amendment_flags_with_retro():
    """Test 4: Backdated amendment (effective_date before document_date) applies retrospectively."""
    contract_fact = {
        "fact_type": "contract_terms",
        "contract_id": "MSA-RETRO-01",
        "effective_date": "2032-01-01",
        "end_date": "2032-12-31",
        "source": {"doc_ref": "contract.pdf", "page": 1},
        "quote": "Base rate 7.00 per kg.",
        "rates": [
            {
                "lane": "USE1-USW1",
                "weight_band": "0-100 kg",
                "rate": "7.00",
                "source": {"doc_ref": "contract.pdf", "page": 2},
                "quote": "USE1-USW1 0-100 kg 7.00",
            }
        ],
    }

    # Invoice for pickup on 2032-03-01 billed at contract rate 7.00 (amount 350.00)
    invoice_fact = {
        "fact_type": "invoice",
        "invoice_id": "INV-RETRO-01",
        "invoice_date": "2032-03-10",
        "source": {"doc_ref": "inv-retro.pdf", "page": 1},
        "quote": "1 SHP-101 2032-03-01 Air freight USE1-USW1 50 kg 7.00 350.00",
        "lines": [
            {
                "shipment_id": "SHP-101",
                "pickup_date": "2032-03-01",
                "lane": "USE1-USW1",
                "charge_type": "Air freight",
                "weight_kg": "50",
                "rate": "7.00",
                "amount": "350.00",
            }
        ],
    }

    # Backdated amendment signed 2032-04-15 with retroactive effective date 2032-02-01 reducing rate to 5.50
    backdated_amendment = {
        "fact_type": "amendment",
        "amends_contract": "MSA-RETRO-01",
        "effective_date": "2032-02-01",  # Effective before pickup date
        "document_date": "2032-04-15",   # Signed after effective date -> backdated!
        "source": {"doc_ref": "amendment-retro.pdf", "page": 1},
        "quote": "Retroactive rate 5.50 effective 2032-02-01 signed 2032-04-15",
        "changes": [
            {"field": "rate", "lane": "USE1-USW1", "new_value": "5.50"}
        ],
    }

    # As of 2032-05-01 (after document date), the backdated amendment is in force
    findings = check_overcharge(
        [contract_fact, invoice_fact, backdated_amendment],
        as_of_date="2032-05-01",
    )
    assert len(findings) == 1, f"Expected 1 finding for backdated rate reduction, got {len(findings)}"
    f = findings[0]
    # Correct freight is 50 kg * 5.50 = 275.00. Overcharge is 350.00 - 275.00 = 75.00
    assert f["verdict"] == "finding"
    assert f["amount_usd"] == "75.00"
    assert f.get("retro") is True, f"Expected retro=True for backdated amendment, got {f.get('retro')}"
    print("PASS: test_backdated_amendment_flags_with_retro passed")


def main():
    print("=" * 60)
    print("Running Overcharge detector unit tests")
    print("=" * 60)
    test_example_overcharge()
    test_lookalike_decoy()
    test_future_rate_change_does_not_flag()
    test_backdated_amendment_flags_with_retro()
    print("\nALL OVERCHARGE UNIT TESTS PASSED!")


if __name__ == "__main__":
    main()
