#!/usr/bin/env python
"""Unit tests for Conflict and Cannot Determine detection and Ingest Order Independence."""
from __future__ import annotations

import difflib
import json
import shutil
import sys
import tempfile
from pathlib import Path

# Add repo root and harness to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "harness"))

from conflicts import detect_conflicts, detect_cannot_determine
try:
    import harness.harness as harness
except ImportError:
    import harness


def test_example_conflict():
    """Test 1: Load data/examples/conflict/expected_finding.json and verify exact match.

    Invoice TX-US-2032-5166 has lane USW1-USC3:
    - Signed amendment sets rate: 2.88 USD
    - Schedule 1 rate notice sets rate: 3.03 USD
    - Precedence clause 13.3 states neither prevails automatically.
    - Amount range: ["0.00", "20.87"].
    """
    expected_path = REPO_ROOT / "data" / "examples" / "conflict" / "expected_finding.json"
    expected_doc = json.loads(expected_path.read_text(encoding="utf-8"))

    as_of = expected_doc["as_of"]  # "2032-11-30"
    exp_item = expected_doc["findings"][0]

    contract_fact = {
        "fact_type": "contract_terms",
        "contract_id": "TBX-US-EXP-E90",
        "effective_date": "2032-02-07",
        "source": {
            "doc_ref": "batch_01/0002.eml#MSA-TBX-US-EXP-E90.pdf",
            "page": 4,
        },
        "quote": (
            "13.3 Unranked instruments. Where a rate notice under clause 3.5 and an amendment state "
            "different rates for the same lane and period, the parties shall resolve the difference "
            "jointly; neither prevails automatically."
        ),
        "unranked_clause": (
            "13.3 Unranked instruments. Where a rate notice under clause 3.5 and an amendment state "
            "different rates for the same lane and period, the parties shall resolve the difference "
            "jointly; neither prevails automatically."
        ),
        "unranked_page": 4,
        "schedule_1_contacts": [
            {
                "name": "Isolde Zeller",
                "role": "Rates Specialist",
                "categories": ["rates"],
                "email": "isolde.zeller@carrier.example",
            }
        ],
        "surcharges": [
            {"name": "fuel_surcharge", "rate_or_amount": "23.14"}
        ],
    }

    amendment_fact = {
        "fact_type": "amendment",
        "amends_contract": "TBX-US-EXP-E90",
        "effective_date": "2032-03-16",
        "lane": "USW1-USC3",
        "rate": "2.88",
        "weight_band": "100-299.5 kg",
        "quote": "USW1-USC3 100-299.5 kg 2.88 USD",
        "source": {
            "doc_ref": "batch_01/0003.eml#TBX-US-EXP-E90-AMD-1.pdf",
            "page": 1,
        },
    }

    rate_notice_fact = {
        "fact_type": "email_event",
        "event_type": "rate_notice",
        "email_date": "2032-06-02",
        "sender_name": "Isolde Zeller",
        "sender_email": "isolde.zeller@carrier.example",
        "referenced_contract_id": "TBX-US-EXP-E90",
        "subject": "Rate notice USW1-USC3",
        "lane": "USW1-USC3",
        "rate": "3.03",
        "quote": "USW1-USC3 100-299.5 kg: 3.03 USD",
        "source": {
            "doc_ref": "batch_02/0001.eml",
            "page": 1,
        },
    }

    invoice_fact = {
        "fact_type": "invoice",
        "invoice_id": "TX-US-2032-5166",
        "invoice_date": "2032-06-26",
        "contract_id": "TBX-US-EXP-E90",
        "invoice_total": "421.62",
        "lines": [
            {
                "lane": "USW1-USC3",
                "charge_type": "Air freight",
                "amount": "421.62",
                "weight_kg": "113",
                "pickup_date": "2032-06-17",
            }
        ],
        "source": {
            "doc_ref": "batch_02/0002.eml#TX-US-2032-5166.pdf",
            "page": 1,
        },
    }

    facts = [contract_fact, amendment_fact, rate_notice_fact, invoice_fact]
    findings = detect_conflicts([], facts, as_of_date=as_of, mode="supervised")

    assert len(findings) == 1, f"Expected 1 finding, got {len(findings)}"
    finding = findings[0]

    assert finding["id"] == exp_item["id"]  # "overcharge:TX-US-2032-5166"
    assert finding["class"] == exp_item["class"]  # "overcharge"
    assert finding["variant"] == exp_item["variant"]  # "XC-01"
    assert finding["verdict"] == exp_item["verdict"]  # "conflict"
    assert finding["amount_usd"] == exp_item["amount_usd"]  # None
    assert finding["amount_range"] == exp_item["amount_range"]  # ["0.00", "20.87"]
    assert finding["action"] == exp_item["action"]  # "escalate"
    assert finding["confidence"] == exp_item["confidence"]  # 0.9

    # Assert both sources cited in evidence
    assert len(finding["evidence"]) == len(exp_item["evidence"])
    for i, ev in enumerate(finding["evidence"]):
        assert ev["doc"] == exp_item["evidence"][i]["doc"]
        assert ev["page"] == exp_item["evidence"][i]["page"]
        assert ev["quote"] == exp_item["evidence"][i]["quote"]

    # Assert sources list is present
    assert "sources" in finding
    assert len(finding["sources"]) >= 2

    print("PASS: test_example_conflict matched expected_finding.json exactly")


