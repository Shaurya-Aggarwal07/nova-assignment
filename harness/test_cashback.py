#!/usr/bin/env python
"""Unit tests for the Cashback detector (harness/checks/cashback_credits.py)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Add repo root and harness to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "harness"))

from checks.cashback_credits import check_cashback_credits, check_cashback


def test_example_cashback():
    """Test 1: Load expected_finding.json and verify check_cashback_credits matches exactly.

    Contract TBX-US-EXP-E58 has volume rebate tiers:
      QS >= 55,000.00: 5%
      QS >= 91,000.00: 7.5%
      QS >= 150,000.00: 10%
    In 2033-Q2:
      TX-US-2033-5462: 21,517.17
      TX-US-2033-5464: 44,049.82
      Total QS = 65,566.99 -> 5% = 3,278.35 USD owed.
    In 2033-Q3:
      TX-US-2033-5468: 49,543.66 qualifying (8,703.54 duty excluded)
      TX-US-2033-5472: 1,837.73 qualifying (186.96 duty excluded)
      Total QS = 51,381.39 < 55,000.00 -> 0% (no finding).
    """
    expected_path = REPO_ROOT / "data" / "examples" / "cashback" / "expected_finding.json"
    if not expected_path.exists():
        expected_path = REPO_ROOT / "data" / "examples" / "cashback_credits" / "expected_finding.json"
    expected_doc = json.loads(expected_path.read_text(encoding="utf-8"))

    as_of = expected_doc["as_of"]  # "2033-11-30"
    expected_cashback = expected_doc["portfolio"]["cashback"]
    assert len(expected_cashback) >= 1
    exp_item = expected_cashback[0]

    contract_fact = {
        "fact_type": "contract_terms",
        "contract_id": "TBX-US-EXP-E58",
        "effective_date": "2033-01-30",
        "end_date": "2034-01-29",
        "source": {
            "doc_ref": "batch_01/0002.eml#MSA-TBX-US-EXP-E58.pdf",
            "page": 4,
        },
        "quote": (
            "7.1 Volume rebate. The Carrier shall credit a rebate on each Quarter's Qualifying Spend (QS) "
            "at the tier reached: QS >= USD 55,000.00: 5%; QS >= USD 91,000.00: 7.5%; "
            "QS >= USD 150,000.00: 10%. The tier rate applies to all QS in the Quarter."
        ),
        "rebate_page": 4,
        "qualifying_spend_clause": (
            "1.1 Qualifying Spend. Qualifying Spend means freight charges (base freight, fuel, "
            "security surcharge and Annex D accessorials) on invoices dated in the period, net of "
            "all credit notes dated in the period whatever invoice they reference (rebate and "
            "improvement credits excepted). It excludes taxes (sales tax, GST), duties and "
            "disbursements, late fees, and rebate or improvement credits. INR amounts are "
            "converted per invoice at the Annex F rate for the month of the invoice date."
        ),
        "qualifying_spend_page": 3,
        "qualifying_spend_doc": "batch_01/0002.eml#MSA-TBX-US-EXP-E58.pdf",
        "rebate_tiers": [
            {"threshold": "55000.00", "rebate_pct": "5"},
            {"threshold": "91000.00", "rebate_pct": "7.5"},
            {"threshold": "150000.00", "rebate_pct": "10"},
        ],
    }

    # Q2 Invoices
    inv1 = {
        "fact_type": "invoice",
        "invoice_id": "TX-US-2033-5462",
        "invoice_date": "2033-04-12",
        "contract_id": "TBX-US-EXP-E58",
        "invoice_total": "21517.17",
        "qualifying_spend": "21517.17",
        "lines": [
            {"charge_type": "Air freight", "amount": "21517.17"}
        ],
        "source": {"doc_ref": "batch_01/0003.eml#TX-US-2033-5462.pdf", "page": 1},
    }
    inv2 = {
        "fact_type": "invoice",
        "invoice_id": "TX-US-2033-5464",
        "invoice_date": "2033-05-28",
        "contract_id": "TBX-US-EXP-E58",
        "invoice_total": "44049.82",
        "qualifying_spend": "44049.82",
        "lines": [
            {"charge_type": "Air freight", "amount": "44049.82"}
        ],
        "source": {"doc_ref": "batch_01/0004.eml#TX-US-2033-5464.pdf", "page": 1},
    }

    # Q3 Invoices (Decoy)
    inv3 = {
        "fact_type": "invoice",
        "invoice_id": "TX-US-2033-5468",
        "invoice_date": "2033-07-31",
        "contract_id": "TBX-US-EXP-E58",
        "invoice_total": "58247.20",
        "lines": [
            {"charge_type": "Air freight", "amount": "49543.66"},
            {"charge_type": "Import duty (disbursement)", "amount": "8703.54"},
        ],
        "source": {"doc_ref": "batch_02/0001.eml#TX-US-2033-5468.pdf", "page": 1},
    }
    inv4 = {
        "fact_type": "invoice",
        "invoice_id": "TX-US-2033-5472",
        "invoice_date": "2033-09-11",
        "contract_id": "TBX-US-EXP-E58",
        "invoice_total": "2024.69",
        "lines": [
            {"charge_type": "Air freight", "amount": "1837.73"},
            {"charge_type": "Customs duty", "amount": "186.96"},
        ],
        "source": {"doc_ref": "batch_02/0002.eml#TX-US-2033-5472.pdf", "page": 1},
    }

    facts = [contract_fact, inv1, inv2, inv3, inv4]

    # Test with include_clean=True to match expected_finding.json including decoy
    findings = check_cashback_credits(facts, as_of_date=as_of, mode="supervised", include_clean=True)
    assert len(findings) == 2, f"Expected 2 items, got {len(findings)}"

    q2_finding = next(f for f in findings if f["period"] == "2033-Q2")
    assert q2_finding["verdict"] == exp_item["verdict"]  # "finding"
    assert q2_finding["amount_usd"] == exp_item["amount_usd"]  # "3278.35"
    assert q2_finding["contract_id"] == exp_item["contract_id"]
    assert q2_finding["credit_type"] == exp_item["credit_type"]
    assert q2_finding["action"] == exp_item["action"]  # "draft:claim_credit"
    assert q2_finding["confidence"] == exp_item["confidence"]  # 0.95
    assert len(q2_finding["evidence"]) == len(exp_item["evidence"])
    for i, ev in enumerate(q2_finding["evidence"]):
        assert ev["doc"] == exp_item["evidence"][i]["doc"]
        assert ev["page"] == exp_item["evidence"][i]["page"]
        assert ev["quote"] == exp_item["evidence"][i]["quote"]

    # Also test default include_clean=False (only findings returned)
    findings_only = check_cashback_credits(facts, as_of_date=as_of, mode="supervised", include_clean=False)
    assert len(findings_only) == 1
    assert findings_only[0]["id"] == "cashback:TBX-US-EXP-E58/2033-Q2/rebate"
    assert findings_only[0]["amount_usd"] == "3278.35"

    print("PASS: test_example_cashback matched expected_finding.json exactly")


def test_lookalike_condition_not_met_does_not_flag():
    """Test 2: Look-alike that is fine: rebate condition not yet met, should NOT flag.

    In 2033-Q3, Qualifying Spend is 51,381.39 which is below the minimum tier threshold 55,000.00.
    check_cashback_credits should NOT produce any finding.
    """
    contract_fact = {
        "fact_type": "contract_terms",
        "contract_id": "TBX-US-EXP-E58",
        "effective_date": "2033-01-30",
        "end_date": "2034-01-29",
        "rebate_tiers": [
            {"threshold": "55000.00", "rebate_pct": "5"},
        ],
    }
    inv_under = {
        "fact_type": "invoice",
        "invoice_id": "TX-US-2033-5468",
        "invoice_date": "2033-07-31",
        "contract_id": "TBX-US-EXP-E58",
        "lines": [
            {"charge_type": "Air freight", "amount": "49543.66"},
            {"charge_type": "Import duty", "amount": "8703.54"},
        ],
    }

    facts = [contract_fact, inv_under]
    findings = check_cashback_credits(facts, as_of_date="2033-11-30", mode="supervised", include_clean=False)
    assert len(findings) == 0, f"Expected 0 findings for under-threshold quarter, got {len(findings)}"
    print("PASS: test_lookalike_condition_not_met_does_not_flag correctly produced 0 findings")


def test_direct_promised_rebate_and_matching_payment():
    """Test 3: Contract with direct rebate_amount and condition.

    - When condition met and unpaid: flags finding
    - When condition met and paid: does NOT flag
    - When condition not met: does NOT flag
    """
    # Sub-test 3a: condition met, unpaid -> flags
    cfact = {
        "fact_type": "contract_terms",
        "contract_id": "TBX-US-DIR-01",
        "rebate_amount": "500.00",
        "rebate_condition_min_spend": "10000.00",
        "rebate_due_date": "2033-06-30",
        "credit_type": "rebate",
        "period": "2033-Q2",
        "source": {"doc_ref": "batch_01/contract.pdf", "page": 2},
        "quote": "Carrier shall pay 500 USD rebate if spend exceeds 10,000 USD.",
    }
    inv = {
        "fact_type": "invoice",
        "invoice_id": "TX-1",
        "invoice_date": "2033-05-10",
        "contract_id": "TBX-US-DIR-01",
        "invoice_total": "15000.00",
    }
    findings = check_cashback_credits([cfact, inv], as_of_date="2033-07-01")
    assert len(findings) == 1
    assert findings[0]["amount_usd"] == "500.00"
    assert findings[0]["verdict"] == "finding"
    print("PASS: direct rebate unpaid flagged correctly")

    # Sub-test 3b: matching payment exists -> does NOT flag
    payment_fact = {
        "fact_type": "payment",
        "contract_id": "TBX-US-DIR-01",
        "date": "2033-06-25",
        "payment_entries": [
            {
                "credit_type": "rebate",
                "amount": "500.00",
                "period": "2033-Q2",
                "status": "paid",
            }
        ],
    }
    findings_paid = check_cashback_credits([cfact, inv, payment_fact], as_of_date="2033-07-01")
    assert len(findings_paid) == 0
    print("PASS: direct rebate paid does not flag")

    # Sub-test 3c: spend below condition -> does NOT flag
    inv_low = {
        "fact_type": "invoice",
        "invoice_id": "TX-2",
        "invoice_date": "2033-05-10",
        "contract_id": "TBX-US-DIR-01",
        "invoice_total": "5000.00",
    }
    findings_unmet = check_cashback_credits([cfact, inv_low], as_of_date="2033-07-01")
    assert len(findings_unmet) == 0
    print("PASS: direct rebate condition unmet does not flag")


def test_autonomous_mode_and_high_value_escalation():
    """Test 4: Autonomous mode generates execute:claim_credit, or human_review if >= 5000."""
    cfact = {
        "fact_type": "contract_terms",
        "contract_id": "TBX-US-AUTO-01",
        "rebate_amount": "4000.00",
        "period": "2033-Q1",
        "rebate_due_date": "2033-03-31",
    }
    findings = check_cashback_credits([cfact], as_of_date="2033-04-01", mode="autonomous")
    assert len(findings) == 1
    assert findings[0]["action"] == "execute:claim_credit"

    # High value >= $5,000 -> human_review
    cfact_high = {
        "fact_type": "contract_terms",
        "contract_id": "TBX-US-AUTO-02",
        "rebate_amount": "6000.00",
        "period": "2033-Q1",
        "rebate_due_date": "2033-03-31",
    }
    findings_high = check_cashback_credits([cfact_high], as_of_date="2033-04-01", mode="autonomous")
    assert len(findings_high) == 1
    assert findings_high[0]["action"] == "human_review"
    print("PASS: autonomous actions and high value gating verified")


def main():
    print("=" * 60)
    print("Running Cashback and credits detector unit tests")
    print("=" * 60)
    test_example_cashback()
    test_lookalike_condition_not_met_does_not_flag()
    test_direct_promised_rebate_and_matching_payment()
    test_autonomous_mode_and_high_value_escalation()
    print("\nALL CASHBACK AND CREDITS UNIT TESTS PASSED!")


if __name__ == "__main__":
    main()