def test_missing_contract_cannot_determine():
    """Test 2: Hand-constructed scenario with invoice charges but missing contract.

    Assert that cannot_determine is returned with missing document and holder.
    """
    invoice_fact = {
        "fact_type": "invoice",
        "invoice_id": "TX-MISSING-01",
        "invoice_date": "2035-07-15",
        "contract_id": "TBX-MISSING-CONTRACT",
        "invoice_total": "750.00",
        "lines": [
            {
                "line_no": 1,
                "lane": "USW1-USE1",
                "charge_type": "Air freight",
                "amount": "750.00",
            }
        ],
        "source": {
            "doc_ref": "batch_02/0010.eml#TX-MISSING-01.pdf",
            "page": 1,
        },
        "carrier_name": "Carrier (Tarrowby Express)",
    }

    # Pass invoice fact with NO contract_terms facts
    findings = detect_cannot_determine([], [invoice_fact], as_of_date="2035-08-01", mode="supervised")

    assert len(findings) == 1, f"Expected 1 finding, got {len(findings)}"
    cd_finding = findings[0]

    assert cd_finding["verdict"] == "cannot_determine"
    assert cd_finding["invoice_id"] == "TX-MISSING-01"
    assert cd_finding["amount_usd"] is None
    assert cd_finding["action"] == "draft:request_document"
    assert cd_finding.get("missing_doc") is not None
    assert "Contract" in cd_finding["missing_doc"]["name"]
    assert cd_finding.get("missing_document") is not None
    assert cd_finding.get("document_holder") is not None
    assert len(cd_finding["evidence"]) >= 1

    print("PASS: test_missing_contract_cannot_determine passed")


def test_missing_annex_cannot_determine():
    """Test 3: Missing Annex A rev 2 cited in amendment (sc-6f955cee scenario)."""
    contract_fact = {
        "fact_type": "contract_terms",
        "contract_id": "TBX-US-EXP-01",
        "schedule_1_contacts": [
            {
                "name": "Fintan Corrigan",
                "role": "Pricing Manager",
                "party": "TBX-US",
                "categories": ["rates"],
            }
        ],
        "source": {"doc_ref": "batch_01/0001.eml#MSA-TBX-US-EXP-01.pdf", "page": 1},
    }

    amd_fact = {
        "fact_type": "amendment",
        "amends_contract": "TBX-US-EXP-01",
        "effective_date": "2035-06-04",
        "quote": "2. The Annex A rates for lane(s) USS2-INN1 are deleted and replaced as follows: Annex A rev 2 (attached) applies from 2035-06-04 to lane(s) USS2-INN1.",
        "source": {"doc_ref": "batch_01/0002.eml#TBX-US-EXP-01-AMD-1.pdf", "page": 1},
    }

    inv_fact = {
        "fact_type": "invoice",
        "invoice_id": "TX-US-2035-0237",
        "invoice_date": "2035-06-18",
        "contract_id": "TBX-US-EXP-01",
        "invoice_total": "500.00",
        "lines": [
            {"lane": "USS2-INN1", "pickup_date": "2035-06-10", "amount": "500.00"}
        ],
        "source": {"doc_ref": "batch_02/0001.eml#TX-US-2035-0237.pdf", "page": 1},
    }

    facts = [contract_fact, amd_fact, inv_fact]
    findings = detect_cannot_determine([], facts, as_of_date="2035-09-30", mode="supervised")

    assert len(findings) == 1
    f = findings[0]
    assert f["verdict"] == "cannot_determine"
    assert f["missing_doc"]["name"] == "Annex A rev 2"
    assert f["missing_doc"]["holder"]["person"] == "Fintan Corrigan"
    assert f["missing_doc"]["holder"]["party"] == "TBX-US"
    print("PASS: test_missing_annex_cannot_determine passed")


def test_ingest_order_independence():
    """Test 5: Ingest order independence test per prompt requirements.

    1. Ingest batch_01, then batch_02 -> generate report_batch_01_first.json
    2. Ingest batch_02, then batch_01 (reverse) -> generate report_batch_02_first.json
    3. Assert the two reports are IDENTICAL.
    """
    b1_dir = REPO_ROOT / "data" / "scenarios" / "batch_01"
    b2_dir = REPO_ROOT / "data" / "scenarios" / "batch_02"

    if not b1_dir.is_dir() or not b2_dir.is_dir():
        print("SKIP: scenario batches not found for ingest order test")
        return

    tmp_dir = Path(tempfile.mkdtemp(prefix="nova_order_test_"))
    try:
        # Run 1: Ingest batch_01 first, then batch_02
        state1 = tmp_dir / "state1"
        harness.ingest(str(b1_dir), str(state1))
        harness.ingest(str(b2_dir), str(state1))
        rep1 = harness.report("2035-09-30", mode="supervised", state_dir=str(state1))
        rep1_str = json.dumps(rep1, indent=2, sort_keys=True)

        # Run 2: Ingest batch_02 first, then batch_01 (REVERSE ORDER)
        state2 = tmp_dir / "state2"
        harness.ingest(str(b2_dir), str(state2))
        harness.ingest(str(b1_dir), str(state2))
        rep2 = harness.report("2035-09-30", mode="supervised", state_dir=str(state2))
        rep2_str = json.dumps(rep2, indent=2, sort_keys=True)

        # Save both reports
        rep1_file = tmp_dir / "report_batch_01_first.json"
        rep2_file = tmp_dir / "report_batch_02_first.json"
        rep1_file.write_text(rep1_str, encoding="utf-8")
        rep2_file.write_text(rep2_str, encoding="utf-8")

        diff = list(difflib.unified_diff(
            rep1_str.splitlines(keepends=True),
            rep2_str.splitlines(keepends=True),
            fromfile="report_batch_01_first.json",
            tofile="report_batch_02_first.json",
        ))

        if diff:
            diff_text = "".join(diff)
            print("DIFF DETECTED:")
            print(diff_text)
            assert False, "Reports differ based on ingest order!"
        else:
            print("no diff, test passed")

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main():
    print("=" * 60)
    print("Running Conflict and Cannot Determine detector unit tests")
    print("=" * 60)
    test_example_conflict()
    test_missing_contract_cannot_determine()
    test_missing_annex_cannot_determine()
    print("Running Ingest Order Independence Test...")
    test_ingest_order_independence()
    print("\nALL CONFLICT AND CANNOT DETERMINE TESTS PASSED!")


if __name__ == "__main__":
    main()
